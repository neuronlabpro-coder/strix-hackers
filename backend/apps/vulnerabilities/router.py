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
from backend.apps.vulnerabilities.remediation import (
    NoModelAvailableError,
    NoRepositoryLinkedError,
    PublicarRemediationError,
    RemediationError,
    generar_y_publicar,
)
from backend.apps.vulnerabilities.schemas import (
    AutofixRequest,
    AutofixResponse,
    RemediationResponse,
    VulnerabilityDetail,
    VulnerabilityListItem,
    VulnerabilityPage,
    VulnerabilityTriageRequest,
    VulnerabilityTriageResponse,
)
from backend.apps.webhooks.emission import (
    EventType,
    publish_event,
    vulnerability_status_payload,
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
    await publish_event(
        session,
        EventType.VULNERABILITY_STATUS_CHANGED,
        tenant.organization.id,
        vulnerability_status_payload(
            vulnerability_id=vulnerability.id,
            previous_status=previous_status.value,
            new_status=payload.status.value,
            severity=vulnerability.severity,
            title=vulnerability.title,
        ),
    )
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
    "/api/v1/vulnerabilities/{vulnerability_id}/remediate",
    response_model=RemediationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_autofix_rate_limit)],
)
async def propose_remediation(
    vulnerability_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> RemediationResponse:
    """Genera la corrección con el modelo de la cadena `AUTOFIX` y abre la pull request.

    ## Por qué es una ruta nueva y no un campo más de `create-fix-pr`

    Porque hacen cosas distintas y cobro distinto. `create-fix-pr` publica un diff **que ya
    está en la base** —lo trajo el motor durante el escaneo— y no cuesta tokens. Esta genera
    el diff, que sí los gasta, y por eso tiene su propio límite de tasa y su propio código de
    error cuando el modelo no responde.

    Fusionarlas habría dejado un endpoint cuyo comportamiento —y cuyo precio— depende de si
    el campo `autofix_patch_diff` viene nulo, y eso no lo puede expresar un `422`.

    ## Por qué la respuesta lleva la URL y no el diff

    Por R4. El diff es evidencia inmutable y se lee desde la vulnerabilidad; devolverlo en la
    respuesta de la acción lo expondría en cualquier log intermedio sin aportar nada.
    """

    if tenant.role != RoleEnum.ADMIN and not tenant.user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador",
        )

    # El filtro de organización va en el `WHERE` junto al `id`, nunca después: traer la fila
    # de otro workspace a memoria para decidir que no es suya es R3 resuelto tarde.
    encontrado = await session.execute(
        select(Vulnerability).where(
            Vulnerability.id == vulnerability_id,
            Vulnerability.organization_id == tenant.organization.id,
        )
    )
    vulnerability = encontrado.scalar_one_or_none()
    if vulnerability is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vulnerabilidad no encontrada"
        )

    try:
        url = await generar_y_publicar(session, vulnerability)
    except NoRepositoryLinkedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except NoModelAvailableError as error:
        # No es `503` sino `422`: el servicio está sano, lo que falta es configuración. Un
        # `503` haría que un balanceador lo interpretara como una caída y dejara de enviar
        # tráfico, y no hay nada que balancear.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    except PublicarRemediationError as error:
        # El consumo **ya** se cobró: los tokens se gastaron aunque la PR no se abriera. El
        # mensaje lo dice para que la interfaz no ofrezca "reintentar" como si fuera gratis.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)
        ) from error
    except RemediationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error

    return RemediationResponse(
        vulnerability_id=vulnerability.id,
        remediation_pr_url=url,
        status=IssueStatusEnum.REMEDIATION_PROPOSED,
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
