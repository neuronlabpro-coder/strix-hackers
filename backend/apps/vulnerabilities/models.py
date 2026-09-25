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
    """Estado de remediación de una vulnerabilidad."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    FIXED = "FIXED"
    SNOOZED = "SNOOZED"
    IGNORED = "IGNORED"


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
