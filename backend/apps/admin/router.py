"""Endpoints de la consola de SuperAdmin."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.admin.schemas import (
    AdminOrganizationItem,
    AdminOrganizationPage,
    InfrastructureHealthResponse,
)
from backend.apps.admin.service import check_infrastructure
from backend.apps.organizations.models import Organization
from backend.core.database import get_db

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]


@router.get("/organizations", response_model=AdminOrganizationPage)
async def list_organizations(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminOrganizationPage:
    """Inventario global de organizaciones con plan y saldo de créditos."""

    total_result = await session.execute(
        select(func.count()).select_from(Organization)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(Organization)
        .order_by(Organization.created_at.desc(), Organization.id)
        .limit(limit)
        .offset(offset)
    )
    return AdminOrganizationPage(
        items=[
            AdminOrganizationItem.model_validate(organization)
            for organization in result.scalars().all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/health", response_model=InfrastructureHealthResponse)
async def read_infrastructure(
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> InfrastructureHealthResponse:
    """Sondea PostgreSQL y Redis sin exponer credenciales ni endpoints."""

    return await check_infrastructure(session)
