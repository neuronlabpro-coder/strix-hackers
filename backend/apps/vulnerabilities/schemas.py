"""Esquemas Pydantic de la API de vulnerabilidades."""

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict

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


class AutofixResponse(BaseModel):
    """URL del Pull/Merge Request de remediación."""

    autofix_url: AnyHttpUrl


class VulnerabilityPage(BaseModel):
    """Página de vulnerabilidades de un tenant."""

    items: list[VulnerabilityListItem]
    total: int
    limit: int
    offset: int
