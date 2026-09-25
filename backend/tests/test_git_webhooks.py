import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.repositories.models import (
    GitProviderEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.router_webhooks import _verify_signature
from backend.core.crypto import decrypt_secret, encrypt_secret
from backend.core.rate_limit import get_rate_limit_redis
from backend.main import app

pytestmark = pytest.mark.integration


async def create_repository(
    session: AsyncSession,
    provider: GitProviderEnum,
) -> Repository:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Webhook {provider.value} {suffix}",
        slug=f"webhook-{provider.value.lower()}-{suffix}",
    )
    session.add(organization)
    await session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=provider,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    session.add(repository)
    await session.flush()
    return repository


def signature_headers(provider: GitProviderEnum, secret: str, body: bytes) -> dict[str, str]:
    if provider == GitProviderEnum.GITLAB:
        return {
            "X-Gitlab-Token": secret,
            "X-Gitlab-Event-UUID": f"delivery-{uuid.uuid4().hex}",
        }
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    header = {
        GitProviderEnum.GITHUB: "X-Hub-Signature-256",
        GitProviderEnum.BITBUCKET: "X-Hub-Signature",
        GitProviderEnum.GITEA: "X-Gitea-Signature",
    }[provider]
    delivery_header = {
        GitProviderEnum.GITHUB: "X-GitHub-Delivery",
        GitProviderEnum.GITLAB: "X-Gitlab-Event-UUID",
        GitProviderEnum.BITBUCKET: "X-Request-UUID",
        GitProviderEnum.GITEA: "X-Gitea-Delivery",
    }[provider]
    if provider == GitProviderEnum.GITEA:
        return {header: digest, delivery_header: f"delivery-{uuid.uuid4().hex}"}
    return {
        header: f"sha256={digest}",
        delivery_header: f"delivery-{uuid.uuid4().hex}",
    }


def webhook_body(repository: Repository, provider: GitProviderEnum) -> bytes:
    remote_id = repository.remote_repo_id
    if provider == GitProviderEnum.GITLAB:
        payload = {
            "object_kind": "merge_request",
            "project": {"id": int(remote_id)},
            "object_attributes": {"action": "open", "iid": 7},
        }
    elif provider == GitProviderEnum.BITBUCKET:
        payload = {
            "repository": {"uuid": remote_id},
            "push": {"changes": []},
        }
    elif provider == GitProviderEnum.GITEA:
        payload = {
            "repository": {"id": int(remote_id)},
            "action": "opened",
        }
    else:
        payload = {
            "repository": {"id": int(remote_id)},
            "action": "opened",
        }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(GitProviderEnum))
async def test_valid_webhook_signature_is_accepted_immediately(
    integration_session: AsyncSession,
    provider: GitProviderEnum,
) -> None:
    assert integration_session is not None
    repository = await create_repository(integration_session, provider)
    body = webhook_body(repository, provider)
    headers = signature_headers(provider, repository.webhook_secret, body)

    with patch(
        "backend.apps.repositories.router_webhooks.process_git_webhook_event.delay"
    ) as delay:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/webhooks/git/{provider.value.lower()}",
                content=body,
                headers=headers,
            )

    assert response.status_code == 202
    delay.assert_called_once()
    assert delay.call_args.kwargs["organization_id"] == str(repository.organization_id)
    assert delay.call_args.kwargs["repository_id"] == str(repository.id)


@pytest.mark.asyncio
async def test_delivery_id_replay_is_claimed_once(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await create_repository(integration_session, GitProviderEnum.GITHUB)
    body = webhook_body(repository, GitProviderEnum.GITHUB)
    headers = signature_headers(GitProviderEnum.GITHUB, repository.webhook_secret, body)
    headers["X-GitHub-Delivery"] = "delivery-replay-1"
    replay_redis = AsyncMock()
    replay_redis.set.side_effect = [True, False]
    app.dependency_overrides[get_rate_limit_redis] = lambda: replay_redis

    with patch(
        "backend.apps.repositories.router_webhooks.process_git_webhook_event.delay"
    ) as delay:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.post(
                "/api/v1/webhooks/git/github",
                content=body,
                headers=headers,
            )
            replayed_headers = dict(headers)
            replayed_headers["X-GitHub-Delivery"] = "delivery-replay-2"
            second = await client.post(
                "/api/v1/webhooks/git/github",
                content=body,
                headers=replayed_headers,
            )

    app.dependency_overrides.pop(get_rate_limit_redis, None)
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    delay.assert_called_once()


@pytest.mark.asyncio
async def test_github_webhook_rejects_tampered_signature(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await create_repository(integration_session, GitProviderEnum.GITHUB)
    body = webhook_body(repository, GitProviderEnum.GITHUB)
    headers = signature_headers(
        GitProviderEnum.GITHUB,
        repository.webhook_secret,
        body + b"tampered",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/git/github",
            content=body,
            headers=headers,
        )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}


@pytest.mark.asyncio
async def test_webhook_publish_failure_releases_delivery_reservation(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await create_repository(integration_session, GitProviderEnum.GITHUB)
    body = webhook_body(repository, GitProviderEnum.GITHUB)
    headers = signature_headers(GitProviderEnum.GITHUB, repository.webhook_secret, body)
    headers["X-GitHub-Delivery"] = "delivery-release-1"
    replay_redis = AsyncMock()
    replay_redis.set.return_value = True
    app.dependency_overrides[get_rate_limit_redis] = lambda: replay_redis

    with patch(
        "backend.apps.repositories.router_webhooks.process_git_webhook_event.delay",
        side_effect=RuntimeError("broker unavailable"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/webhooks/git/github",
                content=body,
                headers=headers,
            )

    app.dependency_overrides.pop(get_rate_limit_redis, None)
    assert response.status_code == 503
    replay_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_without_delivery_id_uses_body_fallback(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await create_repository(integration_session, GitProviderEnum.GITHUB)
    body = webhook_body(repository, GitProviderEnum.GITHUB)
    headers = signature_headers(GitProviderEnum.GITHUB, repository.webhook_secret, body)
    headers.pop("X-GitHub-Delivery")

    with patch(
        "backend.apps.repositories.router_webhooks.process_git_webhook_event.delay"
    ) as delay:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/webhooks/git/github",
                content=body,
                headers=headers,
            )

    assert response.status_code == 202
    delay.assert_called_once()


def test_non_ascii_signature_is_rejected_without_server_error() -> None:
    assert not _verify_signature(GitProviderEnum.GITEA, "secret", b"{}", "\xe9")


@pytest.mark.asyncio
async def test_webhook_without_signature_is_rejected(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await create_repository(integration_session, GitProviderEnum.GITHUB)
    body = webhook_body(repository, GitProviderEnum.GITHUB)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/git/github",
            content=body,
        )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}


@pytest.mark.asyncio
async def test_git_credentials_are_isolated_by_organization(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    from backend.apps.repositories.models import GitCredential

    organization_a = Organization(name="Git A", slug=f"git-a-{uuid.uuid4().hex}")
    organization_b = Organization(name="Git B", slug=f"git-b-{uuid.uuid4().hex}")
    integration_session.add_all([organization_a, organization_b])
    await integration_session.flush()
    credential_a = GitCredential(
        organization_id=organization_a.id,
        provider=GitProviderEnum.GITHUB,
        encrypted_access_token=encrypt_secret(
            "token-a",
            organization_id=str(organization_a.id),
            provider=GitProviderEnum.GITHUB.value,
        ),
    )
    credential_b = GitCredential(
        organization_id=organization_b.id,
        provider=GitProviderEnum.GITLAB,
        encrypted_access_token=encrypt_secret(
            "token-b",
            organization_id=str(organization_b.id),
            provider=GitProviderEnum.GITLAB.value,
        ),
    )
    integration_session.add_all([credential_a, credential_b])
    await integration_session.flush()

    assert decrypt_secret(
        credential_a.encrypted_access_token,
        organization_id=str(organization_a.id),
        provider=GitProviderEnum.GITHUB.value,
    ) == "token-a"
    assert decrypt_secret(
        credential_b.encrypted_access_token,
        organization_id=str(organization_b.id),
        provider=GitProviderEnum.GITLAB.value,
    ) == "token-b"
    assert credential_a.organization_id != credential_b.organization_id

    repository_b = Repository(
        organization_id=organization_b.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="isolated",
        full_name="other/isolated",
        clone_url="https://github.com/other/isolated.git",
    )
    integration_session.add(repository_b)
    await integration_session.flush()
    from backend.apps.repositories.services import (
        GitCredentialNotFoundError,
        build_client_for_repository,
    )

    with pytest.raises(GitCredentialNotFoundError):
        await build_client_for_repository(integration_session, repository_b)


@pytest.mark.asyncio
async def test_pull_request_review_rejects_cross_tenant_repository(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository_a = await create_repository(integration_session, GitProviderEnum.GITHUB)
    repository_b = await create_repository(integration_session, GitProviderEnum.GITHUB)
    review = PullRequestReview(
        organization_id=repository_a.organization_id,
        repository_id=repository_b.id,
        pr_number=1,
        pr_title="cross tenant",
        pr_author="author",
        source_branch="feature",
        target_branch="main",
        commit_sha="a" * 40,
    )

    with pytest.raises(IntegrityError):
        async with integration_session.begin_nested():
            integration_session.add(review)
            await integration_session.flush()

    repository_a.organization_id = repository_b.organization_id
    with pytest.raises(DBAPIError):
        async with integration_session.begin_nested():
            await integration_session.flush()
