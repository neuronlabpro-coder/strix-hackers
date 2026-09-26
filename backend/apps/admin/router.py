"""Endpoints de la consola de SuperAdmin.

## Por qué estas rutas no aceptan un tenant

Toda la consola opera **sobre la plataforma**, no sobre el workspace de alguien. Por eso
no usan `get_current_tenant` y no aceptan `X-Organization-Id`: un superusuario que
consulta `/admin/organizations` está mirando la lista completa, y el filtro por tenant
sería la forma de hacer que esa lista pareciera acotada sin serlo.

La contrapartida es que estas rutas **no** llevan el aislamiento R3 por tenant, y por eso
exigen `is_superuser` en cada una. La dependencia es explícita y no se hereda del router,
para que añadir una ruta nueva no la haga pública por descuido.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin import queries
from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.admin.schemas import (
    AdminAuditPage,
    AdminCreditGrant,
    AdminCreditGrantResult,
    AdminOrganizationItem,
    AdminOrganizationPage,
    AdminOrganizationPlanUpdate,
    AdminOverviewResponse,
    AdminSalePage,
    AdminUserItem,
    AdminUserPage,
    AdminUserUpdate,
    InfrastructureHealthResponse,
)
from backend.apps.admin.service import check_infrastructure
from backend.apps.audit.models import AuditActionEnum
from backend.apps.llm_router.models import LLMModelConfig
from backend.apps.llm_router.schemas import (
    LLMModelCreate,
    LLMModelPage,
    LLMModelResponse,
    LLMModelUpdate,
    LLMUsageMetrics,
)
from backend.apps.llm_router.service import DuplicateLLMModelError, empty_usage, usage_metrics
from backend.apps.organizations.models import Organization, PlanTierEnum, User
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


@router.get("/overview", response_model=AdminOverviewResponse)
async def read_overview(
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> AdminOverviewResponse:
    """Resumen global de la plataforma con las seis metricas de cabecera.

    Va en una sola respuesta y no seis: el panel pinta una fila de tarjetas y seis
    peticiones en paralelo se ven como un panel que tarda en aparecer y parpadea mientras
    llegan. El sondeo de infraestructura viene dentro por el mismo motivo.
    """

    now = datetime.now(UTC)
    infrastructure = await check_infrastructure(session, now=now)
    return AdminOverviewResponse(
        metrics=await queries.build_overview(session, now, infrastructure),
        infrastructure=infrastructure,
        generated_at=now,
    )


@router.get("/tenants", response_model=AdminOrganizationPage)
async def list_organizations(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    plan: Annotated[PlanTierEnum | None, Query()] = None,
    lifecycle: Annotated[
        str | None,
        Query(description="active, deleted o deactivated. Sin valor: todos."),
    ] = None,
    search: Annotated[
        str | None, Query(description="Coincidencia parcial sobre nombre o slug.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminOrganizationPage:
    """Inventario global de tenants, con filtros de plan, estado y busqueda.

    Se conservan los nombres de los parametros que ya existian y se anaden los nuevos, en
    lugar de reemplazarlos. Un cliente que ya llama a esta ruta con `limit` y `offset`
    sigue funcionando, y cambiar el contrato de una ruta en produccion sin motivo es la
    forma de romper algo que no habia que tocar.
    """

    if lifecycle is not None and lifecycle not in {"active", "deleted", "deactivated"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "El estado solo admite 'active', 'deleted' o 'deactivated'. "
                f"Se recibio {lifecycle!r}."
            ),
        )
    return await queries.list_organizations(
        session,
        plan=plan,
        lifecycle=lifecycle,
        search=search,
        limit=limit,
        offset=offset,
    )


async def _find_organization(
    session: AsyncSession, organization_id: UUID
) -> Organization:
    """Resuelve un tenant de la plataforma, o `404`.

    Un superusuario puede tocar cualquier tenant, así que aquí no hay filtro por
    organización: el `404` significa que ese identificador no existe, y no que el
    superusuario no tenga permiso sobre él.
    """

    organization = (
        await session.execute(
            select(Organization).where(Organization.id == organization_id)
        )
    ).scalar_one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organización no encontrada",
        )
    return organization


@router.patch("/tenants/{organization_id}", response_model=AdminOrganizationItem)
async def update_organization(
    organization_id: UUID,
    payload: AdminOrganizationPlanUpdate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> AdminOrganizationItem:
    """Cambia el plan de un tenant."""

    organization = await _find_organization(session, organization_id)
    return await queries.set_organization_plan(session, organization, payload.plan_tier)


@router.post(
    "/tenants/{organization_id}/credits",
    response_model=AdminCreditGrantResult,
    status_code=status.HTTP_201_CREATED,
)
async def grant_organization_credits(
    organization_id: UUID,
    payload: AdminCreditGrant,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> AdminCreditGrantResult:
    """Acredita creditos de forma administrativa.

    El motivo del asiento lo fija el servidor en `ADMIN_ADJUSTMENT` y el endpoint **no lo
    acepta del cliente**: un endpoint que dejara escribir `STRIPE_PURCHASE` permitiria
    forjar un asiento con la misma clase que una compra real, y el ledger es la prueba de
    que hubo un cobro. La nota libre va aparte y es interna.

    ## Por qué el importe no puede ser negativo

    Un ajuste a la baja es una devolucion, y esa es otra operacion con otra autorizacion y
    otro asiento. Permitir negativos aqui daria dos caminos para la misma accion distinta
    y el mas facil de auditar es el que no existe.
    """

    organization = await _find_organization(session, organization_id)
    if organization.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No se pueden acreditar creditos a una organización dada de baja. Reactívela"
                " primero."
            ),
        )
    return await queries.grant_credits(
        session, organization, payload.amount, payload.note
    )


@router.delete(
    "/tenants/{organization_id}", response_model=AdminOrganizationItem
)
async def deactivate_organization(
    organization_id: UUID,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> AdminOrganizationItem:
    """Da de baja logicamente un tenant y revoca su acceso.

    Reutiliza el servicio de baja del modulo de organizaciones en vez de reimplementarlo.
    La version de aqui no acepta el `X-Organization-Id` del cliente porque opera sobre
    cualquier tenant, asi que resuelve la fila y se la pasa. El asiento de auditoria, la
    revocacion de membresias y el contador vuelven a escribirse en un unico sitio, que es
    lo unico que evita que las dos rutas diverjan con el tiempo.
    """

    from backend.apps.organizations.deletion import _soft_delete_organization

    organization = await _find_organization(session, organization_id)
    if organization.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La organización ya estaba dada de baja",
        )
    report = await _soft_delete_organization(session, organization, _superuser.id)
    await session.refresh(organization)
    page = await queries.list_organizations(session, search=organization.slug, limit=1)
    if page.items:
        return page.items[0]
    del report
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="La baja se aplico pero el tenant no se pudo releer",
    )


@router.get("/users", response_model=AdminUserPage)
async def list_users(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    search: Annotated[str | None, Query()] = None,
    only_superusers: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminUserPage:
    """Listado global de usuarios con sus workspaces.

    No expone el hash de contrasena ni el token de verificacion: la respuesta se construye
    con una lista explicita de campos, asi que anadir una columna sensible a `User` no la
    anade a la vista.
    """

    return await queries.list_users(
        session,
        search=search,
        only_superusers=only_superusers,
        limit=limit,
        offset=offset,
    )


@router.patch("/users/{user_id}", response_model=AdminUserItem)
async def update_user(
    user_id: UUID,
    payload: AdminUserUpdate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> AdminUserItem:
    """Activa o desactiva una cuenta, y le quita o le da el superusuario.

    ## Por qué un `404` y no un `403` cuando no existe

    La respuesta delata lo mismo que cualquier `404` de la plataforma: ese identificador no
    corresponde a ninguna cuenta. Un superusuario **sí** puede tocar cualquier cuenta, así
    que aquí la frontera no es el aislamiento por tenant sino el propio permiso.

    ## Por qué no se puede quitar el superusuario al último

    Ver `queries.LastSuperuserError`. La operación no tiene vuelta atrás desde la propia
    consola, y el `409` lo dice en vez de dejar al operador descubriéndolo después.
    """

    usuario = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if usuario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado"
        )

    try:
        return await queries.set_user_flags(
            session,
            usuario,
            is_active=payload.is_active,
            is_superuser=payload.is_superuser,
        )
    except queries.LastSuperuserError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error


@router.get("/sales", response_model=AdminSalePage)
async def list_sales(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    organization_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminSalePage:
    """Historial de transacciones procesadas por Stripe."""

    return await queries.list_sales(
        session, organization_id=organization_id, limit=limit, offset=offset
    )


@router.get("/audit", response_model=AdminAuditPage)
async def list_audit(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    organization_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query(description="Nombre exacto de la acción.")] = None,
    search: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminAuditPage:
    """Visor global del rastro forense de toda la plataforma.

    Solo lectura. La tabla es *append-only* por R4 y la unica operacion de esta consola
    que la modifica es la baja de organizacion, que escribe su propio asiento.

    ## Por qué una acción desconocida es `422` y no un `200` vacío

    La columna es un `ENUM` de PostgreSQL. Si el texto se pasa tal cual, la base lanza
    `invalid input value for enum` y la petición muere con `500`, que es la peor de las
    tres respuestas posibles para alguien que escribe mal un filtro.

    Devolver cero resultados tampoco vale: haría creer que no hubo ninguna entrada con esa
    acción, cuando lo cierto es que esa acción no existe. El `422` enumera las válidas, que
    es lo que el operador necesita para corregir el filtro sin abrir el código.
    """

    filtro_accion: AuditActionEnum | None = None
    if action is not None:
        try:
            filtro_accion = AuditActionEnum(action)
        except ValueError as error:
            validas = ", ".join(sorted(valor.value for valor in AuditActionEnum))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"La acción '{action}' no existe. Acciones válidas: {validas}."
                ),
            ) from error

    return await queries.list_audit(
        session,
        organization_id=organization_id,
        action=filtro_accion,
        search=search,
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
