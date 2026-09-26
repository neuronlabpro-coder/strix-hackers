"""Endpoints de la consola de SuperAdmin."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.admin.schemas import (
    AdminOrganizationItem,
    AdminOrganizationPage,
    InfrastructureHealthResponse,
)
from backend.apps.admin.service import check_infrastructure
from backend.apps.llm_router.models import LLMModelConfig
from backend.apps.llm_router.schemas import (
    LLMModelCreate,
    LLMModelPage,
    LLMModelResponse,
    LLMModelUpdate,
    LLMUsageMetrics,
)
from backend.apps.llm_router.service import DuplicateLLMModelError, empty_usage, usage_metrics
from backend.apps.organizations.models import Organization
from backend.core.database import get_db

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]


def _llm_response(
    model: LLMModelConfig, usage: LLMUsageMetrics | None = None
) -> LLMModelResponse:
    """Construye la fila del catálogo con sus métricas de consumo.

    `usage` no viene del modelo sino de un agregado aparte, así que se valida el
    modelo sin ese campo y se inyecta después.
    """

    fields = LLMModelResponse.model_fields
    payload = {
        key: getattr(model, key)
        for key in fields
        if key != "usage" and hasattr(model, key)
    }
    return LLMModelResponse(**payload, usage=usage or empty_usage())


@router.get("/llm/", response_model=LLMModelPage)
async def list_llm_models(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> LLMModelPage:
    """Catálogo de modelos de lenguaje con el consumo real de cada uno."""

    total_result = await session.execute(
        select(func.count()).select_from(LLMModelConfig)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(LLMModelConfig)
        .order_by(LLMModelConfig.priority_order, LLMModelConfig.model_id)
        .limit(limit)
        .offset(offset)
    )
    models = list(result.scalars().all())
    metrics = await usage_metrics(session, [model.id for model in models])
    return LLMModelPage(
        items=[_llm_response(model, metrics.get(model.id)) for model in models],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/llm/", response_model=LLMModelResponse, status_code=201)
async def create_llm_model(
    payload: LLMModelCreate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> LLMModelResponse:
    """Da de alta un modelo de OpenRouter."""

    from backend.apps.llm_router.service import create_model

    try:
        model = await create_model(
            session,
            model_id=payload.model_id,
            display_name=payload.display_name,
            base_cost_input_m=payload.base_cost_input_m,
            base_cost_output_m=payload.base_cost_output_m,
            markup_pct=payload.markup_pct,
            priority_order=payload.priority_order,
            is_active=payload.is_active,
            use_case=payload.use_case,
        )
    except DuplicateLLMModelError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ese modelo ya está en el catálogo",
        ) from error
    await session.commit()
    await session.refresh(model)
    return _llm_response(model)


@router.patch("/llm/{model_id}", response_model=LLMModelResponse)
async def update_llm_model(
    model_id: UUID,
    payload: LLMModelUpdate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> LLMModelResponse:
    """Ajusta margen, prioridad o estado activo de un modelo.

    `model_id` no es mutable a propósito: es la identidad que aparece en los logs
    de consumo y en la configuración que se inyecta a los contenedores. Renombrar
    un modelo dejaría registros históricos sin modelo al que atribuirlos.
    """

    result = await session.execute(
        select(LLMModelConfig).where(LLMModelConfig.id == model_id)
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Modelo no encontrado"
        )

    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Envía al menos un campo para modificar",
        )
    for field_name, value in changes.items():
        setattr(model, field_name, value)
    await session.commit()
    await session.refresh(model)
    metrics = await usage_metrics(session, [model.id])
    return _llm_response(model, metrics.get(model.id))


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
