"""Modelo append-only del rastro de auditoría de la plataforma."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class AuditActionEnum(StrEnum):
    """Acciones auditables. Se añaden, nunca se reinterpretan."""

    STATUS_CHANGED = "STATUS_CHANGED"
    REPOSITORY_POLICY_UPDATED = "REPOSITORY_POLICY_UPDATED"
    REPOSITORY_CONNECTED = "REPOSITORY_CONNECTED"
    REPOSITORY_DISCONNECTED = "REPOSITORY_DISCONNECTED"
    # La baja lógica de un tenant. Es la entrada que justifica por qué el rastro
    # financiero de esa organización sigue existiendo años después de que dejó de
    # operar: el asiento se escribe antes de revocar los accesos, y por eso tiene que
    # poder emitirse aunque la revocación falle a mitad.
    ORGANIZATION_DELETED = "ORGANIZATION_DELETED"


class AuditLogEntry(Base):
    """Entrada inmutable de auditoría.

    R4: la tabla `audit_log` es *append-only* y solo acepta `INSERT`. El trigger
    `trg_protect_audit_log_append_only` impide que una entrada se altere o se
    elimine, de modo que el rastro de un cambio de triaje conserva su valor
    probatorio.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_org_entity", "organization_id", "entity_type", "entity_id"),
        Index("ix_audit_log_org_created_at", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT por el mismo motivo que `credit_ledger`: el rastro forense no
        # desaparece con la organización que lo originó. El tenant se da de baja
        # lógicamente y su auditoría se conserva.
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[AuditActionEnum] = mapped_column(
        SQLEnum(AuditActionEnum, name="audit_action_enum"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
