"""Esquemas Pydantic estrictos del inventario y gestión de repositorios."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.apps.repositories.inventory import NormalizedRepository
from backend.apps.repositories.models import GitProviderEnum, Repository
from backend.apps.repositories.validation import GitReferenceError, validate_git_branch


class RepositoryResponse(BaseModel):
    """Vista pública de un repositorio conectado; nunca expone el secreto HMAC."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: GitProviderEnum
    remote_repo_id: str
    name: str
    full_name: str
    clone_url: str
    default_branch: str
    pr_reviews_enabled: bool
    is_active: bool
    webhook_registered: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_repository(cls, repository: Repository) -> RepositoryResponse:
        return cls(
            id=repository.id,
            provider=repository.provider,
            remote_repo_id=repository.remote_repo_id,
            name=repository.name,
            full_name=repository.full_name,
            clone_url=repository.clone_url,
            default_branch=repository.default_branch,
            pr_reviews_enabled=repository.pr_reviews_enabled,
            is_active=repository.is_active,
            webhook_registered=repository.webhook_id is not None,
            created_at=repository.created_at,
            updated_at=repository.updated_at,
        )


class RepositoryPage(BaseModel):
    """Página paginada de repositorios de la organización activa."""

    items: list[RepositoryResponse]
    total: int
    limit: int
    offset: int


class RemoteRepositoryResponse(BaseModel):
    """Repositorio accesible con la credencial conectada, ya normalizado."""

    remote_repo_id: str
    name: str
    full_name: str
    clone_url: str
    default_branch: str
    is_private: bool
    already_connected: bool

    @classmethod
    def from_normalized(
        cls,
        normalized: NormalizedRepository,
        *,
        already_connected: bool,
    ) -> RemoteRepositoryResponse:
        return cls(
            remote_repo_id=normalized.remote_repo_id,
            name=normalized.name,
            full_name=normalized.full_name,
            clone_url=normalized.clone_url,
            default_branch=normalized.default_branch,
            is_private=normalized.is_private,
            already_connected=already_connected,
        )


class RemoteRepositoryPage(BaseModel):
    """Inventario remoto paginado en memoria para el selector del frontend."""

    items: list[RemoteRepositoryResponse]
    total: int
    limit: int
    offset: int
    provider: GitProviderEnum


class RepositoryConnectRequest(BaseModel):
    """Solicitud de alta de un repositorio remoto previamente inventariado."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: GitProviderEnum
    remote_repo_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    pr_reviews_enabled: bool = True


class RepositoryConnectResponse(BaseModel):
    """Resultado del alta, indicando si el webhook pudo registrarse."""

    repository: RepositoryResponse
    webhook_registered: bool
    created: bool


class RepositoryUpdateRequest(BaseModel):
    """Cambios de política sobre un repositorio ya conectado."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pr_reviews_enabled: bool | None = None
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if (
            self.pr_reviews_enabled is None
            and self.default_branch is None
            and self.is_active is None
        ):
            raise ValueError("Debe enviarse al menos un campo para actualizar")
        if self.default_branch is not None:
            try:
                validate_git_branch(self.default_branch, field_name="la rama por defecto")
            except GitReferenceError as error:
                raise ValueError(str(error)) from error
        return self
