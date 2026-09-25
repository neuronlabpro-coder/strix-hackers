import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.apps.repositories.oauth import (
    OAuthProviderSettings,
    OAuthStateError,
    OAuthToken,
    create_oauth_state,
    decode_oauth_state,
)
from backend.apps.repositories.router_auth import get_oauth_provider_settings
from backend.core.crypto import decrypt_secret
from backend.core.rate_limit import get_rate_limit_redis
from backend.core.security import create_access_token
from backend.main import app


class _OAuthRedis:
    """Redis mínimo que emula set-nx y getdel para el state de un solo uso."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None:
        del ex
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)


def test_oauth_state_is_signed_bound_and_expires() -> None:
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    state = create_oauth_state(
        GitProviderEnum.GITHUB,
        organization_id,
        user_id,
        now=now,
    )

    payload = decode_oauth_state(state, GitProviderEnum.GITHUB, now=now)
    assert payload.organization_id == organization_id
    assert payload.user_id == user_id
    assert payload.provider == GitProviderEnum.GITHUB

    with pytest.raises(OAuthStateError):
        decode_oauth_state(state + "x", GitProviderEnum.GITHUB, now=now)
    with pytest.raises(OAuthStateError):
        decode_oauth_state(state, GitProviderEnum.GITLAB, now=now)
    with pytest.raises(OAuthStateError):
        decode_oauth_state(
            state,
            GitProviderEnum.GITHUB,
            now=now + timedelta(minutes=11),
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oauth_callback_stores_encrypted_token_and_state_is_single_use(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"OAuth {suffix}", slug=f"oauth-{suffix}")
    user = User(
        email=f"oauth-{suffix}@example.com",
        hashed_password="not-used",
        full_name="OAuth User",
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

    redis = _OAuthRedis()
    app.dependency_overrides[get_rate_limit_redis] = lambda: redis

    def _fake_oauth_settings(provider: str) -> OAuthProviderSettings:
        del provider
        return OAuthProviderSettings(
            provider=GitProviderEnum.GITHUB,
            client_id="client-id",
            client_secret="client-secret",
            authorize_url="https://github.example.com/login/oauth/authorize",
            token_url="https://github.example.com/login/oauth/access_token",
            scopes=("repo", "admin:repo_hook"),
        )

    app.dependency_overrides[get_oauth_provider_settings] = _fake_oauth_settings
    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            authorize = await client.get(
                "/api/v1/repositories/oauth/github/authorize",
                headers=headers,
            )
            assert authorize.status_code == 302
            state = parse_qs(urlsplit(authorize.headers["location"]).query)["state"][0]

            with patch(
                "backend.apps.repositories.router_auth.exchange_oauth_code",
                new=AsyncMock(
                    return_value=OAuthToken(
                        access_token="github-access-token",
                        refresh_token="github-refresh-token",
                        expires_in=3600,
                    )
                ),
            ):
                callback = await client.get(
                    "/api/v1/repositories/oauth/github/callback",
                    params={"state": state, "code": "temporary-code"},
                )
                replay = await client.get(
                    "/api/v1/repositories/oauth/github/callback",
                    params={"state": state, "code": "temporary-code"},
                )
    finally:
        app.dependency_overrides.pop(get_rate_limit_redis, None)
        app.dependency_overrides.pop(get_oauth_provider_settings, None)

    assert callback.status_code == 303
    assert callback.headers["location"].endswith("/repositories?connected=GITHUB")
    assert "github-access-token" not in callback.headers["location"]
    assert replay.status_code == 400

    credential_result = await integration_session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == organization.id,
            GitCredential.provider == GitProviderEnum.GITHUB,
        )
    )
    credential = credential_result.scalar_one()
    assert "github-access-token" not in credential.encrypted_access_token
    assert (
        decrypt_secret(
            credential.encrypted_access_token,
            organization_id=str(organization.id),
            provider=GitProviderEnum.GITHUB.value,
        )
        == "github-access-token"
    )
    assert credential.token_expires_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oauth_callback_rejects_tampered_state_without_touching_credential(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"OAuth Neg {suffix}", slug=f"oauth-neg-{suffix}")
    user = User(
        email=f"oauth-neg-{suffix}@example.com",
        hashed_password="not-used",
        full_name="OAuth Negativo",
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

    state = create_oauth_state(GitProviderEnum.GITHUB, organization.id, user.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/repositories/oauth/github/callback",
            params={"state": f"{state[:-1]}0", "code": "temporary-code"},
        )
    assert response.status_code == 400

    credential_result = await integration_session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == organization.id,
            GitCredential.provider == GitProviderEnum.GITHUB,
        )
    )
    assert credential_result.scalar_one_or_none() is None
