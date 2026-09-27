"""Modelos de vulnerabilidades y evidencias técnicas inmutables."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class SeverityEnum(StrEnum):
    """Severidades normalizadas de los hallazgos."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class IssueStatusEnum(StrEnum):
    """Estado de remediación de una vulnerabilidad.

    ## Por qué `REMEDIATION_PROPOSED` es un estado y no una marca

    Porque "hay un PR abierto que lo arregla" y "está arreglado" son afirmaciones distintas
    que se contradicen: el PR puede cerrarse sin fusionarse, o fusionarse y que el arreglo no
    solucione. Un hallazgo con un PR abierto **sigue siendo un hallazgo abierto**, y meterlo
    en `FIXED` al abrir el PR haría que el panel dijera que está resuelto cuando lo único que
    ha pasado es que alguien ha propuesto una solución.

    El estado dice exactamente lo que es cierto: hay una propuesta. Cerrar el PR sin fusionar
    devuelve el hallazgo a `OPEN`, y eso es una transición que el cliente puede hacer
    explícitamente.
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    REMEDIATION_PROPOSED = "REMEDIATION_PROPOSED"
    FIXED = "FIXED"
    SNOOZED = "SNOOZED"
    IGNORED = "IGNORED"

    @property
    def is_open_for_closure(self) -> bool:
        """Si el hallazgo cuenta como abierto para el resumen de postura.

        `REMEDIATION_PROPOSED` cuenta como abierto a propósito, por lo mismo que el estado
        existe: un PR sin fusionar no arregla nada. Es el mismo criterio que usa la
        herramienta MCP `get_vulnerability_summary`, y los dos leen de aquí para que no puedan
        discrepar sobre qué es un hallazgo abierto.
        """

        return self in (
            IssueStatusEnum.OPEN,
            IssueStatusEnum.IN_PROGRESS,
            IssueStatusEnum.REMEDIATION_PROPOSED,
        )


class Vulnerability(Base):
    """Hallazgo técnico asociado a un run y a su organización."""

    __tablename__ = "vulnerabilities"
    __table_args__ = (
        Index("ix_vulnerabilities_org_severity", "organization_id", "severity"),
        Index("ix_vulnerabilities_org_status", "organization_id", "status"),
        UniqueConstraint(
            "run_id",
            "source_finding_id",
            name="uq_vulnerabilities_run_source_finding",
        ),
        CheckConstraint(
            "cvss_score >= 0 AND cvss_score <= 10",
            name="ck_vulnerabilities_cvss_score",
        ),
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            ["pentest_runs.id", "pentest_runs.organization_id"],
            name="fk_vulnerabilities_run_organization",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    source_finding_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[SeverityEnum] = mapped_column(
        SQLEnum(SeverityEnum, name="severity_enum"), nullable=False, index=True
    )
    cvss_score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    cve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    affected_target: Mapped[str] = mapped_column(String(512), nullable=False)
    affected_line: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poc_reproduction_raw: Mapped[str] = mapped_column(Text, nullable=False)
    autofix_patch_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Enlace a la Pull Request o Merge Request que propone la corrección.
    #:
    #: Vive **en** la vulnerabilidad y no en una tabla aparte de propuestas porque solo puede
    #: haber una activa: una segunda propuesta sobre el mismo hallazgo se genera contra un
    #: diff que ya existe, y el que está en la columna es el vigente. Guardar un histórico
    #: exigiría una tabla con su propio ciclo de vida, y ese dato no lo necesita nadie: lo
    #: que importa es dónde mirar hoy.
    #:
    #: Es una referencia **externa** y por eso no lleva clave foránea: la PR vive en GitHub o
    #: GitLab, y una integridad referencial contra un servicio de terceros no es integridad,
    #: es una dependencia que convierte un borrado de repositorio en un fallo de la base de
    #: datos.
    remediation_pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: El parche que generó la **plataforma** a partir de la evidencia, cuando existe.
    #:
    #: ## Por qué no se escribe en `autofix_patch_diff`
    #:
    #: Porque ese campo es **evidencia forense** y el trigger `trg_protect_vulnerability_evidence`
    #: lo hace inmutable junto con el PoC, la línea afectada y el CVSS. Y tiene razón: lo que
    #: escribió el motor dentro del contenedor durante el escaneo es lo que ocurrió, y no puede
    #: cambiarse después porque eso invalidaría la auditoría.
    #:
    #: Un diff generado **después**, en la plataforma, desde esa evidencia y sin desplegar el
    #: repositorio, es otra cosa: es un borrador. No ocurrió en el escaneo, no se ha aplicado y
    #: se puede volver a generar. Meterlo en la columna de evidencia sería escribir una
    #: propuesta sobre un registro forense, y ninguna de las dos cosas saldría bien: la
    #: evidencia quedaría contaminada y la propuesta no podría rehacerse.
    #:
    #: No es, por tanto, una decisión de almacenamiento sino de significado: lo que se vio y lo
    #: que se propone no son el mismo hecho y no pueden vivir en la misma columna.
    remediation_patch_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[IssueStatusEnum] = mapped_column(
        SQLEnum(IssueStatusEnum, name="issue_status_enum"),
        nullable=False,
        default=IssueStatusEnum.OPEN,
        server_default=IssueStatusEnum.OPEN.name,
        index=True,
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
