import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.clients.base import GitClientError
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.core.crypto import encrypt_secret
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


class _FakeGitClient:
    def __init__(self) -> None:
        self.created_webhooks: list[tuple[str, str, tuple[str, ...]]] = []
        self.deleted_webhooks: list[tuple[str, str]] = []

    def list_repositories(self) -> list[dict[str, object]]:
        return [
            {
                "id": 101,
                "name": "app",
                "full_name": "acme/app",
                "clone_url": "https://github.com/acme/app.git",
                "default_branch": "main",
                "private": True,
            }
        ]

    def get_repository(self, remote_repo_id: str) -> dict[str, object]:
        assert remote_repo_id == "101"
        return self.list_repositories()[0]

    def create_webhook(
        self,
        repo_full_name: str,
        callback_url: str,
        secret: str,
        events: tuple[str, ...],
    ) -> str:
        del secret
        self.created_webhooks.append((repo_full_name, callback_url, events))
        return "webhook-101"

    def delete_webhook(self, repo_full_name: str, webhook_id: str) -> None:
        self.deleted_webhooks.append((repo_full_name, webhook_id))

    def close(self) -> None:
        return None


class _FailingWebhookClient(_FakeGitClient):
    """Cliente simulado donde el proveedor rechaza el registro del webhook."""

    def create_webhook(
        self,
        repo_full_name: str,
        callback_url: str,
        secret: str,
        events: tuple[str, ...],
    ) -> str:
        del repo_full_name, callback_url, secret, events
        raise GitClientError("El proveedor Git rechazó la operación", status_code=403)


async def _create_tenant(
    session: AsyncSession,
    *,
    role: RoleEnum = RoleEnum.ADMIN,
) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Repositories {suffix}",
        slug=f"repositories-{suffix}",
    )
    user = User(
        email=f"repositories-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Repository Admin",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=role,
        )
    )
    credential = GitCredential(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        encrypted_access_token=encrypt_secret(
            "github-token",
            organization_id=str(organization.id),
            provider=GitProviderEnum.GITHUB.value,
        ),
    )
    session.add(credential)
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


@pytest.mark.asyncio
async def test_remote_connect_list_patch_and_delete_repository_flow(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _create_tenant(integration_session)
    fake_client = _FakeGitClient()
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            remote = await client.get(
                "/api/v1/repositories/remote?provider=GITHUB",
                headers=headers,
            )
            assert remote.status_code == 200
            assert remote.json()["items"][0]["full_name"] == "acme/app"

            connected = await client.post(
                "/api/v1/repositories/connect",
                headers=headers,
                json={
                    "provider": "GITHUB",
                    "remote_repo_id": "101",
                    "pr_reviews_enabled": True,
                },
            )
            assert connected.status_code == 201
            repository_payload = connected.json()["repository"]
            repository_id = uuid.UUID(repository_payload["id"])
            assert connected.json()["webhook_registered"] is True
            assert "webhook_secret" not in repository_payload
            assert fake_client.created_webhooks == [
                (
                    "acme/app",
                    "http://localhost:8000/api/v1/webhooks/git/github",
                    ("pull_request", "issue_comment"),
                )
            ]

            listed = await client.get("/api/v1/repositories/", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["total"] == 1
            assert listed.json()["items"][0]["webhook_registered"] is True

            updated = await client.patch(
                f"/api/v1/repositories/{repository_id}",
                headers=headers,
                json={"pr_reviews_enabled": False, "default_branch": "develop"},
            )
            assert updated.status_code == 200
            assert updated.json()["pr_reviews_enabled"] is False
            assert updated.json()["default_branch"] == "develop"

            deleted = await client.delete(
                f"/api/v1/repositories/{repository_id}",
                headers=headers,
            )
            assert deleted.status_code == 204
            assert fake_client.deleted_webhooks == [("acme/app", "webhook-101")]

    remaining = await integration_session.execute(
        select(Repository).where(Repository.id == repository_id)
    )
    assert remaining.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_repositories_are_strictly_isolated_between_tenants(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization_a, headers_a = await _create_tenant(integration_session)
    organization_b, _headers_b = await _create_tenant(integration_session)
    repository_b = Repository(
        organization_id=organization_b.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="202",
        name="private-beta",
        full_name="beta/private",
        clone_url="https://github.com/beta/private.git",
    )
    integration_session.add(repository_b)
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/v1/repositories/", headers=headers_a)
        assert listed.status_code == 200
        assert listed.json()["total"] == 0

        patched = await client.patch(
            f"/api/v1/repositories/{repository_b.id}",
            headers=headers_a,
            json={"pr_reviews_enabled": False},
        )
        assert patched.status_code == 404

        deleted = await client.delete(
            f"/api/v1/repositories/{repository_b.id}",
            headers=headers_a,
        )
        assert deleted.status_code == 404

    await integration_session.refresh(repository_b)
    assert repository_b.organization_id == organization_b.id
    assert repository_b.is_active is True
    assert organization_a.id != organization_b.id


@pytest.mark.asyncio
async def test_repository_management_requires_admin_role(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _create_tenant(
        integration_session,
        role=RoleEnum.MEMBER,
    )
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_FakeGitClient()),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            connected = await client.post(
                "/api/v1/repositories/connect",
                headers=headers,
                json={"provider": "GITHUB", "remote_repo_id": "101"},
            )
            assert connected.status_code == 403

    repository_count = await integration_session.execute(
        select(func.count()).select_from(Repository)
    )
    assert int(repository_count.scalar_one()) == 0


@pytest.mark.asyncio
async def test_connect_persists_repository_even_if_webhook_registration_fails(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _create_tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_FailingWebhookClient()),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            connected = await client.post(
                "/api/v1/repositories/connect",
                headers=headers,
                json={"provider": "GITHUB", "remote_repo_id": "101"},
            )
            remote = await client.get(
                "/api/v1/repositories/remote?provider=GITHUB",
                headers=headers,
            )
    assert remote.status_code == 200
    assert remote.json()["items"][0]["already_connected"] is True
    assert connected.status_code == 201
    payload = connected.json()
    assert payload["webhook_registered"] is False
    assert payload["repository"]["webhook_registered"] is False

    repository_result = await integration_session.execute(
        select(Repository).where(
            Repository.id == uuid.UUID(payload["repository"]["id"]),
        )
    )
    repository = repository_result.scalar_one()
    assert repository.webhook_id is None
    assert len(repository.webhook_secret) >= 32
    assert "webhook_secret" not in payload["repository"]


@pytest.mark.asyncio
async def test_remote_inventory_requires_connected_credential(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Sin Cred {suffix}", slug=f"sin-cred-{suffix}")
    user = User(
        email=f"sin-cred-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Sin Credencial",
        email_verified=True,
    )
    integration_session.add_all([organization, user])
    await integration_session.flush()
    integration_session.add(
        Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=RoleEnum.ADMIN,
        )
    )
    await integration_session.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/repositories/remote?provider=GITHUB",
            headers=headers,
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_unsupported_providers_are_reported_as_not_implemented(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _create_tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/repositories/remote?provider=BITBUCKET",
            headers=headers,
        )
    assert response.status_code == 501


@pytest.mark.asyncio
async def test_delete_is_blocked_while_reviews_are_in_progress(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _create_tenant(integration_session)
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="303",
        name="busy",
        full_name="acme/busy",
        clone_url="https://github.com/acme/busy.git",
        webhook_id="webhook-303",
    )
    integration_session.add(repository)
    await integration_session.flush()
    integration_session.add(
        PullRequestReview(
            organization_id=organization.id,
            repository_id=repository.id,
            pr_number=7,
            pr_title="Cambio sensible",
            pr_author="octocat",
            source_branch="feature/one",
            target_branch="main",
            commit_sha="a" * 40,
            status=PRReviewStatusEnum.SCANNING,
        )
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_FakeGitClient()),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            deleted = await client.delete(
                f"/api/v1/repositories/{repository.id}",
                headers=headers,
            )
    assert deleted.status_code == 409

    remaining = await integration_session.execute(
        select(Repository).where(Repository.id == repository.id)
    )
    assert remaining.scalar_one_or_none() is not None
