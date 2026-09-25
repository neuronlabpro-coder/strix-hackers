"""Esquemas del rastro de auditoría."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.audit.models import AuditActionEnum


class AuditLogEntryResponse(BaseModel):
    """Entrada de auditoría expuesta al panel."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    actor_user_id: UUID | None
    action: AuditActionEnum
    entity_type: str
    entity_id: UUID
    from_state: str | None
    to_state: str | None
    created_at: datetime


class AuditLogPage(BaseModel):
    """Página de entradas de auditoría acotada a una organización."""

    items: list[AuditLogEntryResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
