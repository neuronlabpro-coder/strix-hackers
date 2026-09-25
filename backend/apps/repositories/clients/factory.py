"""Construcción segura de clientes Git desde una credencial tenant-bound."""

from __future__ import annotations

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
    repository: Repository,
    api_base_url: str | None,
    http_client: httpx.Client | None,
) -> BaseGitClient:
    if provider == GitProviderEnum.GITHUB:
        return GitHubClient(
            access_token,
            repository.organization_id,
            api_base_url or "https://api.github.com",
            http_client,
            repository.full_name,
        )
    if provider == GitProviderEnum.GITLAB:
        return GitLabClient(
            access_token,
            repository.organization_id,
            api_base_url or "https://gitlab.com/api/v4",
            http_client,
            repository.full_name,
        )
    raise UnsupportedGitProviderError("El proveedor Git todavía no está soportado")


def get_git_client(
    credential: GitCredential,
    repository: Repository,
    *,
    api_base_url: str | None = None,
    http_client: httpx.Client | None = None,
) -> BaseGitClient:
    """Valida tenant/proveedor, descifra en memoria y vincula el repositorio."""

    if credential.organization_id != repository.organization_id:
        raise PermissionError("La credencial no pertenece a la organización del repositorio")
    if credential.provider != repository.provider:
        raise PermissionError("La credencial no corresponde al proveedor del repositorio")
    access_token = decrypt_secret(
        credential.encrypted_access_token,
        organization_id=str(credential.organization_id),
        provider=credential.provider.value,
        field="access",
    )
    return _construct_client(
        credential.provider,
        access_token,
        repository,
        api_base_url,
        http_client,
    )
