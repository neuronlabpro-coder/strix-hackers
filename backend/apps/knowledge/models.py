"""Modelo del catálogo técnico de remediación (OWASP Top 10 y CWE)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class KnowledgeCategoryEnum(StrEnum):
    """Familias de vulnerabilidad cubiertas por el motor."""

    INJECTION = "INJECTION"
    XSS = "XSS"
    AUTH = "AUTH"
    CRYPTOGRAPHY = "CRYPTOGRAPHY"
    SECRET_EXPOSURE = "SECRET_EXPOSURE"  # noqa: S105 - nombre de categoría, no un secreto
    DESERIALIZATION = "DESERIALIZATION"
    SSRF = "SSRF"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    LOGIC = "LOGIC"
    DEPENDENCY = "DEPENDENCY"


class KnowledgeSeverityEnum(StrEnum):
    """Severidad de referencia del apunte de remediación."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class KnowledgeEntry(Base):
    """Apunte de remediación de una familia de vulnerabilidad.

    A diferencia de `Vulnerability`, este modelo no pertenece a una organización:
    es una referencia técnica compartida, de solo lectura y mantenida con
    migraciones. Los apuntes que un tenant escribe sobre sus propias aplicaciones
    (reglas de negocio, activos críticos) vivirán en el módulo de conocimiento
    por organización del bloque siguiente.
    """

    __tablename__ = "knowledge_entries"
    __table_args__ = (
        Index("ix_knowledge_entries_category", "category"),
        Index("ix_knowledge_entries_severity", "severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[KnowledgeCategoryEnum] = mapped_column(
        SQLEnum(KnowledgeCategoryEnum, name="knowledge_category_enum"), nullable=False
    )
    severity: Mapped[KnowledgeSeverityEnum] = mapped_column(
        SQLEnum(KnowledgeSeverityEnum, name="knowledge_severity_enum"), nullable=False
    )
    risk_summary: Mapped[str] = mapped_column(Text, nullable=False)
    vulnerable_example: Mapped[str] = mapped_column(Text, nullable=False)
    secure_example: Mapped[str] = mapped_column(Text, nullable=False)
    mitigation: Mapped[str] = mapped_column(Text, nullable=False)
    owasp_category: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
