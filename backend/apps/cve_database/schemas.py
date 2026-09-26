"""Esquemas del catálogo CVE."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.cve_database.models import CVESeverityEnum


class CVESummary(BaseModel):
    """Ficha resumida para la tabla del buscador."""

    model_config = ConfigDict(from_attributes=True)

    cve_id: str
    severity: CVESeverityEnum
    cvss_score: Decimal
    epss_score: Decimal | None
    is_kev: bool
    published_at: datetime
    description: str


class CVEDetail(CVESummary):
    """Ficha completa de una vulnerabilidad.

    No añade campos a propósito: el detalle y la fila de la tabla muestran lo
    mismo, de modo que abrir un CVE desde la lista no cambia lo que el usuario
    ve. Un endpoint de detalle sin información adicional solo añade una petición.
    """


class CVEPage(BaseModel):
    """Página del catálogo CVE."""

    items: list[CVESummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class CVEYearsResponse(BaseModel):
    """Años con registros publicados, del más reciente al más antiguo."""

    years: list[int] = Field(min_length=1)
