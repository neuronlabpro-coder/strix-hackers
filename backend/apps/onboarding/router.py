"""Endpoint del progreso de onboarding."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.onboarding.schemas import OnboardingResponse
from backend.apps.onboarding.service import build_onboarding_status
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

router = APIRouter(tags=["onboarding"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


@router.get("/api/v1/onboarding/status", response_model=OnboardingResponse)
async def read_onboarding_status(
    tenant: TenantDependency,
    session: SessionDependency,
) -> OnboardingResponse:
    """Progreso del checklist `Get Set Up` de MENU-MAP §1.1 acotado al tenant."""

    return await build_onboarding_status(session, tenant.organization.id)
