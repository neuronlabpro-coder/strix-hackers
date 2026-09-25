"""Endpoints de lectura paginada de vulnerabilidades por tenant."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.apps.vulnerabilities.schemas import (
    VulnerabilityDetail,
    VulnerabilityListItem,
    VulnerabilityPage,
)
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=100_000)]
VulnerabilityStatus = Annotated[IssueStatusEnum | None, Query(alias="status")]
TargetFilter = Annotated[str | None, Query(max_length=512)]


@router.get("/api/v1/vulnerabilities/", response_model=VulnerabilityPage)
async def list_vulnerabilities(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
    severity: SeverityEnum | None = None,
    vulnerability_status: VulnerabilityStatus = None,
    target: TargetFilter = None,
) -> VulnerabilityPage:
    """Lista únicamente vulnerabilidades del tenant activo con filtros y paginación."""

    filters = [Vulnerability.organization_id == tenant.organization.id]
    if severity is not None:
        filters.append(Vulnerability.severity == severity)
    if vulnerability_status is not None:
        filters.append(Vulnerability.status == vulnerability_status)
    if target is not None:
        filters.append(Vulnerability.affected_target == target)

    total_result = await session.execute(
        select(func.count()).select_from(Vulnerability).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(Vulnerability)
        .where(*filters)
        .order_by(Vulnerability.discovered_at.desc(), Vulnerability.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [VulnerabilityListItem.model_validate(item) for item in result.scalars().all()]
    return VulnerabilityPage(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/api/v1/vulnerabilities/{vulnerability_id}",
    response_model=VulnerabilityDetail,
)
async def get_vulnerability(
    vulnerability_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> VulnerabilityDetail:
    """Devuelve el detalle y las evidencias de un hallazgo del tenant activo."""

    result = await session.execute(
        select(Vulnerability).where(
            Vulnerability.id == vulnerability_id,
            Vulnerability.organization_id == tenant.organization.id,
        )
    )
    vulnerability = result.scalar_one_or_none()
    if vulnerability is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vulnerabilidad no encontrada",
        )
    return VulnerabilityDetail.model_validate(vulnerability)
