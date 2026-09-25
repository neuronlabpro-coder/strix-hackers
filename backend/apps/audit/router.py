"""Endpoints de solo lectura del rastro de auditoría."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditLogEntry
from backend.apps.audit.schemas import AuditLogEntryResponse, AuditLogPage
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

router = APIRouter(tags=["audit"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


@router.get("/api/v1/audit-log/", response_model=AuditLogPage)
async def list_audit_log(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    entity_type: Annotated[str | None, Query(max_length=64)] = None,
    entity_id: Annotated[UUID | None, Query()] = None,
) -> AuditLogPage:
    """Historial de auditoría de la organización activa, del más reciente al más antiguo.

    MENU-MAP §3.4 pide historial inmutable de cambios de estado en la ficha del
    hallazgo. La tabla es append-only (R4): esta ruta no puede alterar el rastro,
    solo leerlo, y filtra por `organization_id` como cualquier otra lectura (R3).
    """

    filters = [AuditLogEntry.organization_id == tenant.organization.id]
    if entity_type is not None:
        filters.append(AuditLogEntry.entity_type == entity_type)
    if entity_id is not None:
        filters.append(AuditLogEntry.entity_id == entity_id)

    total_result = await session.execute(
        select(func.count()).select_from(AuditLogEntry).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(AuditLogEntry)
        .where(*filters)
        .order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return AuditLogPage(
        items=[
            AuditLogEntryResponse.model_validate(entry) for entry in result.scalars().all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
