"""Pruebas del listado de revisiones de PR por repositorio e inmutabilidad del audit log."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
    generate_webhook_secret,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

_VALID_CIPHERTEXT = "v1." + "A" * 24 + "." + "B" * 43


async def _tenant(session: AsyncSession) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Reviews {suffix}", slug=f"reviews-{suffix}")
    user = User(
        email=f"reviews-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Reviews User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _repository(organization_id: uuid.UUID, suffix: str) -> Repository:
    return Repository(
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"remote-{suffix}",
        name="app",
        full_name=f"acme/app-{suffix}",
        clone_url=f"https://github.com/acme/app-{suffix}.git",
        default_branch="main",
        webhook_secret=generate_webhook_secret(),
    )


def _review(
    organization_id: uuid.UUID,
    repository_id: uuid.UUID,
    number: int,
    status: PRReviewStatusEnum,
) -> PullRequestReview:
    return PullRequestReview(
        organization_id=organization_id,
        repository_id=repository_id,
        pr_number=number,
        pr_title=f"Pull request #{number}",
        pr_author="octocat",
        source_branch="feature/segura",
        target_branch="main",
        commit_sha="a" * 40,
        head_clone_url=f"https://github.com/acme/app/pull/{number}.git",
        base_sha="b" * 40,
        status=status,
        issues_caught_critical=1,
        issues_caught_high=2,
        merge_blocked=True,
    )


@pytest.mark.asyncio
async def test_repository_reviews_are_paginated_and_filtered(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    suffix = uuid.uuid4().hex
    repository = _repository(organization.id, suffix)
    integration_session.add(repository)
    await integration_session.flush()
    integration_session.add_all(
        [
            _review(organization.id, repository.id, 1, PRReviewStatusEnum.PASSED),
            _review(organization.id, repository.id, 2, PRReviewStatusEnum.FAILED),
            _review(organization.id, repository.id, 3, PRReviewStatusEnum.QUEUED),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(
            f"/api/v1/repositories/{repository.id}/reviews?limit=2&offset=0",
            headers=headers,
        )
        failed = await client.get(
            f"/api/v1/repositories/{repository.id}/reviews?status=FAILED",
            headers=headers,
        )
        by_branch = await client.get(
            f"/api/v1/repositories/{repository.id}/reviews?source_branch=feature",
            headers=headers,
        )

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    assert payload["limit"] == 2
    assert payload["offset"] == 0
    assert len(payload["items"]) == 2
    first = payload["items"][0]
    # Nunca se expone el secreto HMAC del webhook ni la URL de clonado del head.
    assert "webhook_secret" not in first
    assert "head_clone_url" not in first
    assert "comment_id" not in first
    assert first["issues_caught_critical"] == 1
    assert first["pr_number"] in {1, 2, 3}
    assert first["repository_name"] == f"acme/app-{suffix}"
    assert len(first["short_sha"]) == 7
    # El orden es `created_at DESC, id DESC`; las tres revisiones comparten
    # `created_at` porque se crean en la misma transacción, así que solo se
    # comprueba que la página devuelve elementos distintos del conjunto total.
    assert {item["pr_number"] for item in payload["items"]} <= {1, 2, 3}

    assert failed.json()["total"] == 1
    assert failed.json()["items"][0]["status"] == "FAILED"
    assert by_branch.json()["total"] == 3


@pytest.mark.asyncio
async def test_repository_reviews_never_leak_other_tenants(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization_a, headers_a = await _tenant(integration_session)
    organization_b, _headers_b = await _tenant(integration_session)
    repository_b = _repository(organization_b.id, uuid.uuid4().hex)
    integration_session.add(repository_b)
    await integration_session.flush()
    integration_session.add(
        _review(organization_b.id, repository_b.id, 99, PRReviewStatusEnum.FAILED)
    )
    await integration_session.commit()
    assert organization_a.id != organization_b.id
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/repositories/{repository_b.id}/reviews",
            headers=headers_a,
        )

    assert response.status_code == 404
    assert "secreto" not in response.text.lower()


@pytest.mark.asyncio
async def test_repository_reviews_require_authentication(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _headers = await _tenant(integration_session)
    repository = _repository(organization.id, uuid.uuid4().hex)
    integration_session.add(repository)
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/repositories/{repository.id}/reviews")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_audit_log_is_append_only(
    integration_session: AsyncSession,
) -> None:
    """R4 exige que el rastro de auditoría no se pueda alterar ni borrar.

    Cada sentencia se aísla en su propio savepoint: sin ellos, la primera
    operación rechazada aborta la transacción y la segunda solo devolvería
    "current transaction is aborted", que no demuestra nada sobre el trigger.
    """

    assert integration_session is not None
    organization, _headers = await _tenant(integration_session)
    user_id = (
        await integration_session.execute(
            select(User.id).where(User.email.like("reviews-%@example.com"))
        )
    ).scalars().first()
    assert user_id is not None
    entry = AuditLogEntry(
        organization_id=organization.id,
        actor_user_id=user_id,
        action=AuditActionEnum.STATUS_CHANGED,
        entity_type="vulnerability",
        entity_id=uuid.uuid4(),
        from_state="OPEN",
        to_state="FIXED",
    )
    integration_session.add(entry)
    await integration_session.commit()
    entry_id = entry.id

    for statement, label in (
        ("UPDATE audit_log SET to_state = 'IGNORED' WHERE id = :entry_id", "update"),
        ("DELETE FROM audit_log WHERE id = :entry_id", "delete"),
        ("TRUNCATE audit_log", "truncate"),
    ):
        with pytest.raises(Exception) as rejection:
            async with integration_session.begin_nested():
                await integration_session.execute(text(statement), {"entry_id": entry_id})
        assert "append-only" in str(rejection.value).lower(), (
            f"{label} no fue bloqueado por el trigger: {rejection.value}"
        )

    await integration_session.rollback()
    # La entrada sigue ahí: los tres rechazos no alteraron el rastro.
    remaining = (
        await integration_session.execute(
            select(func.count(AuditLogEntry.id)).where(AuditLogEntry.id == entry_id)
        )
    ).scalar_one()
    assert int(remaining) == 1


@pytest.mark.asyncio
async def test_credentials_are_not_exposed_by_reviews_endpoint(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = _repository(organization.id, uuid.uuid4().hex)
    integration_session.add(repository)
    await integration_session.flush()
    integration_session.add(
        GitCredential(
            organization_id=organization.id,
            provider=GitProviderEnum.GITHUB,
            encrypted_access_token=_VALID_CIPHERTEXT,
        )
    )
    integration_session.add(
        _review(organization.id, repository.id, 7, PRReviewStatusEnum.SCANNING)
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/repositories/{repository.id}/reviews",
            headers=headers,
        )

    assert response.status_code == 200
    assert _VALID_CIPHERTEXT not in response.text
    assert repository.webhook_secret not in response.text
