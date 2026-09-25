"""Esquemas del resumen de dashboard expuestos al panel web."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.repositories.models import GitProviderEnum
from backend.apps.vulnerabilities.models import SeverityEnum


class RepositoryMonitoringStatusEnum(StrEnum):
    """Estado de monitorización derivado de las revisiones del repositorio."""

    NOT_TESTED = "NOT_TESTED"
    TESTED = "TESTED"
    SCANNING = "SCANNING"


class SeverityCount(BaseModel):
    """Conteo de hallazgos abiertos por severidad."""

    model_config = ConfigDict(from_attributes=True)

    severity: SeverityEnum
    total: int = Field(ge=0)


class RepositoryDashboardItem(BaseModel):
    """Resumen operativo de un repositorio conectado."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: GitProviderEnum
    name: str
    full_name: str
    is_active: bool
    pr_reviews_enabled: bool
    webhook_registered: bool
    status: RepositoryMonitoringStatusEnum
    open_vulnerabilities: int = Field(ge=0)
    last_tested_at: datetime | None


class DashboardSummaryResponse(BaseModel):
    """KPIs de postura, distribución por severidad y estado de repositorios."""

    security_score: int = Field(ge=0, le=100)
    open_issues: int = Field(ge=0)
    total_issues: int = Field(ge=0)
    fix_rate: float = Field(ge=0.0, le=1.0)
    prs_reviewed: int = Field(ge=0)
    prs_reviewed_total: int = Field(ge=0)
    pentests_total: int = Field(ge=0)
    repositories_monitored: int = Field(ge=0)
    severity_distribution: list[SeverityCount]
    repositories: list[RepositoryDashboardItem]
    generated_at: datetime
