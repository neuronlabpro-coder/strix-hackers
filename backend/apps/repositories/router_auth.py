"""Rutas OAuth para conectar credenciales Git de GitHub y GitLab."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.clients.base import GitClientError, GitUserIdentity
from backend.apps.repositories.clients.factory import (
    UnsupportedGitProviderError,
    build_client_for_token,
)
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.apps.repositories.oauth import (
    OAuthAuthorizeResponse,
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
from backend.apps.repositories.schemas import (
    PersonalTokenConnectRequest,
    PersonalTokenConnectResponse,
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


def _prefers_json(request: Request) -> bool:
    """Detecta clientes XHR que necesitan la URL de consentimiento en el cuerpo."""

    return "application/json" in request.headers.get("accept", "")


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
    response_model=None,
)
async def authorize_git_provider(
    request: Request,
    provider: str,
    tenant: TenantDependency,
    provider_settings: OAuthSettingsDependency,
    redis_client: RedisDependency,
) -> Response:
    """Genera un state firmado y de un solo uso antes de redirigir al proveedor.

    Una navegación directa del navegador recibe un `302`, pero ese flujo no puede
    portar el encabezado `Authorization`. Por eso los clientes XHR del panel piden
    `Accept: application/json` y reciben la URL de consentimiento en el cuerpo, con
    la misma semántica de state, expiración y rate limit.
    """

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
    authorization_url = build_authorization_url(
        provider_settings,
        state,
        _oauth_callback_url(selected),
    )
    if _prefers_json(request):
        payload = OAuthAuthorizeResponse(
            authorization_url=authorization_url,
            expires_in=settings.oauth_state_ttl_seconds,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "authorization_url": payload.authorization_url,
                "expires_in": payload.expires_in,
            },
        )
    return RedirectResponse(
        url=authorization_url,
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
) -> Response:
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


def verify_provider_token(
    token: str,
    provider: GitProviderEnum,
    organization_id: UUID,
) -> GitUserIdentity:
    """Consulta al proveedor quién es el dueño del token.

    Se ejecuta en un hilo porque el cliente Git es síncrono sobre `httpx.Client`. Es un
    módulo a nivel de nombre y no una clase por un motivo concreto: es el punto donde
    las pruebas sustituyen la red, y un módulo se puede parchear entero sin tener que
    saber qué clase interna lo envuelve.
    """

    client = build_client_for_token(provider, token, organization_id)
    try:
        return client.get_authenticated_user()
    finally:
        # El cierre va en `finally` a propósito: el cliente sostiene un socket TLS y un
        # fallo de red no lo libera por sí solo. Sin esto, cada PAT verificado filtraría
        # una conexión del pool del sistema.
        client.close()


@router.post(
    "/api/v1/repositories/credentials/token",
    response_model=PersonalTokenConnectResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_repository_management_rate_limit)],
)
async def connect_personal_token(
    payload: PersonalTokenConnectRequest,
    tenant: TenantDependency,
    session: SessionDependency,
) -> PersonalTokenConnectResponse:
    """Conecta una credencial enviando un Token Personal de acceso.

    El flujo OAuth de arriba cubre el caso normal de producción, pero exige registrar
    una OAuth App en GitHub o GitLab: un trámite manual, fuera del producto, que
    bloquea el trabajo en local. Esta ruta existe para eso, y como efecto secundario
    evita que el primer uso de la plataforma dependa de tener una app registrada en dos
    sitios externos.

    ## Por qué se verifica antes de cifrar

    Un PAT no caduca por sí solo: sigue teniendo el formato correcto mucho después de
    que el usuario lo haya revocado en GitHub. Si se aceptara sin comprobarlo, el
    fallo aparecería en mitad de una sincronización, con el panel mostrando un error
    que no señala la causa real. Verificar en el borde cuesta una llamada y convierte
    un diagnóstico tardío en un mensaje inmediato.

    ## Por qué sustituir en lugar de acumular

    `git_credentials` es única por `(organization_id, provider)`, igual que en el flujo
    OAuth: mantener dos credenciales vivas obligaría a elegir cuál usar en cada
    sincronización, y esa elección no está en ninguna parte de la interfaz.
    """

    if tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador para gestionar credenciales",
        )

    try:
        identity = await asyncio.to_thread(
            verify_provider_token,
            payload.token,
            payload.provider,
            tenant.organization.id,
        )
    except UnsupportedGitProviderError as error:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="El proveedor todavía no tiene conector de gestión",
        ) from error
    except GitClientError as error:
        # El mensaje del cliente ya viene saneado: nunca incluye el token. Se traduce
        # a `400` y no a `502` porque la causa es la credencial, no el proveedor: un
        # `401` del proveedor no es un problema de la plataforma que se pueda reintentar.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El proveedor rechazó la credencial: {error}",
        ) from error

    encrypted_access_token = encrypt_organization_credential(
        payload.token,
        tenant.organization.id,
        payload.provider,
        field="access",
    )
    result = await session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == tenant.organization.id,
            GitCredential.provider == payload.provider,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(
            GitCredential(
                organization_id=tenant.organization.id,
                provider=payload.provider,
                encrypted_access_token=encrypted_access_token,
            )
        )
    else:
        # Un PAT no tiene fecha de expiración conocida, así que se borra la anterior:
        # dejarla puesta haría que un cliente usara un token ya sustituido por el
        # usuario. La credencial OAuth tampoco se conserva porque tiene su propia fila
        # única, y lo que se guarda aquí es exactamente lo que el usuario eligió.
        existing.encrypted_access_token = encrypted_access_token
        existing.token_expires_at = None
    await session.commit()

    return PersonalTokenConnectResponse(
        provider=payload.provider,
        account_login=identity.login,
        account_display_name=identity.display_name,
        account_email=identity.email,
        replaced_existing=existing is not None,
    )
