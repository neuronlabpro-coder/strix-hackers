"""Esquemas Pydantic de la API de vulnerabilidades."""

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum


class VulnerabilityListItem(BaseModel):
    """Resumen de un hallazgo para listados paginados."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    title: str
    severity: SeverityEnum
    cvss_score: float
    cve_id: str | None
    affected_target: str
    status: IssueStatusEnum
    discovered_at: datetime


class VulnerabilityDetail(VulnerabilityListItem):
    """Detalle completo con evidencias técnicas."""

    description: str
    affected_line: str | None
    poc_reproduction_raw: str
    autofix_patch_diff: str | None
    updated_at: datetime


class AutofixRequest(BaseModel):
    """Identificador de la revisión que origina el hallazgo."""

    review_id: UUID


class VulnerabilityTriageRequest(BaseModel):
    """Triaje de un hallazgo: únicamente su estado de remediación.

    R4 hace la defensa en dos capas. `extra="forbid"` hace que cualquier campo
    forense enviado por el cliente (`title`, `poc_reproduction_raw`, `cvss_score`,
    `cve_id`, `affected_target`, `autofix_patch_diff`, `run_id`, ...) provoque un
    `422` en el borde, antes de tocar la base de datos. Si alguien alcanzara el
    endpoint por otra vía, el trigger `protect_vulnerability_evidence` seguiría
    bloqueando la escritura a nivel de PostgreSQL.
    """

    model_config = ConfigDict(extra="forbid")

    status: IssueStatusEnum = Field(
        description="Único campo mutable: el estado del ciclo de vida de remediación."
    )


class VulnerabilityTriageResponse(BaseModel):
    """Resultado del triaje con la marca temporal del cambio."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: IssueStatusEnum
    updated_at: datetime
    changed: bool = Field(
        description="Falso cuando el estado solicitado ya era el actual: no se audita."
    )


class AutofixResponse(BaseModel):
    """URL del Pull/Merge Request de remediación."""

    autofix_url: AnyHttpUrl


class VulnerabilityPage(BaseModel):
    """Página de vulnerabilidades de un tenant."""

    items: list[VulnerabilityListItem]
    total: int
    limit: int
    offset: int
