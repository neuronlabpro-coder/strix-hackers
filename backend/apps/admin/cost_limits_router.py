"""Topes de coste del proveedor y vista previa para SuperAdmin."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin.cost_limit_schemas import CostLimitCreate, CostLimitResponse
from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.llm_router.cost_limits import limites_de
from backend.apps.llm_router.models import LLMCostLimit, NivelLimiteCosteEnum, OperacionCosteEnum
from backend.apps.organizations.models import Organization
from backend.core.config import settings
from backend.core.database import get_db

router = APIRouter(prefix="/api/v1/admin/cost-limits", tags=["admin"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]


@router.get("/", response_model=list[CostLimitResponse])
async def list_cost_limits(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    scope: NivelLimiteCosteEnum | None = None,
    organization_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[CostLimitResponse]:
    consulta = select(LLMCostLimit)
    if scope is not None:
        consulta = consulta.where(LLMCostLimit.scope == scope)
    if organization_id is not None:
        consulta = consulta.where(LLMCostLimit.organization_id == organization_id)
    filas = await session.execute(
        consulta.order_by(LLMCostLimit.valid_from.desc(), LLMCostLimit.id).limit(limit)
    )
    return [CostLimitResponse.model_validate(fila) for fila in filas.scalars()]


@router.get("/preview")
async def preview_cost_limits(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    organization_id: UUID,
    operation: OperacionCosteEnum,
) -> dict[str, object]:
    existe = await session.scalar(
        select(Organization.id).where(Organization.id == organization_id)
    )
    if existe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organización no encontrada"
        )
    resolucion = await limites_de(
        session,
        organization_id=organization_id,
        operation=operation,
        default_max_budget_usd=settings.strix_max_budget_usd,
        default_max_turns=settings.strix_max_turns,
    )
    return resolucion.a_json()


@router.post("/", response_model=CostLimitResponse, status_code=201)
async def create_cost_limit(
    payload: CostLimitCreate,
    superuser: SuperuserDependency,
    session: SessionDependency,
) -> CostLimitResponse:
    if payload.organization_id is not None:
        existe = await session.scalar(
            select(Organization.id).where(Organization.id == payload.organization_id)
        )
        if existe is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Organización no encontrada"
            )
    desde = payload.valid_from or datetime.now(UTC)
    if payload.valid_until is not None and payload.valid_until <= desde:
        raise HTTPException(status_code=422, detail="valid_until debe ser posterior a valid_from")
    fila = LLMCostLimit(
        scope=payload.scope,
        organization_id=payload.organization_id,
        operation=payload.operation,
        plan_tier=payload.plan_tier,
        max_budget_usd=payload.max_budget_usd,
        max_turns=payload.max_turns,
        valid_from=desde,
        valid_until=payload.valid_until,
        created_by=superuser.id,
    )
    session.add(fila)
    await session.commit()
    await session.refresh(fila)
    return CostLimitResponse.model_validate(fila)


@router.patch("/{policy_id}", response_model=CostLimitResponse)
async def replace_cost_limit(
    policy_id: UUID,
    payload: CostLimitCreate,
    superuser: SuperuserDependency,
    session: SessionDependency,
) -> CostLimitResponse:
    actual = await session.get(LLMCostLimit, policy_id)
    if actual is None:
        raise HTTPException(status_code=404, detail="Política no encontrada")
    ahora = datetime.now(UTC)
    if actual.valid_from > ahora:
        raise HTTPException(status_code=409, detail="La política aún no está vigente")
    if actual.valid_until is not None and actual.valid_until <= ahora:
        raise HTTPException(status_code=409, detail="La política ya ha caducado")
    if payload.valid_from is not None and payload.valid_from > ahora:
        raise HTTPException(status_code=422, detail="La edición debe entrar en vigor ahora")
    if payload.organization_id is not None:
        existe = await session.scalar(
            select(Organization.id).where(Organization.id == payload.organization_id)
        )
        if existe is None:
            raise HTTPException(status_code=404, detail="Organización no encontrada")
    if payload.valid_until is not None and payload.valid_until <= ahora:
        raise HTTPException(
            status_code=422, detail="valid_until debe ser posterior al momento actual"
        )
    actual.valid_until = ahora
    nueva = LLMCostLimit(
        scope=payload.scope,
        organization_id=payload.organization_id,
        operation=payload.operation,
        plan_tier=payload.plan_tier,
        max_budget_usd=payload.max_budget_usd,
        max_turns=payload.max_turns,
        valid_from=ahora,
        valid_until=payload.valid_until,
        created_by=superuser.id,
    )
    session.add(nueva)
    await session.commit()
    await session.refresh(nueva)
    return CostLimitResponse.model_validate(nueva)


@router.delete("/{policy_id}", status_code=204)
async def delete_cost_limit(
    policy_id: UUID,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> None:
    actual = await session.get(LLMCostLimit, policy_id)
    if actual is None:
        raise HTTPException(status_code=404, detail="Política no encontrada")
    ahora = datetime.now(UTC)
    if actual.valid_from > ahora:
        raise HTTPException(status_code=409, detail="La política aún no está vigente")
    if actual.valid_until is not None and actual.valid_until <= ahora:
        raise HTTPException(status_code=409, detail="La política ya ha caducado")
    actual.valid_until = ahora
    await session.commit()
