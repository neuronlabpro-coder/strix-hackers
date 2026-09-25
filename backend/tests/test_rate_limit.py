from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from redis.asyncio import Redis
from starlette.requests import Request

from backend.apps.organizations.schemas import (
    EmailResendRequest,
    InvitationAcceptRequest,
    LoginRequest,
)
from backend.core.config import settings
from backend.core.rate_limit import (
    enforce_email_resend_rate_limit,
    enforce_invitation_accept_rate_limit,
    enforce_login_rate_limit,
    enforce_register_rate_limit,
    rate_limit_key,
)


def make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("198.51.100.25", 4312),
        }
    )


@pytest.mark.asyncio
async def test_rate_limit_key_is_stable_and_does_not_expose_ip() -> None:
    key = rate_limit_key("login", "198.51.100.25")

    assert key == rate_limit_key("login", "198.51.100.25")
    assert "198.51.100.25" not in key
    assert key.startswith("fenix:rate-limit:login:")


@pytest.mark.asyncio
async def test_login_rate_limit_returns_429_with_retry_after() -> None:
    redis = AsyncMock(spec=Redis)
    redis.eval.return_value = settings.auth_login_rate_limit + 1
    redis.ttl.return_value = 42

    with pytest.raises(HTTPException) as error:
        await enforce_login_rate_limit(
            make_request(),
            LoginRequest(email="user@example.com", password="contraseña-de-prueba-123"),
            redis,
        )

    assert error.value.status_code == 429
    assert error.value.headers == {"Retry-After": "42"}


@pytest.mark.asyncio
async def test_login_rate_limit_uses_ip_and_account_buckets() -> None:
    redis = AsyncMock(spec=Redis)
    redis.eval.side_effect = [settings.auth_login_rate_limit, settings.auth_login_rate_limit]
    redis.ttl.side_effect = [60, 60]

    await enforce_login_rate_limit(
        make_request(),
        LoginRequest(email="User@Example.COM", password="contraseña-de-prueba-123"),
        redis,
    )

    assert redis.eval.call_count == 2
    assert redis.ttl.call_count == 2


@pytest.mark.asyncio
async def test_email_resend_rate_limit_uses_email_bucket() -> None:
    redis = AsyncMock(spec=Redis)
    redis.eval.side_effect = [settings.auth_register_rate_limit, settings.auth_register_rate_limit]
    redis.ttl.side_effect = [60, 60]

    await enforce_email_resend_rate_limit(
        make_request(),
        EmailResendRequest(email="user@example.com"),
        redis,
    )

    assert redis.eval.call_count == 2
    assert redis.ttl.call_count == 2


@pytest.mark.asyncio
async def test_invitation_acceptance_rate_limit_uses_token_bucket() -> None:
    redis = AsyncMock(spec=Redis)
    redis.eval.side_effect = [settings.auth_register_rate_limit, settings.auth_register_rate_limit]
    redis.ttl.side_effect = [60, 60]

    await enforce_invitation_accept_rate_limit(
        make_request(),
        InvitationAcceptRequest(token="a" * 32),
        redis,
    )

    assert redis.eval.call_count == 2
    assert redis.ttl.call_count == 2


@pytest.mark.asyncio
async def test_register_rate_limit_allows_requests_below_limit() -> None:
    redis = AsyncMock(spec=Redis)
    redis.eval.return_value = settings.auth_register_rate_limit
    redis.ttl.return_value = 60

    await enforce_register_rate_limit(make_request(), redis)

    redis.eval.assert_called_once()
    redis.ttl.assert_called_once()
