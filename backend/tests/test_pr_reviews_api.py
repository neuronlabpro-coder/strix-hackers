"""Pruebas del listado global de revisiones de PR y sus KPIs.

El endpoint por repositorio ya existía (`/repositories/{id}/reviews`) pero no había
forma de ver la actividad de pull requests de toda la organización, que es lo que
pide la vista `/pr-reviews` de MENU-MAP §4.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(
    session: AsyncSession, *, plan_tier: str = "PRO"
) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"PR Reviews {suffix}", slug=f"prr-{suffix}", plan_tier=plan_tier
    )
    user = User(
        email=f"prr-{suffix}@example.com",
        hashed_password="not-used",
        full_name="PR Reviewer",
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


async def _repository(session: AsyncSession, organization_id: uuid.UUID) -> Repository:
    suffix = uuid.uuid4().hex
    repository = Repository(
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"9{suffix[:6]}",
        name=f"repo-{suffix[:4]}",
        full_name=f"acme/repo-{suffix[:4]}",
        clone_url=f"https://github.com/acme/repo-{suffix[:4]}.git",
        default_branch="main",
    )
    session.add(repository)
    await session.commit()
    return repository


def _review(
    repository: Repository,
    *,
    pr_number: int,
    status: PRReviewStatusEnum = PRReviewStatusEnum.PASSED,
    critical: int = 0,
    high: int = 0,
    merge_blocked: bool = False,
    days_ago: int = 1,
) -> PullRequestReview:
    return PullRequestReview(
        organization_id=repository.organization_id,
        repository_id=repository.id,
        run_id=None,
        pr_number=pr_number,
        pr_title=f"Pull request #{pr_number}",
        pr_author="dev@example.com",
        source_branch=f"feature/pr-{pr_number}",
        target_branch="main",
        commit_sha="a" * 40,
        base_sha="b" * 40,
        status=status,
        issues_caught_critical=critical,
        issues_caught_high=high,
        merge_blocked=merge_blocked,
        finished_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


# --------------------------------------------------------------------------- #
# Listado global
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_reviews_lists_across_every_repository_of_the_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    first = await _repository(integration_session, organization.id)
    second = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(first, pr_number=1, days_ago=1),
            _review(second, pr_number=2, days_ago=2),
            _review(second, pr_number=3, days_ago=3),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    # El repositorio viaja en cada fila: la tabla global no puede exigir un segundo
    # clic para saber de qué repositorio es cada PR.
    assert {item["repository_name"] for item in body["items"]} == {
        first.full_name,
        second.full_name,
    }
    # Ordenado por fecha descendente: lo más reciente primero.
    created = [item["created_at"] for item in body["items"]]
    assert created == sorted(created, reverse=True)


@pytest.mark.asyncio
async def test_pr_reviews_isolates_tenants(integration_session: AsyncSession) -> None:
    """R3: la organización A nunca ve revisiones de la B."""

    assert integration_session is not None
    alpha, alpha_headers = await _tenant(integration_session)
    beta, _beta_headers = await _tenant(integration_session)
    alpha_repository = await _repository(integration_session, alpha.id)
    beta_repository = await _repository(integration_session, beta.id)
    integration_session.add_all(
        [
            _review(alpha_repository, pr_number=10),
            _review(beta_repository, pr_number=20),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/", headers=alpha_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["repository_name"] == alpha_repository.full_name


@pytest.mark.asyncio
async def test_pr_reviews_filters_by_status_and_repository(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    first = await _repository(integration_session, organization.id)
    second = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(first, pr_number=1, status=PRReviewStatusEnum.PASSED),
            _review(first, pr_number=2, status=PRReviewStatusEnum.FAILED, merge_blocked=True),
            _review(second, pr_number=3, status=PRReviewStatusEnum.SCANNING),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        by_status = await client.get(
            "/api/v1/pr-reviews/?status=FAILED", headers=headers
        )
        by_repo = await client.get(
            f"/api/v1/pr-reviews/?repository_id={second.id}", headers=headers
        )

    assert by_status.json()["total"] == 1
    assert by_status.json()["items"][0]["pr_number"] == 2
    assert by_repo.json()["total"] == 1
    assert by_repo.json()["items"][0]["pr_number"] == 3


@pytest.mark.asyncio
async def test_pr_reviews_rejects_a_repository_from_another_tenant(
    integration_session: AsyncSession,
) -> None:
    """Un filtro por repositorio ajeno devuelve la lista vacía, no un `403` filtrando existencia."""

    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    other, _other_headers = await _tenant(integration_session)
    foreign_repository = await _repository(integration_session, other.id)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/pr-reviews/?repository_id={foreign_repository.id}", headers=headers
        )

    assert response.status_code == 200
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_pr_reviews_paginates(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [_review(repository, pr_number=number, days_ago=number) for number in range(1, 6)]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/pr-reviews/?limit=2&offset=0", headers=headers)
        second = await client.get("/api/v1/pr-reviews/?limit=2&offset=2", headers=headers)

    assert first.json()["total"] == 5
    assert len(first.json()["items"]) == 2
    assert second.json()["offset"] == 2
    first_ids = {item["id"] for item in first.json()["items"]}
    second_ids = {item["id"] for item in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


# --------------------------------------------------------------------------- #
# KPIs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_review_metrics_count_audited_clean_and_blocking(
    integration_session: AsyncSession,
) -> None:
    """Los tres KPIs de la cabecera deben sumar la misma categoría sin solaparse.

    Se cuenta por revisión, no por hallazgo: una revisión con doce hallazgos sigue
    siendo una revisión. Y una revisión con `merge_blocked` cuenta como bloqueante
    aunque su estado sea `PASSED`, porque lo que阻止 el merge es la bandera, no el
    estado del escaneo.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(repository, pr_number=1, status=PRReviewStatusEnum.PASSED),
            _review(repository, pr_number=2, status=PRReviewStatusEnum.PASSED),
            _review(
                repository,
                pr_number=3,
                status=PRReviewStatusEnum.FAILED,
                critical=2,
                high=5,
                merge_blocked=True,
            ),
            _review(
                repository,
                pr_number=4,
                status=PRReviewStatusEnum.PASSED,
                high=1,
                merge_blocked=True,
            ),
            _review(repository, pr_number=5, status=PRReviewStatusEnum.ERROR),
            _review(repository, pr_number=6, status=PRReviewStatusEnum.QUEUED),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/metrics", headers=headers)

    assert response.status_code == 200
    metrics = response.json()
    # Auditadas: todas las revisiones que el sistema llegó a(create y registró.
    assert metrics["total"] == 6
    # Limpias: pasaron sin bloquear el merge.
    assert metrics["clean"] == 2
    # Bloqueantes: con el merge bloqueado, sea cual sea el estado del escaneo.
    assert metrics["blocking"] == 2
    # El desglose de severidad cuenta hallazgos, no revisiones.
    assert metrics["issues_critical"] == 2
    assert metrics["issues_high"] == 6


@pytest.mark.asyncio
async def test_pr_review_metrics_are_tenant_scoped(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    alpha, alpha_headers = await _tenant(integration_session)
    beta, _beta_headers = await _tenant(integration_session)
    alpha_repository = await _repository(integration_session, alpha.id)
    beta_repository = await _repository(integration_session, beta.id)
    integration_session.add_all(
        [
            _review(alpha_repository, pr_number=1),
            _review(beta_repository, pr_number=2),
            _review(beta_repository, pr_number=3),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/metrics", headers=alpha_headers)

    assert response.json()["total"] == 1


# --------------------------------------------------------------------------- #
# Autenticación
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_reviews_endpoints_require_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get("/api/v1/pr-reviews/")
        metrics = await client.get("/api/v1/pr-reviews/metrics")

    assert listing.status_code == 401
    assert metrics.status_code == 401
