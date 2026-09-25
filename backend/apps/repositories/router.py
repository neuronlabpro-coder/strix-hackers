"""Endpoints multi-tenant de inventario, conexión y gestión de repositorios."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import RoleEnum
from backend.apps.repositories.clients.base import BaseGitClient, GitClientError
from backend.apps.repositories.clients.factory import UnsupportedGitProviderError
from backend.apps.repositories.inventory import (
    NormalizedRepository,
    RemoteRepositoryError,
    normalize_remote_repository,
    webhook_callback_url,
    webhook_subscription_events,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
    generate_webhook_secret,
)
from backend.apps.repositories.schemas import (
    RemoteRepositoryPage,
    RemoteRepositoryResponse,
    RepositoryConnectRequest,
    RepositoryConnectResponse,
    RepositoryPage,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from backend.apps.repositories.services import (
    GitCredentialNotFoundError,
    build_organization_client,
)
from backend.core.crypto import CryptoError
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant
from backend.core.rate_limit import enforce_repository_management_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
ManagementRateLimit = Depends(enforce_repository_management_rate_limit)

_IN_PROGRESS_REVIEW_STATUSES = (PRReviewStatusEnum.QUEUED, PRReviewStatusEnum.SCANNING)
_SUPPORTED_MANAGEMENT_PROVIDERS = frozenset({GitProviderEnum.GITHUB, GitProviderEnum.GITLAB})


def _require_admin(tenant: TenantContext) -> None:
    if tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador",
        )


def _translate_client_error(error: GitClientError) -> HTTPException:
    if error.status_code in {401, 403}:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="La credencial del proveedor fue rechazada o carece de permisos",
        )
    if error.status_code == 404:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El recurso no existe en el proveedor Git",
        )
    if error.status_code == 429:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="El proveedor Git agotó su cuota temporal",
            headers={"Retry-After": str(error.retry_after or 30)},
        )
    if error.status_code is not None and error.status_code >= 500:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El proveedor Git no está disponible",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="No se pudo completar la operación con el proveedor Git",
    )


@asynccontextmanager
async def _open_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> AsyncIterator[BaseGitClient]:
    if provider not in _SUPPORTED_MANAGEMENT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="El proveedor todavía no tiene conector de gestión",
        )
    client = await _try_open_client(session, organization_id, provider)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conecta primero una credencial de {provider.value} para esta organización",
        )
    try:
        yield client
    finally:
        client.close()


async def _try_open_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> BaseGitClient | None:
    """Construye el cliente del tenant o devuelve `None` si no hay credencial utilizable."""

    try:
        return await build_organization_client(session, organization_id, provider)
    except (GitCredentialNotFoundError, UnsupportedGitProviderError, CryptoError):
        return None


def _normalized_inventory(
    provider: GitProviderEnum,
    raw_repositories: list[dict[str, object]],
) -> list[NormalizedRepository]:
    """Descarta entradas que no podemos materializar de forma segura."""

    normalized: list[NormalizedRepository] = []
    for payload in raw_repositories:
        try:
            normalized.append(normalize_remote_repository(provider, payload))
        except RemoteRepositoryError as error:
            logger.warning(
                "Repositorio remoto descartado del inventario: provider=%s reason=%s",
                provider.value,
                str(error),
            )
    return normalized


@router.get(
    "/api/v1/repositories/remote",
    response_model=RemoteRepositoryPage,
    dependencies=[ManagementRateLimit],
)
async def list_remote_repositories(
    tenant: TenantDependency,
    session: SessionDependency,
    provider: Annotated[GitProviderEnum, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> RemoteRepositoryPage:
    """Lista el inventario accesible con la credencial conectada del tenant."""

    connected_result = await session.execute(
        select(Repository.remote_repo_id).where(
            Repository.organization_id == tenant.organization.id,
            Repository.provider == provider,
        )
    )
    connected_ids = {row for row in connected_result.scalars()}
    async with _open_client(session, tenant.organization.id, provider) as client:
        try:
            raw_repositories = await asyncio.to_thread(client.list_repositories)
        except GitClientError as error:
            raise _translate_client_error(error) from error
        normalized = _normalized_inventory(provider, raw_repositories)
    window = normalized[offset : offset + limit]
    return RemoteRepositoryPage(
        items=[
            RemoteRepositoryResponse.from_normalized(
                repository,
                already_connected=repository.remote_repo_id in connected_ids,
            )
            for repository in window
        ],
        total=len(normalized),
        limit=limit,
        offset=offset,
        provider=provider,
    )


@router.post(
    "/api/v1/repositories/connect",
    response_model=RepositoryConnectResponse,
    dependencies=[ManagementRateLimit],
)
async def connect_repository(
    response: Response,
    tenant: TenantDependency,
    session: SessionDependency,
    payload: RepositoryConnectRequest,
) -> RepositoryConnectResponse:
    """Da de alta el repositorio verificando sus metadatos contra el proveedor."""

    _require_admin(tenant)
    existing_result = await session.execute(
        select(Repository).where(
            Repository.provider == payload.provider,
            Repository.remote_repo_id == payload.remote_repo_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None and existing.organization_id != tenant.organization.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El repositorio ya está vinculado a otra organización",
        )

    async with _open_client(session, tenant.organization.id, payload.provider) as client:
        try:
            raw_repository = await asyncio.to_thread(
                client.get_repository,
                payload.remote_repo_id,
            )
        except GitClientError as error:
            raise _translate_client_error(error) from error
        try:
            normalized = normalize_remote_repository(payload.provider, raw_repository)
        except RemoteRepositoryError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="El proveedor devolvió metadatos de repositorio no utilizables",
            ) from error

        repository = existing
        created = repository is None
        if repository is None:
            repository = Repository(
                organization_id=tenant.organization.id,
                provider=payload.provider,
                remote_repo_id=normalized.remote_repo_id,
                name=normalized.name,
                full_name=normalized.full_name,
                clone_url=normalized.clone_url,
                default_branch=normalized.default_branch,
                pr_reviews_enabled=payload.pr_reviews_enabled,
                webhook_secret=generate_webhook_secret(),
            )
            session.add(repository)
        else:
            repository.name = normalized.name
            repository.full_name = normalized.full_name
            repository.clone_url = normalized.clone_url
            repository.default_branch = normalized.default_branch
            repository.pr_reviews_enabled = payload.pr_reviews_enabled
            repository.is_active = True
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El repositorio ya está conectado",
            ) from error

        webhook_registered = False
        if repository.webhook_id is None:
            try:
                repository.webhook_id = await asyncio.to_thread(
                    client.create_webhook,
                    normalized.full_name,
                    webhook_callback_url(payload.provider),
                    repository.webhook_secret,
                    webhook_subscription_events(),
                )
                webhook_registered = True
            except (GitClientError, RemoteRepositoryError) as error:
                logger.warning(
                    "No se pudo registrar el webhook: provider=%s repository=%s reason=%s",
                    payload.provider.value,
                    normalized.full_name,
                    str(error),
                )
        else:
            webhook_registered = True
        await session.commit()
        await session.refresh(repository)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return RepositoryConnectResponse(
        repository=RepositoryResponse.from_repository(repository),
        webhook_registered=webhook_registered,
        created=created,
    )


@router.get("/api/v1/repositories/", response_model=RepositoryPage)
async def list_repositories(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    provider: Annotated[GitProviderEnum | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> RepositoryPage:
    """Lista paginada de repositorios conectados de la organización activa."""

    filters = [Repository.organization_id == tenant.organization.id]
    if provider is not None:
        filters.append(Repository.provider == provider)
    if is_active is not None:
        filters.append(Repository.is_active.is_(is_active))
    total_result = await session.execute(
        select(func.count()).select_from(Repository).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(Repository)
        .where(*filters)
        .order_by(Repository.created_at.desc(), Repository.id)
        .limit(limit)
        .offset(offset)
    )
    repositories = result.scalars().all()
    return RepositoryPage(
        items=[RepositoryResponse.from_repository(repository) for repository in repositories],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/repositories/{repository_id}", response_model=RepositoryResponse)
async def get_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
) -> RepositoryResponse:
    """Devuelve un repositorio únicamente si pertenece a la organización activa."""

    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    return RepositoryResponse.from_repository(repository)


@router.patch(
    "/api/v1/repositories/{repository_id}",
    response_model=RepositoryResponse,
    dependencies=[ManagementRateLimit],
)
async def update_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
    payload: RepositoryUpdateRequest,
) -> RepositoryResponse:
    """Actualiza la política de revisiones y la rama por defecto del repositorio."""

    _require_admin(tenant)
    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    if payload.pr_reviews_enabled is not None:
        repository.pr_reviews_enabled = payload.pr_reviews_enabled
    if payload.default_branch is not None:
        repository.default_branch = payload.default_branch
    if payload.is_active is not None:
        repository.is_active = payload.is_active
    await session.commit()
    await session.refresh(repository)
    return RepositoryResponse.from_repository(repository)


@router.delete(
    "/api/v1/repositories/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ManagementRateLimit],
)
async def delete_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
) -> Response:
    """Desvincula el repositorio y elimina el webhook en el proveedor si es posible."""

    _require_admin(tenant)
    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    in_progress_result = await session.execute(
        select(func.count())
        .select_from(PullRequestReview)
        .where(
            PullRequestReview.repository_id == repository.id,
            PullRequestReview.organization_id == tenant.organization.id,
            PullRequestReview.status.in_(_IN_PROGRESS_REVIEW_STATUSES),
        )
    )
    if int(in_progress_result.scalar_one()) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede desvincular un repositorio con revisiones en curso",
        )
    if repository.webhook_id is not None:
        client = await _try_open_client(
            session,
            tenant.organization.id,
            repository.provider,
        )
        if client is None:
            logger.warning(
                "Sin credencial utilizable para eliminar el webhook: provider=%s repository=%s",
                repository.provider.value,
                repository.full_name,
            )
        else:
            try:
                await asyncio.to_thread(
                    client.delete_webhook,
                    repository.full_name,
                    repository.webhook_id,
                )
            except GitClientError as error:
                logger.warning(
                    "No se pudo eliminar el webhook remoto: provider=%s repository=%s reason=%s",
                    repository.provider.value,
                    repository.full_name,
                    str(error),
                )
            finally:
                client.close()
    await session.delete(repository)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _load_tenant_repository(
    session: AsyncSession,
    organization_id: UUID,
    repository_id: UUID,
) -> Repository:
    """Carga un repositorio acotado al tenant; 404 si pertenece a otra organización."""

    result = await session.execute(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.organization_id == organization_id,
        )
    )
    repository = result.scalar_one_or_none()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repositorio no encontrado",
        )
    return repository
