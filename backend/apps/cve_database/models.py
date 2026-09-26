"""Modelo del catálogo CVE de referencia.

A diferencia de `Vulnerability`, estos registros no pertenecen a ninguna
organización: son datos públicos de referencia que describen una vulnerabilidad
conocida, no un hallazgo de un cliente. Sirven para que un Pentest pueda resolver
el impacto real de un `cve_id` que Strix reporte.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    func,
    literal_column,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class CVESeverityEnum(StrEnum):
    """Severidad normalizada de un CVE.

    No incluye `INFO`: el feed de CISA y NVD no publican CVEs informacionales, y
    un valor que el feed nunca emite sería una cuarta categoría muerta.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CVERecord(Base):
    """Vulnerabilidad pública conocida, indexada para búsqueda rápida."""

    __tablename__ = "cve_records"
    __table_args__ = (
        CheckConstraint(
            "cvss_score >= 0 AND cvss_score <= 10",
            name="ck_cve_records_cvss_range",
        ),
        CheckConstraint(
            "epss_score IS NULL OR (epss_score >= 0 AND epss_score <= 1)",
            name="ck_cve_records_epss_range",
        ),
        CheckConstraint("cve_id ~ '^CVE-[0-9]{4}-[0-9]{4,}$'", name="ck_cve_records_id_format"),
        Index("ix_cve_records_severity_published", "severity", "published_at"),
        Index("ix_cve_records_kev_published", "is_kev", "published_at"),
    )

    # El identificador CVE es la clave primaria natural: no necesita UUID y buscar
    # por él es la operación más frecuente del módulo.
    cve_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    severity: Mapped[CVESeverityEnum] = mapped_column(
        SQLEnum(CVESeverityEnum, name="cve_severity_enum"), nullable=False
    )
    cvss_score: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    epss_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    is_kev: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)


# Los dos índices de búsqueda se declaran fuera de `__table_args__` porque son
# índices de expresión y necesitan las columnas ya definidas, cosa que en
# `__table_args__` todavía no ha ocurrido.
#
# - `ix_cve_records_search`: GIN sobre el `tsvector` de la descripción, que es lo
#   que hace que buscar por palabra clave no degrade a un barrido secuencial.
# - `ix_cve_records_id_trgm`: trigram sobre el identificador. `cve_id LIKE
#   '%2026%'` no usa el índice btree, y sin trigram el filtro por año —la
#   navegación más usada— sería el más lento de todos.
Index(
    "ix_cve_records_search",
    # El diccionario se inyecta como `literal_column` porque `REGCONFIG` no tiene
    # renderizador de literales en SQLAlchemy: castear un string falla al compilar
    # el DDL del índice.
    func.to_tsvector(
        literal_column("'spanish'::regconfig"),
        func.coalesce(CVERecord.description, ""),
    ),
    postgresql_using="gin",
)
Index(
    "ix_cve_records_id_trgm",
    CVERecord.cve_id,
    postgresql_using="gin",
    postgresql_ops={"cve_id": "gin_trgm_ops"},
)
