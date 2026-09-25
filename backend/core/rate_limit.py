"""Rate limiting distribuido para endpoints de autenticación."""

import hashlib
from dataclasses import dataclass
from inspect import isawaitable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.apps.organizations.models import User
from backend.apps.organizations.schemas import (
    EmailResendRequest,
    EmailVerificationRequest,
    InvitationAcceptRequest,
    LoginRequest,
)
from backend.core.config import settings
from backend.core.middleware import TenantContext, get_current_tenant, get_current_user
from backend.core.redis import redis_client

_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Resultado de una ventana de rate limiting."""

    count: int
    retry_after: int


def rate_limit_key(scope: str, identifier: str) -> str:
    """Construye una clave Redis determinista sin exponer la IP en texto plano."""

    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return f"fenix:rate-limit:{scope}:{digest}"


def _request_identifier(request: Request) -> str:
    if request.client is None:
        return "unknown-client"
    return request.client.host


async def _apply_rate_limit(
    client: Redis,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int,
) -> RateLimitResult:
    key = rate_limit_key(scope, identifier)
    try:
        count_result = client.eval(_RATE_LIMIT_SCRIPT, 1, key, str(window_seconds))
        if isawaitable(count_result):
            count_result = await count_result
        count = int(count_result)

        ttl_result = client.ttl(key)
        if isawaitable(ttl_result):
            ttl_result = await ttl_result
        ttl = int(ttl_result)
    except (RedisError, OSError, TimeoutError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio de autenticación temporalmente no disponible",
        ) from error

    retry_after = window_seconds if ttl is None or ttl < 0 else max(1, ttl)
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Límite de intentos excedido",
            headers={"Retry-After": str(retry_after)},
        )
    return RateLimitResult(count=count, retry_after=retry_after)


async def get_rate_limit_redis() -> Redis:
    """Entrega el cliente Redis compartido para el rate limiter."""

    return redis_client


RedisDependency = Annotated[Redis, Depends(get_rate_limit_redis)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


async def enforce_login_rate_limit(
    request: Request,
    payload: LoginRequest,
    client: RedisDependency,
) -> None:
    """Limita los intentos de login por IP y por cuenta normalizada."""

    await _apply_rate_limit(
        client,
        "login-ip",
        _request_identifier(request),
        settings.auth_login_rate_limit,
        settings.auth_login_rate_window_seconds,
    )
    await _apply_rate_limit(
        client,
        "login-account",
        str(payload.email).strip().lower(),
        settings.auth_login_rate_limit,
        settings.auth_login_rate_window_seconds,
    )


async def enforce_register_rate_limit(
    request: Request,
    client: RedisDependency,
) -> None:
    """Limita los registros por IP y ventana configurada."""

    await _apply_rate_limit(
        client,
        "register",
        _request_identifier(request),
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )


async def enforce_create_organization_rate_limit(
    current_user: CurrentUserDependency,
    client: RedisDependency,
) -> None:
    """Limita la creación de workspaces por usuario autenticado."""

    await _apply_rate_limit(
        client,
        "organization-create",
        str(current_user.id),
        settings.organization_create_rate_limit,
        settings.organization_create_rate_window_seconds,
    )


async def enforce_email_verification_rate_limit(
    request: Request,
    payload: EmailVerificationRequest,
    client: RedisDependency,
) -> None:
    """Limita la verificación por IP y por token, sin almacenar el token en Redis."""

    await _apply_rate_limit(
        client,
        "email-verification-ip",
        _request_identifier(request),
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )
    await _apply_rate_limit(
        client,
        "email-verification-token",
        payload.token,
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )


async def enforce_email_resend_rate_limit(
    request: Request,
    payload: EmailResendRequest,
    client: RedisDependency,
) -> None:
    """Limita el reenvío por IP y por email sin revelar la cuenta."""

    await _apply_rate_limit(
        client,
        "email-resend-ip",
        _request_identifier(request),
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )
    await _apply_rate_limit(
        client,
        "email-resend-account",
        str(payload.email).strip().lower(),
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )


async def enforce_invitation_accept_rate_limit(
    request: Request,
    payload: InvitationAcceptRequest,
    client: RedisDependency,
) -> None:
    """Limita intentos de adivinar tokens de invitación por IP y token."""

    await _apply_rate_limit(
        client,
        "invitation-accept-ip",
        _request_identifier(request),
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )
    await _apply_rate_limit(
        client,
        "invitation-accept-token",
        payload.token,
        settings.auth_register_rate_limit,
        settings.auth_register_rate_window_seconds,
    )


async def enforce_autofix_rate_limit(
    tenant: TenantDependency,
    client: RedisDependency,
) -> None:
    """Limita la creación de ramas de autofix por usuario y organización."""

    await _apply_rate_limit(
        client,
        "autofix-create",
        f"{tenant.user.id}:{tenant.organization.id}",
        settings.autofix_create_rate_limit,
        settings.autofix_create_rate_window_seconds,
    )


async def enforce_git_webhook_rate_limit(
    request: Request,
    client: RedisDependency,
) -> None:
    """Limita webhooks públicos por IP antes de validar o consultar la BD."""

    await _apply_rate_limit(
        client,
        "git-webhook",
        _request_identifier(request),
        settings.git_webhook_rate_limit,
        settings.git_webhook_rate_window_seconds,
    )


async def enforce_pentest_rate_limit(
    tenant: TenantDependency,
    client: RedisDependency,
) -> None:
    """Limita la creación de pentests por usuario y organización."""

    await _apply_rate_limit(
        client,
        "pentest-create",
        f"{tenant.user.id}:{tenant.organization.id}",
        settings.pentest_create_rate_limit,
        settings.pentest_create_rate_window_seconds,
    )


async def enforce_invitation_rate_limit(
    tenant: TenantDependency,
    client: RedisDependency,
) -> None:
    """Limita la creación de invitaciones por usuario y tenant."""

    await _apply_rate_limit(
        client,
        "invitation",
        f"{tenant.user.id}:{tenant.organization.id}",
        settings.invitation_rate_limit,
        settings.invitation_rate_window_seconds,
    )
