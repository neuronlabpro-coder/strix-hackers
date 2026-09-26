"""Esquemas Pydantic estrictos del inventario y gestión de repositorios."""

from __future__ import annotations

import re
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


# Formatos de token personal que cada proveedor ha emitido. No es una lista cerrada y
# no pretende serlo: un prefijo nuevo válidos un token perfectamente bueno, así que el
# formato solo sirve para **descartar basura sin gastar una llamada de red**, y la
# autoridad sobre si el token funciona es siempre la API del proveedor. Un formato
# desconocido cae en el patrón genérico y se verifica igual.
#
# Los `noqa: S105` son falsos positivos: la regla ve un nombre con `TOKEN` y supone que
# hay una credencial, cuando lo que hay es una expresión regular. Van en la línea de la
# cadena y no en la del nombre porque es ahí donde ruff ancla el diagnóstico.
_GITHUB_TOKEN_PATTERN = (
    r"^(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|[0-9a-f]{40}"  # noqa: S105
    r"|[A-Za-z0-9_\-]{20,255})$"
)
_GITLAB_TOKEN_PATTERN = (
    r"^(glpat-[A-Za-z0-9_\-]{20,}|[A-Za-z0-9_\-]{20,255})$"  # noqa: S105
)

# Prefijos que identifican a un proveedor sin ambigüedad. Se comprueban contra el
# proveedor *declarado* para poder decir "este token es de GitLab" en vez de dejar que
# GitHub rechace un token que nunca fue suyo. Solo se listan los prefijos exclusivos:
# `ghp_` de GitHub y `glpat-` de GitLab no se solapan con nada.
_PREFIJOS_DE_PROVEEDOR: tuple[tuple[GitProviderEnum, str], ...] = (
    (GitProviderEnum.GITHUB, "ghp_"),
    (GitProviderEnum.GITHUB, "gho_"),
    (GitProviderEnum.GITHUB, "ghu_"),
    (GitProviderEnum.GITHUB, "ghs_"),
    (GitProviderEnum.GITHUB, "ghr_"),
    (GitProviderEnum.GITHUB, "github_pat_"),
    (GitProviderEnum.GITLAB, "glpat-"),
)


class PersonalTokenConnectRequest(BaseModel):
    """Alta de credencial mediante Token Personal de acceso.

    El formato se comprueba aquí para no gastar una llamada al proveedor con basura
    escrita a mano, pero un token con el prefijo correcto y revocado se acepta en el
    borde: solo la API del proveedor puede decir si funciona, y por eso el endpoint
    verifica antes de cifrar.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: GitProviderEnum
    token: str = Field(min_length=20, max_length=255, pattern=_GITHUB_TOKEN_PATTERN)
    # El nombre es etiqueta para el usuario, no identificador. Se conserva porque
    # `git_credentials` es única por `(organization_id, provider)`: un tenant solo
    # tiene una credencial por proveedor y el nombre evita tener que suponer cuál es.
    name: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _enforce_provider_format(self) -> PersonalTokenConnectRequest:
        """Aplica el patrón del proveedor y descarta el token de otro proveedor.

        El campo se declara con el patrón de GitHub porque es el más específico de los
        dos, pero un token de GitLab no lo cumple. Ajustarlo aquí evita duplicar el
        campo y tener dos rutas para la misma validación.

        ## Por qué se distingue "token de otro" de "token inválido"

        Los patrones tienen una rama genérica al final para que un prefijo nuevo de un
        proveedor siga funcionando. Esa misma rama hace que un PAT de GitLab enviada
        como GitHub pase el filtro y llegue a GitHub, que responde con algo como "se
        rechazó la credencial": un mensaje que señala al proveedor equivocado y lleva a
        revisar permisos que no son el problema. Es el error más probable de este
        formulario, y se detecta aquí por el prefijo, que no miente.
        """

        for otro_proveedor, marca in _PREFIJOS_DE_PROVEEDOR:
            if otro_proveedor is self.provider:
                continue
            if self.token.lower().startswith(marca):
                raise ValueError(
                    f"El token parece de {otro_proveedor.value}, no de {self.provider.value}"
                )

        if self.provider == GitProviderEnum.GITLAB:
            if re.match(_GITLAB_TOKEN_PATTERN, self.token) is None:
                raise ValueError("El token no tiene el formato de un PAT de GitLab")
        return self


class PersonalTokenConnectResponse(BaseModel):
    """Identidad de la cuenta conectada, sin rastro del secreto.

    Se devuelve a quién pertenece el token para que el panel muestre la cuenta
    **antes** de que el usuario sincronice nada: conectar el repositorio equivocado
    es el error caro, y se detecta en cuanto se ve el nombre y no después.
    """

    provider: GitProviderEnum
    account_login: str
    account_display_name: str | None = None
    account_email: str | None = None
    replaced_existing: bool = False


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
