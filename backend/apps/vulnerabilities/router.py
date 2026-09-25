"""Endpoints de lectura paginada de vulnerabilidades por tenant."""

import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.organizations.models import RoleEnum
from backend.apps.repositories.autofix import AutofixError, create_autofix_branch_and_pr
from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.apps.vulnerabilities.schemas import (
    AutofixRequest,
    AutofixResponse,
    VulnerabilityDetail,
    VulnerabilityListItem,
    VulnerabilityPage,
    VulnerabilityTriageRequest,
    VulnerabilityTriageResponse,
)
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant
from backend.core.rate_limit import enforce_autofix_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
AutofixRateLimit = Annotated[None, Depends(enforce_autofix_rate_limit)]
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
    search: Annotated[str | None, Query(max_length=256)] = None,
) -> VulnerabilityPage:
    """Lista únicamente vulnerabilidades del tenant activo con filtros y paginación."""

    filters = [Vulnerability.organization_id == tenant.organization.id]
    if severity is not None:
        filters.append(Vulnerability.severity == severity)
    if vulnerability_status is not None:
        filters.append(Vulnerability.status == vulnerability_status)
    if target is not None:
        filters.append(Vulnerability.affected_target == target)
    if search:
        pattern = f"%{search.strip().lower()}%"
        filters.append(
            or_(
                func.lower(Vulnerability.title).like(pattern),
                func.lower(Vulnerability.affected_target).like(pattern),
                func.lower(Vulnerability.cve_id).like(pattern),
            )
        )

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


@router.patch(
    "/api/v1/vulnerabilities/{vulnerability_id}",
    response_model=VulnerabilityTriageResponse,
)
async def triage_vulnerability(
    vulnerability_id: UUID,
    payload: VulnerabilityTriageRequest,
    tenant: TenantDependency,
    session: SessionDependency,
) -> VulnerabilityTriageResponse:
    """Cambia únicamente el estado de remediación y deja rastro en el audit log.

    R4: las evidencias forenses son inmutables. El esquema solo admite `status` y
    rechaza con `422` cualquier otro campo, de modo que un cliente que intente
    reescribir la prueba de concepto nunca llega a la base de datos. El aislamiento
    R3 se aplica en la propia consulta: un hallazgo de otro tenant devuelve `404`,
    no `403`, para no confirmar la existencia de un identificador ajeno.
    """

    result = await session.execute(
        select(Vulnerability)
        .where(
            Vulnerability.id == vulnerability_id,
            Vulnerability.organization_id == tenant.organization.id,
        )
        .with_for_update()
    )
    vulnerability = result.scalar_one_or_none()
    if vulnerability is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vulnerabilidad no encontrada",
        )

    if vulnerability.status == payload.status:
        return VulnerabilityTriageResponse(
            id=vulnerability.id,
            status=vulnerability.status,
            updated_at=vulnerability.updated_at,
            changed=False,
        )

    previous_status = vulnerability.status
    vulnerability.status = payload.status
    session.add(
        AuditLogEntry(
            organization_id=tenant.organization.id,
            actor_user_id=tenant.user.id,
            action=AuditActionEnum.STATUS_CHANGED,
            entity_type="vulnerability",
            entity_id=vulnerability.id,
            from_state=previous_status.value,
            to_state=payload.status.value,
        )
    )
    await session.commit()
    await session.refresh(vulnerability)
    logger.info(
        "Triaje de vulnerabilidad %s: %s -> %s por usuario %s",
        vulnerability.id,
        previous_status.value,
        payload.status.value,
        tenant.user.id,
    )
    return VulnerabilityTriageResponse(
        id=vulnerability.id,
        status=vulnerability.status,
        updated_at=vulnerability.updated_at,
        changed=True,
    )


@router.post(
    "/api/v1/vulnerabilities/{vulnerability_id}/create-fix-pr",
    response_model=AutofixResponse,
)
async def create_fix_pr(
    vulnerability_id: UUID,
    payload: AutofixRequest,
    tenant: TenantDependency,
    session: SessionDependency,
    _autofix_rate_limit: AutofixRateLimit,
) -> AutofixResponse:
    """Crea una rama de autofix sin exponer el diff ni el token en la respuesta."""

    if tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador",
        )
    result = await session.execute(
        select(Vulnerability).where(
            Vulnerability.id == vulnerability_id,
            Vulnerability.organization_id == tenant.organization.id,
        )
    )
    vulnerability = result.scalar_one_or_none()
    if vulnerability is None or vulnerability.autofix_patch_diff is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vulnerabilidad o autofix no encontrado",
        )
    try:
        url = await asyncio.to_thread(
            create_autofix_branch_and_pr,
            str(payload.review_id),
            str(vulnerability.id),
        )
    except AutofixError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No se pudo crear el autofix",
        ) from error
    except Exception as error:
        logger.exception("Falló la creación del autofix para %s", vulnerability_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El proveedor Git no pudo crear el autofix",
        ) from error
    return AutofixResponse.model_validate({"autofix_url": url})
