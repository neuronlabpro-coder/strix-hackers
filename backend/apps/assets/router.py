"""Endpoints de la superficie de ataque: dominios verificados y activos descubiertos.

## Por qué los filtros de tenant no se repiten en cada ruta

Porque `TenantDependency` los aplica en la dependencia, no en el cuerpo de la función. Una
ruta que acepta un `organization_id` en la URL tiene que compararlo con el del contexto para
no filtrar nada, y esa comparación es un sitio donde olvidarse no da error: da datos. Aquí no
hay ningún `organization_id` en ninguna ruta, así que no hay nada que olvidar.

La excepción es el `domain_id` de la ruta, que es un **recurso** del tenant y no una
selección: se resuelve siempre junto al `organization_id` en el `WHERE`, y si no coincide
devuelve `404`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.assets import service
from backend.apps.assets.models import AssetTypeEnum
from backend.apps.assets.schemas import (
    AssetListResponse,
    DiscoveryEnqueuedResponse,
    DomainConflictResponse,
    DomainCreate,
    DomainItem,
    DomainListResponse,
    VerifyDomainResponse,
)
from backend.apps.organizations.models import RoleEnum
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

router = APIRouter(prefix="/api/v1/assets", tags=["assets"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


# La regla vive en `core/middleware.exigir_admin_del_tenant` y la comparten `assets`, `agents`,
# `knowledge` y `supply_chain`. Antes había una copia por router, y la diferencia entre
# `!=` y `is not` entre ellas ya era una divergencia real: un `"ADMIN"` de tipo `str` pasaba en
# una copia y no en la otra.
async def _exigir_admin(tenant: TenantDependency) -> None:
    """Exige rol `ADMIN` del tenant para las operaciones de escritura.

    ## Por qué esta indirección y no llamar directamente a la de `core`

    Porque la de `core` es una dependencia de FastAPI y aquí se llama en el **cuerpo** de la
    ruta, después de que la ruta ya ha hecho su trabajo. Reenviarla mantiene el mensaje de
    error específico de la superficie de ataque —que es más útil que el genérico— sin
    reescribir la comparación, que es la parte que puede estar mal.
    """

    if tenant.role is not RoleEnum.ADMIN and not tenant.user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador para gestionar la superficie de ataque",
        )


def _no_encontrado(error: service.DomainNotFoundError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
    )


@router.post("/domains", response_model=DomainItem, status_code=status.HTTP_201_CREATED)
async def create_domain(
    payload: DomainCreate,
    tenant: TenantDependency,
    session: SessionDependency,
) -> DomainItem:
    """Da de alta un dominio y devuelve su registro TXT listo para publicar.

    El `409` cuando el nombre ya está reclamado por otro workspace es la regla que protege
    la propiedad del dominio; ver la nota de `service.DomainConflictError`.
    """

    await _exigir_admin(tenant)
    try:
        dominio = await service.create_domain(
            session, tenant.organization.id, payload
        )
    except service.DomainConflictError as error:
        # El motivo viaja estructurado. El panel lo necesita para ofrecer "abrir el
        # dominio" cuando el conflicto es con uno propio, y un mensaje cuando es con el de
        # otro: interpretando el texto, esa decisión quedaría atada a un idioma.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DomainConflictResponse(
                detail=str(error),
                motivo=error.motivo,
                already_verified=error.already_verified,
                domain_name=payload.domain_name,
            ).model_dump(mode="json"),
        ) from error
    except service.AssetError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error

    return service.build_domain_item(dominio, 0)


@router.get("/domains", response_model=DomainListResponse)
async def list_domains(
    tenant: TenantDependency,
    session: SessionDependency,
) -> DomainListResponse:
    """Los dominios del workspace activo, verificados primero."""

    items = await service.list_domains(session, tenant.organization.id)
    return DomainListResponse(items=items, total=len(items))


@router.post("/domains/{domain_id}/verify", response_model=VerifyDomainResponse)
async def verify_domain(
    domain_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> VerifyDomainResponse:
    """Comprueba el registro TXT del dominio y lo marca verificado si coincide.

    Devuelve `200` tanto si coincide como si no. Un `409` por "no verificado" convertiría
    un resultado esperado —que casi siempre es "todavía no lo he publicado"— en un error, y
    el panel no podría distinguir un `NO_TXT` normal de un fallo de red mirando el código.
    El resultado va en el cuerpo, y el código dice que la comprobación se ha hecho.
    """

    await _exigir_admin(tenant)
    try:
        _, resultado = await service.verify_domain(
            session, tenant.organization.id, domain_id
        )
    except service.DomainNotFoundError as error:
        raise _no_encontrado(error) from error

    return VerifyDomainResponse(
        outcome=resultado.outcome,
        is_verified=resultado.verified,
        expected_value=resultado.expected_value,
        found_values=list(resultado.found_values),
        message_key=resultado.message_key(),
    )


@router.delete("/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_domain(
    domain_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> None:
    """Borra un dominio **no verificado** y sus activos en cascada.

    Un dominio verificado no se borra: es parte del inventario que el cliente ya pagó, y
    borrarlo sin dejar rastro sería hacer desaparecer la superficie de ataque registrada.
    """

    await _exigir_admin(tenant)
    try:
        await service.delete_domain(session, tenant.organization.id, domain_id)
    except service.DomainNotFoundError as error:
        raise _no_encontrado(error) from error
    except service.AssetError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error


@router.get("/discovery", response_model=AssetListResponse)
async def list_assets(
    tenant: TenantDependency,
    session: SessionDependency,
    domain_id: Annotated[UUID | None, Query()] = None,
    asset_type: Annotated[AssetTypeEnum | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AssetListResponse:
    """Activos descubiertos del workspace, filtrables por dominio y por tipo."""

    return await service.list_assets(
        session,
        tenant.organization.id,
        domain_id=domain_id,
        asset_type=asset_type,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/domains/{domain_id}/discover",
    response_model=DiscoveryEnqueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_discovery(
    domain_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> DiscoveryEnqueuedResponse:
    """Encola el descubrimiento de activos de un dominio **verificado**.

    ## Por qué `400` y no `409` cuando el dominio no está verificado

    Porque la petición está bien formada y apunta a un recurso real; lo que no se cumple es
    un **requisito previo** del dominio. Un `409` reserva para "el estado actual del recurso
    choca con la operación", que es lo que sería un `delete` sobre un dominio verificado.
    Aquí lo que falla es que el dominio todavía no cumple lo que hace falta para escanearlo, y
    el mensaje lo dice: hay que verificar primero.

    ## Por qué `202` y no `200`

    Porque el trabajo no está hecho. Devolver `200` haría que un cliente que reintenta
    idempotentemente creyera que ya está encolado y no lo volviera a lanzar; `202` dice que
    se aceptó y que el resultado llega por otro lado. El identificador de tarea permite
    seguirlo.
    """

    await _exigir_admin(tenant)
    try:
        dominio = await service.get_domain(session, tenant.organization.id, domain_id)
    except service.DomainNotFoundError as error:
        raise _no_encontrado(error) from error

    if not dominio.is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El dominio debe estar verificado antes de lanzar el descubrimiento",
        )

    from backend.apps.assets.tasks import discover_domain_assets

    tarea = discover_domain_assets.delay(  # pyright: ignore[reportFunctionMemberAccess]
        str(dominio.id), str(dominio.organization_id)
    )
    existentes = await service.contar_activos(session, dominio.id)

    return DiscoveryEnqueuedResponse(
        task_id=str(tarea.id),
        domain_name=dominio.domain_name,
        existing_assets=existentes,
    )


__all__ = ["router"]
