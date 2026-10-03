"""Esquemas del resumen de dashboard expuestos al panel web."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.repositories.models import GitProviderEnum
from backend.apps.vulnerabilities.models import SeverityEnum
from backend.apps.vulnerabilities.schemas import SeverityCount, StatusCount

__all__ = [
    "DashboardSummaryResponse",
    "FindingsTrendPoint",
    "RepositoryDashboardItem",
    "RepositoryMonitoringStatusEnum",
    "SeverityCount",
    "StatusCount",
]


class RepositoryMonitoringStatusEnum(StrEnum):
    """Estado de monitorización derivado de las revisiones del repositorio."""

    NOT_TESTED = "NOT_TESTED"
    TESTED = "TESTED"
    SCANNING = "SCANNING"


class FindingsTrendPoint(BaseModel):
    """Hallazgos detectados en un día, repartidos por severidad.

    ## Por qué un día por punto y no un periodo acumulativo

    Porque la pregunta del gráfico es «¿cuándo aparecieron los problemas?», y el total
    acumulado no la responde: con un acumulado, un pico de la semana pasada y una semana
    tranquila de hoy se leen igual. Un punto por día con el desglose de severidad sí las
    distingue, porque la altura del área es la producción de ese día.

    ## Por qué `por_severidad` viene siempre con las cinco claves

    Porque un día sin hallazgos críticos vale cero, no «no existe». Con la clave ausente
    tendría que distinguir el panel entre las dos cosas, y la distinción equivocada —«no hay
    críticos»— es justo la que no se puede equivocar.
    """

    dia: date
    total: int = Field(ge=0)
    por_severidad: dict[SeverityEnum, int]


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
    #: Reparto de los hallazgos **abiertos** por severidad. Solo abiertos, porque un hallazgo
    #: corregido ya no es carga de trabajo y mezclarlo aquí escondería al cliente su propia
    #: cola.
    severity_distribution: list[SeverityCount]
    #: Reparto de **todos** los hallazgos por estado de remediación, incluidos los corregidos,
    #: aplazados e ignorados. Es la respuesta a «¿qué estoy cerrando?», que la distribución por
    #: severidad no puede dar: los corregidos no tienen severidad abierta que enseñar.
    status_distribution: list[StatusCount]
    #: Producción diaria de hallazgos de la ventana, desglosada por severidad y con los días
    #: vacíos a cero. Es la serie temporal del panel, y se calcula aquí porque la API no
    #: devolvía datos agregados por periodo.
    findings_trend: list[FindingsTrendPoint]
    repositories: list[RepositoryDashboardItem]
    generated_at: datetime
