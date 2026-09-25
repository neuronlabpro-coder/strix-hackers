"""Construcción segura de clientes Git desde una credencial tenant-bound."""

from __future__ import annotations

from uuid import UUID

import httpx

from backend.apps.repositories.clients.base import BaseGitClient
from backend.apps.repositories.clients.github import GitHubClient
from backend.apps.repositories.clients.gitlab import GitLabClient
from backend.apps.repositories.models import GitCredential, GitProviderEnum, Repository
from backend.core.crypto import decrypt_secret


class UnsupportedGitProviderError(ValueError):
    """El proveedor aún no tiene un adaptador de API habilitado."""


def _construct_client(
    provider: GitProviderEnum,
    access_token: str,
    organization_id: UUID,
    api_base_url: str | None,
    http_client: httpx.Client | None,
    allowed_repo_full_name: str | None,
) -> BaseGitClient:
    if provider == GitProviderEnum.GITHUB:
        return GitHubClient(
            access_token,
            organization_id,
            api_base_url or "https://api.github.com",
            http_client,
            allowed_repo_full_name,
        )
    if provider == GitProviderEnum.GITLAB:
        return GitLabClient(
            access_token,
            organization_id,
            api_base_url or "https://gitlab.com/api/v4",
            http_client,
            allowed_repo_full_name,
        )
    raise UnsupportedGitProviderError("El proveedor Git todavía no está soportado")


def get_client_for_credential(
    credential: GitCredential,
    organization_id: UUID,
    provider: GitProviderEnum,
    *,
    allowed_repo_full_name: str | None = None,
    api_base_url: str | None = None,
    http_client: httpx.Client | None = None,
) -> BaseGitClient:
    """Descifra el token en memoria y acota el cliente a un tenant y repositorio."""

    if credential.organization_id != organization_id:
        raise PermissionError("La credencial no pertenece a la organización solicitada")
    if credential.provider != provider:
        raise PermissionError("La credencial no corresponde al proveedor solicitado")
    access_token = decrypt_secret(
        credential.encrypted_access_token,
        organization_id=str(credential.organization_id),
        provider=credential.provider.value,
        field="access",
    )
    return _construct_client(
        provider,
        access_token,
        organization_id,
        api_base_url,
        http_client,
        allowed_repo_full_name,
    )


def get_git_client(
    credential: GitCredential,
    repository: Repository,
    *,
    api_base_url: str | None = None,
    http_client: httpx.Client | None = None,
) -> BaseGitClient:
    """Valida tenant/proveedor, descifra en memoria y vincula el repositorio."""

    return get_client_for_credential(
        credential,
        repository.organization_id,
        repository.provider,
        allowed_repo_full_name=repository.full_name,
        api_base_url=api_base_url,
        http_client=http_client,
    )
