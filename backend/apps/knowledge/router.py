"""Endpoints de solo lectura del catálogo técnico de remediación."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.models import (
    KnowledgeCategoryEnum,
    KnowledgeEntry,
    KnowledgeSeverityEnum,
)
from backend.apps.knowledge.schemas import KnowledgeDetail, KnowledgePage, KnowledgeSummary
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

router = APIRouter(tags=["knowledge"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


@router.get("/api/v1/knowledge/", response_model=KnowledgePage)
async def list_knowledge_entries(
    _tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    category: Annotated[KnowledgeCategoryEnum | None, Query()] = None,
    severity: Annotated[KnowledgeSeverityEnum | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=256)] = None,
) -> KnowledgePage:
    """Catálogo de remediación filtrable por categoría, severidad y texto libre.

    El contenido es una referencia compartida, no un recurso del tenant, pero la
    ruta sí exige autenticación: el catálogo describe técnicas ofensivas y no
    debe ser un documento público.
    """

    filters = []
    if category is not None:
        filters.append(KnowledgeEntry.category == category)
    if severity is not None:
        filters.append(KnowledgeEntry.severity == severity)
    if search:
        pattern = f"%{search.strip().lower()}%"
        filters.append(
            or_(
                func.lower(KnowledgeEntry.title).like(pattern),
                func.lower(KnowledgeEntry.reference_code).like(pattern),
                func.lower(KnowledgeEntry.risk_summary).like(pattern),
                func.lower(KnowledgeEntry.owasp_category).like(pattern),
            )
        )

    total_result = await session.execute(
        select(func.count()).select_from(KnowledgeEntry).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(KnowledgeEntry)
        .where(*filters)
        .order_by(KnowledgeEntry.severity, KnowledgeEntry.reference_code)
        .limit(limit)
        .offset(offset)
    )
    return KnowledgePage(
        items=[KnowledgeSummary.model_validate(entry) for entry in result.scalars().all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/knowledge/{entry_id}", response_model=KnowledgeDetail)
async def get_knowledge_entry(
    entry_id: UUID,
    _tenant: TenantDependency,
    session: SessionDependency,
) -> KnowledgeDetail:
    """Devuelve el apunte completo con los ejemplos vulnerable y seguro."""

    result = await session.execute(select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id))
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Apunte de conocimiento no encontrado",
        )
    return KnowledgeDetail.model_validate(entry)
