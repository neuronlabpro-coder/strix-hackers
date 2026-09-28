"""Endpoints del inventario de dependencias (`/api/v1/supply-chain`).

## Qué expone y qué no

Expone **leer** el inventario y **indexar** un manifiesto que el cliente aporta. No expone
"sincronizar un repositorio", porque sincronizar significa traer el manifiesto del proveedor y
esa es una decisión sobre R5 que este módulo no toma. Ver el encabezado de `service.py`.

## Por qué el filtro por organización es **incondicional** en los dos endpoints

Porque R3 no es una recomendación, y el listado global es donde más fácil se colaría: sin el
filtro, un cliente vería los paquetes de su vecino, que es informacion sobre su stack.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.apps.supply_chain import service
from backend.apps.supply_chain.models import EcosystemEnum
from backend.apps.supply_chain.schemas import (
    SupplyChainIndexRequest,
    SupplyChainIndexResult,
    SupplyChainPackageItem,
    SupplyChainPackagePage,
    SupplyChainSummary,
)
from backend.core.middleware import (
    SessionDependency,
    TenantContext,
    get_current_tenant,
)

router = APIRouter(prefix="/api/v1/supply-chain", tags=["supply-chain"])

#: El tenant se resuelve por la cabecera `X-Organization-Id` y **se comprueba** contra la
#: membresía del usuario. No es una confianza en lo que dice la cabecera: es un selector que el
#: backend valida, y por eso enviar el identificador de otro workspace no da acceso a nada.
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]

PAGE_SIZE = 50


def _no_encontrado() -> HTTPException:
    """El `404` de estos endpoints.

    `404` y no `403`: un `403` confirmaría que el recurso existe, y con eso basta para enumerar
    identificadores y saber qué tiene el tenant vecino. El `404` no distingue "no existe" de
    "no es tuyo", que es lo único que no filtra información.
    """

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No encontrado")


@router.get("/packages", response_model=SupplyChainPackagePage)
async def listar_paquetes(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: Annotated[uuid.UUID | None, Query()] = None,
    ecosystem: Annotated[EcosystemEnum | None, Query()] = None,
    # `bool | None` y no `bool`: `None` es "no filtrar", y `true`/`false` son las dos
    # comprobaciones. Con un flag no caben las tres, y "solo las no comprobadas" es una pregunta
    # real. Ver el docstring de `listar_paquetes`.
    has_vulnerabilities: Annotated[bool | None, Query()] = None,
    solo_desarrollo: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SupplyChainPackagePage:
    """Las dependencias del workspace, filtrables.

    ## Por qué el orden pone primero lo que **no** se ha comprobado

    Porque un `ORDER BY` por nombre deja las dependencias sin verificar en el mismo plano que
    las comprobadas, y quien mira la tabla asume que todo lo que ve se sabe. Mandando delante
    lo desconocido, la primera pantalla dice la verdad sobre cuanto de lo que hay no se sabe, y
    el usuario no tiene que recorrer la tabla para averiguarlo.
    """

    filas, total = await service.listar_paquetes(
        session,
        tenant.organization.id,
        repository_id=repository_id,
        ecosystem=ecosystem,
        has_vulnerabilities=has_vulnerabilities,
        solo_desarrollo=solo_desarrollo,
        busqueda=search,
        limite=limit,
        offset=offset,
    )
    return SupplyChainPackagePage(
        items=[
            SupplyChainPackageItem(
                id=paquete.id,
                name=paquete.name,
                version=paquete.version,
                ecosystem=paquete.ecosystem,
                license=paquete.license,
                has_vulnerabilities=paquete.has_vulnerabilities,
                cve_ids=list(paquete.cve_ids or []),
                is_dev_dependency=paquete.is_dev_dependency,
                manifest_path=paquete.manifest_path,
                first_seen_at=paquete.first_seen_at,
                last_seen_at=paquete.last_seen_at,
                repository_id=paquete.repository_id,
                repository_name=nombre,
            )
            for paquete, nombre, _corto in filas
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=SupplyChainSummary)
async def resumen_inventario(
    tenant: TenantDependency,
    session: SessionDependency,
) -> SupplyChainSummary:
    """Los números de cabecera del inventario.

    Se cuentan en el backend y **exactos**, con un `COUNT ... FILTER` por estado. Ni se cuentan
    en el panel —que solo tiene la página actual— ni se aproximan sobre lo que vino: los dos
    producen un número falso con apariencia de exacto, y un número falso en la cabecera de un
    panel de seguridad es peor que no darlo.
    """

    datos = await service.resumen_inventario(session, tenant.organization.id)
    return SupplyChainSummary(
        total_dependencies=datos.total_dependencies,
        vulnerable=datos.vulnerable,
        clean=datos.clean,
        unchecked=datos.unchecked,
        by_ecosystem=datos.by_ecosystem,
        repositories_indexed=datos.repositories_indexed,
    )


@router.post(
    "/packages/index",
    response_model=SupplyChainIndexResult,
    status_code=status.HTTP_201_CREATED,
)
async def indexar_manifiesto(
    payload: SupplyChainIndexRequest,
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: Annotated[uuid.UUID, Query()],
) -> SupplyChainIndexResult:
    """Indexa las dependencias que declara un manifiesto que el cliente aporta.

    El texto **no se persiste**: entra, se parsea y solo se guardan sus identificadores. Y
    antes de nada se le aplica la limpieza de credenciales de `manifests.py`, de forma que un
    token dentro de una URL de registro privado no llega a existir en la base.

    Usa el mismo acceso por membresía que el resto del bloque —`X-Organization-Id` comprobado
    contra la membresía activa— y no un permiso de API token. El motivo es que indexar **escribe**
    en el workspace, y las reglas de negocio del cliente no se exponen a integraciones: quien
    alimenta el inventario es la persona sentada en el panel.
    """

    try:
        resultado = await service.indexar_manifiesto(
            session,
            organization_id=tenant.organization.id,
            repository_id=repository_id,
            manifest_path=payload.manifest_path,
            contenido=payload.content,
        )
    except service.RepositoryNotIndexedError as error:
        # `404` y no `403`: ver `_no_encontrado`. Un `403` confirmaría que el repositorio existe.
        raise _no_encontrado() from error

    await session.commit()
    return SupplyChainIndexResult(
        inserted=resultado.inserted,
        updated=resultado.updated,
        discarded=resultado.discarded,
        total=resultado.total,
    )


__all__ = ["PAGE_SIZE", "router"]
