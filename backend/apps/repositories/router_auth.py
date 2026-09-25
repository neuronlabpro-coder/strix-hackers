"""Rutas OAuth para conectar credenciales Git de GitHub y GitLab."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.apps.repositories.oauth import (
    OAuthConfigurationError,
    OAuthError,
    OAuthExchangeError,
    OAuthProviderSettings,
    OAuthStateError,
    build_authorization_url,
    create_oauth_state,
    decode_oauth_state,
    exchange_oauth_code,
    oauth_state_key,
    validate_oauth_provider_settings,
)
from backend.apps.repositories.oauth import (
    get_oauth_provider_settings as load_oauth_provider_settings,
)
from backend.apps.repositories.services import encrypt_organization_credential
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant
from backend.core.rate_limit import (
    RedisDependency,
    enforce_oauth_callback_rate_limit,
    enforce_repository_management_rate_limit,
)

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


def _provider_from_path(provider: str) -> GitProviderEnum:
    try:
        selected = GitProviderEnum(provider.upper())
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proveedor OAuth no soportado",
        ) from error
    if selected not in {GitProviderEnum.GITHUB, GitProviderEnum.GITLAB}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proveedor OAuth no soportado",
        )
    return selected


def get_oauth_provider_settings(provider: Annotated[str, Path()]) -> OAuthProviderSettings:
    """Dependency reemplazable que resuelve la configuración del path OAuth."""

    return load_oauth_provider_settings(_provider_from_path(provider))


OAuthSettingsDependency = Annotated[OAuthProviderSettings, Depends(get_oauth_provider_settings)]


def _oauth_callback_url(provider: GitProviderEnum) -> str:
    return (
        f"{settings.api_public_base_url.rstrip('/')}/api/v1/repositories/oauth/"
        f"{provider.value.lower()}/callback"
    )


def _frontend_redirect(provider: GitProviderEnum, **values: str) -> RedirectResponse:
    query = urlencode({"connected": provider.value, **values})
    return RedirectResponse(
        url=f"{settings.frontend_base_url.rstrip('/')}/repositories?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _validate_state_subject(
    session: AsyncSession,
    organization_id: UUID,
    user_id: UUID,
) -> None:
    result = await session.execute(
        select(Membership)
        .join(Organization, Organization.id == Membership.organization_id)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
            User.is_active.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        raise OAuthStateError("El state ya no pertenece a una organización activa")


async def _store_state(redis_client: Redis, state: str, nonce: str) -> None:
    key = oauth_state_key(state)
    try:
        for _attempt in range(3):
            stored = await redis_client.set(
                key,
                nonce,
                ex=settings.oauth_state_ttl_seconds,
                nx=True,
            )
            if stored:
                return
    except (RedisError, OSError, TimeoutError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo iniciar el flujo OAuth",
            headers={"Retry-After": "10"},
        ) from error
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="No se pudo iniciar el flujo OAuth",
        headers={"Retry-After": "10"},
    )


@router.get(
    "/api/v1/repositories/oauth/{provider}/authorize",
    dependencies=[Depends(enforce_repository_management_rate_limit)],
)
async def authorize_git_provider(
    provider: str,
    tenant: TenantDependency,
    provider_settings: OAuthSettingsDependency,
    redis_client: RedisDependency,
) -> RedirectResponse:
    """Genera un state firmado y de un solo uso antes de redirigir al proveedor."""

    if tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador",
        )
    selected = _provider_from_path(provider)
    if provider_settings.provider != selected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La configuración OAuth no corresponde al proveedor",
        )
    try:
        validate_oauth_provider_settings(provider_settings)
    except OAuthConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El proveedor OAuth no está configurado",
        ) from error
    state = create_oauth_state(
        selected,
        tenant.organization.id,
        tenant.user.id,
    )
    state_payload = decode_oauth_state(state, selected)
    await _store_state(redis_client, state, state_payload.nonce)
    return RedirectResponse(
        url=build_authorization_url(
            provider_settings,
            state,
            _oauth_callback_url(selected),
        ),
        status_code=status.HTTP_302_FOUND,
    )


@router.get(
    "/api/v1/repositories/oauth/{provider}/callback",
    dependencies=[Depends(enforce_oauth_callback_rate_limit)],
)
async def git_oauth_callback(
    provider: str,
    state: Annotated[str, Query(min_length=16, max_length=2048)],
    provider_settings: OAuthSettingsDependency,
    redis_client: RedisDependency,
    session: SessionDependency,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=128)] = None,
) -> RedirectResponse:
    """Consume state, canjea el código y persiste solo ciphertext AES-GCM."""

    selected = _provider_from_path(provider)
    if provider_settings.provider != selected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La configuración OAuth no corresponde al proveedor",
        )
    try:
        state_payload = decode_oauth_state(state, selected)
    except OAuthStateError as state_error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="State OAuth inválido o expirado",
        ) from state_error
    try:
        stored_nonce = await redis_client.getdel(oauth_state_key(state))
    except (RedisError, OSError, TimeoutError) as redis_error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo validar el state OAuth",
            headers={"Retry-After": "10"},
        ) from redis_error
    if not stored_nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="State OAuth inválido o expirado",
        )
    if isinstance(stored_nonce, bytes):
        stored_nonce = stored_nonce.decode("utf-8", errors="ignore")
    if stored_nonce != state_payload.nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="State OAuth inválido o expirado",
        )
    try:
        await _validate_state_subject(
            session,
            state_payload.organization_id,
            state_payload.user_id,
        )
    except OAuthStateError as state_error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="State OAuth no autorizado",
        ) from state_error
    if error is not None:
        return _frontend_redirect(selected, connection="denied")
    if code is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta el código OAuth",
        )
    try:
        token = await exchange_oauth_code(
            provider_settings,
            code,
            _oauth_callback_url(selected),
        )
    except (OAuthConfigurationError, OAuthExchangeError) as exchange_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El proveedor no completó la conexión OAuth",
        ) from exchange_error
    except OAuthError as oauth_error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El flujo OAuth no es válido",
        ) from oauth_error

    encrypted_access_token = encrypt_organization_credential(
        token.access_token,
        state_payload.organization_id,
        selected,
    )
    encrypted_refresh_token = (
        encrypt_organization_credential(
            token.refresh_token,
            state_payload.organization_id,
            selected,
            field="refresh",
        )
        if token.refresh_token
        else None
    )
    result = await session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == state_payload.organization_id,
            GitCredential.provider == selected,
        )
    )
    credential = result.scalar_one_or_none()
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=token.expires_in)
        if token.expires_in is not None
        else None
    )
    if credential is None:
        credential = GitCredential(
            organization_id=state_payload.organization_id,
            provider=selected,
            encrypted_access_token=encrypted_access_token,
            encrypted_refresh_token=encrypted_refresh_token,
            token_expires_at=expires_at,
        )
        session.add(credential)
    else:
        credential.encrypted_access_token = encrypted_access_token
        credential.encrypted_refresh_token = encrypted_refresh_token
        credential.token_expires_at = expires_at
    await session.commit()
    return _frontend_redirect(selected)
