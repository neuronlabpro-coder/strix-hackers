"""Endpoint de solo lectura que alimenta el dashboard principal del panel."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.dashboard.schemas import DashboardSummaryResponse
from backend.apps.dashboard.service import build_dashboard_summary
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


@router.get("/api/v1/dashboard/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    tenant: TenantDependency,
    session: SessionDependency,
) -> DashboardSummaryResponse:
    """Agrega KPIs de postura y estado de repositorios solo de la organización activa."""

    return await build_dashboard_summary(session, tenant.organization.id)
