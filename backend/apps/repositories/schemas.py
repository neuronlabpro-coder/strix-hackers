"""Esquemas Pydantic estrictos del inventario y gestión de repositorios."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.apps.repositories.inventory import NormalizedRepository
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
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


class PRReviewResponse(BaseModel):
    """Revisión de seguridad de un repositorio.

    No expone `head_clone_url`: aunque contiene una URL de clonación del pull
    request, publicarla en el panel convertiría el listado de revisiones en una
    vía para obtener código del cliente. Tampoco expone `comment_id` ni el
    `commit_sha` completo, que no aportan nada a la decisión de triaje.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    repository_id: UUID
    repository_name: str
    run_id: UUID | None
    pr_number: int
    pr_title: str
    pr_author: str
    source_branch: str
    target_branch: str
    short_sha: str
    status: PRReviewStatusEnum
    issues_caught_critical: int
    issues_caught_high: int
    merge_blocked: bool
    finished_at: datetime | None
    created_at: datetime

    @classmethod
    def from_review(
        cls,
        review: PullRequestReview,
        *,
        repository_name: str,
    ) -> PRReviewResponse:
        return cls(
            id=review.id,
            repository_id=review.repository_id,
            repository_name=repository_name,
            run_id=review.run_id,
            pr_number=review.pr_number,
            pr_title=review.pr_title,
            pr_author=review.pr_author,
            source_branch=review.source_branch,
            target_branch=review.target_branch,
            short_sha=review.commit_sha[:7],
            status=review.status,
            issues_caught_critical=review.issues_caught_critical,
            issues_caught_high=review.issues_caught_high,
            merge_blocked=review.merge_blocked,
            finished_at=review.finished_at,
            created_at=review.created_at,
        )


class PRReviewPage(BaseModel):
    """Página de revisiones de un repositorio concreto o de toda la organización."""

    items: list[PRReviewResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class PRReviewMetrics(BaseModel):
    """Indicadores de cabecera de la vista global de revisiones de pull request.

    `total`, `clean` y `blocking` cuentan **revisiones**, no hallazgos: una revisión
    con doce hallazgos sigue siendo una revisión. `issues_critical` e `issues_high` sí
    cuentan hallazgos, porque la pregunta que responde el KPI es distinto.

    Una revisión cuenta como `blocking` cuando `merge_blocked` es cierto, sea cual sea
    el estado del escaneo. Lo que impide el merge es la bandera, no el estado: un
    escaneo que terminó en `PASSED` con un hallazgo de severidad alta sigue bloqueando
    el merge, y un `FAILED` sin hallazgos relevantes no debería hacerlo.
    """

    total: int = Field(ge=0)
    clean: int = Field(ge=0)
    blocking: int = Field(ge=0)
    issues_critical: int = Field(ge=0)
    issues_high: int = Field(ge=0)
