"""Servicios de acceso a credenciales Git con aislamiento por organización."""

from __future__ import annotations

from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.clients.base import BaseGitClient
from backend.apps.repositories.clients.factory import get_git_client
from backend.apps.repositories.models import GitCredential, GitProviderEnum, Repository
from backend.core.crypto import encrypt_secret


class GitCredentialNotFoundError(LookupError):
    """No existe una credencial activa para la organización y proveedor."""


def encrypt_organization_credential(
    plaintext: str,
    organization_id: UUID,
    provider: GitProviderEnum,
    field: str = "access",
) -> str:
    """Cifra un token con AAD ligado al tenant, proveedor y tipo de campo."""

    return encrypt_secret(
        plaintext,
        organization_id=str(organization_id),
        provider=provider.value,
        field=field,
    )


async def get_organization_credential(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> GitCredential:
    """Obtiene exactamente la credencial del tenant solicitado."""

    result = await session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == organization_id,
            GitCredential.provider == provider,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )
    return credential


async def build_client_for_repository(
    session: AsyncSession,
    repository: Repository,
    *,
    api_base_url: str | None = None,
    http_client: httpx.Client | None = None,
) -> BaseGitClient:
    """Descifra el token solo en memoria y lo vincula al tenant del repositorio."""

    credential = await get_organization_credential(
        session,
        repository.organization_id,
        repository.provider,
    )
    return get_git_client(
        credential,
        repository,
        api_base_url=api_base_url,
        http_client=http_client,
    )
