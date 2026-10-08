"""Endpoints del inventario de dependencias (`/api/v1/supply-chain`).

## Qué expone y qué no

Expone **leer** el inventario, **indexar** un manifiesto que el cliente aporta, y
**sincronizar** un repositorio contra la API del proveedor.

Lo tercero se decidió después de escribir el módulo, y la forma que se eligió es la que **no**
introduce código fuente en la plataforma: se piden cuatro ficheros por su nombre y se descartan.
Un `package.json` es una lista de dependencias, no el código del cliente, y R5 habla del código.
`sync.py` explica el razonamiento entero; este encabezado solo deja constancia de que la decisión
está tomada y de dónde viene, porque un `POST` que sincroniza contra un proveedor externo
justifica cada uno de sus parámetros.

## Por qué el filtro por organización es **incondicional** en los dos endpoints

Porque R3 no es una recomendación, y el listado global es donde más fácil se colaría: sin el
filtro, un cliente vería los paquetes de su vecino, que es informacion sobre su stack.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.apps.commercial.service import feature_enabled
from backend.apps.repositories.clients.base import GitClientError
from backend.apps.repositories.router import (
    _open_client,
    _translate_client_error,
)
from backend.apps.supply_chain import service, sync
from backend.apps.supply_chain.models import EcosystemEnum
from backend.apps.supply_chain.schemas import (
    SupplyChainIndexRequest,
    SupplyChainIndexResult,
    SupplyChainPackageItem,
    SupplyChainPackagePage,
    SupplyChainSummary,
    SupplyChainSyncResult,
)
from backend.apps.supply_chain.sync import _cargar_repositorio
from backend.core.middleware import (
    AdminRequired,
    SessionDependency,
    TenantContext,
    get_current_tenant,
)


async def require_supply_chain(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    session: SessionDependency,
) -> None:
    if not await feature_enabled(session, tenant.organization.id, "supply_chain"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Feature no habilitada")


router = APIRouter(
    prefix="/api/v1/supply-chain",
    tags=["supply-chain"],
    dependencies=[Depends(require_supply_chain)],
)

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
    # Exige `ADMIN` por la misma razon que la sincronizacion de arriba, y una mas: esta escribe
    # con `INSERT ... ON CONFLICT`, o sea que un `MEMBER` puede **anadir** dependencias al
    # inventario de su organizacion. Un inventario de dependencias es la entrada de la
    # superficie de ataque; poder escribir en el cambia la postura que el cliente ve.
    dependencies=[AdminRequired],
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


@router.post(
    # Exige `ADMIN`, y por lo mismo que en los documentos de conocimiento: la sincronización
    # abre un cliente Git con la **credencial del workspace** y escribe masivamente en
    # `supply_chain_packages`. El aislamiento por tenant está bien resuelto —además replicado
    # dentro del servicio—, pero sin esto cualquier miembro dispara peticiones salientes con la
    # credencial de la empresa y escribe en la base.
    "/repositories/{repository_id}/sync",
    response_model=SupplyChainSyncResult,
    status_code=status.HTTP_200_OK,
    dependencies=[AdminRequired],
)
async def sync_repository_manifests(
    repository_id: uuid.UUID,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    session: SessionDependency,
) -> SupplyChainSyncResult:
    """Descarga los manifiestos de un repositorio y actualiza su inventario de dependencias.

    ## Por qué vuelve a leer el repositorio y no confía en el `repository_id` de la URL

    Porque el `repository_id` es un identificador adivinable, y porque la credencial que se usa
    para preguntar al proveedor es la de **esta** organización. Si el repositorio fuera de otro
    cliente, la petición se haría con el token de este contra el repositorio de aquel, y el
    inventario resultante aparecería en el panel equivocado. R3.

    El filtro por `organization_id` está dentro de `_cargar_repositorio`, no aquí: ponerlo en el
    servicio y no en la ruta es lo que hace que no se pueda llamar al servicio desde otro sitio
    saltándoselo.
    """

    organization_id = tenant.organization.id

    # El repositorio se lee una vez aquí, y otra dentro del servicio. Es deliberado: el servicio
    # es la frontera de confianza y tiene que poder comprobarlo por su cuenta, aunque quien lo
    # llama ya lo haya comprobado. La segunda lectura es una fila por organization_id contra un
    # índice único, y comprar con ella la garantía de que la ruta no es el único sitio donde se
    # puede introducir un `repository_id` ajeno es un precio aceptable.
    repositorio = await _cargar_repositorio(session, organization_id, repository_id)
    if repositorio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El repositorio no existe en esta organización",
        )

    try:
        async with _open_client(session, organization_id, repositorio.provider) as client:
            resultado = await sync.sincronizar_manifiestos(
                session,
                organization_id=organization_id,
                repository_id=repository_id,
                client=client,
            )
    except GitClientError as error:
        # Un fallo del proveedor es un `502`, no un `400`: la petición del usuario era válida y
        # el que no pudo atenderla fue el proveedor. Y el mensaje se traduce con la misma funcion
        # que usa el router de repositorios, para que el usuario lea el mismo texto si el fallo
        # aparece al conectar el repositorio o al sincronizarlo.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_translate_client_error(error).detail,
        ) from error

    # La construcción es explícita y no un `**resultado.como_dict()`.
    #
    # El `**` de un `dict[str, object]` es cómodo y no se puede verificar: Pydantic recibe `object`
    # en cada campo y pyright no puede decir si el `int` que llega es un `int`. Cuando el esquema
    # y el diccionario se desincronicen, la validación en caliente es lo unico que lo detecta, y
    # eso es un error de produccion en vez de uno de compilacion.
    #
    # De paso, el mapeo se ve entero en un sitio. Que un campo del resultado se llame igual que
    # el del esquema no significa que signifiquen lo mismo, y esa es la clase de error que un
    # `**` esconde.
    return SupplyChainSyncResult(
        manifests_found=list(resultado.manifestos_encontrados),
        manifests_missing=resultado.manifiestos_ausentes,
        packages_inserted=resultado.insertados,
        packages_updated=resultado.actualizados,
        packages_discarded=resultado.descartados,
        errors=list(resultado.errores),
    )
