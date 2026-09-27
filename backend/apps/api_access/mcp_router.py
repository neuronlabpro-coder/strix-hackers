"""Endpoint del servidor MCP.

## Por qué este router devuelve el sobre JSON-RPC tal cual y no un modelo de FastAPI

Porque un modelo de respuesta de FastAPI **rechaza** lo que no encaja en el esquema declarado,
y el transporte MCP necesita exactamente lo contrario: devolver un error con forma de error.
Si la respuesta estuviera tipada, un `-32601` no se podría serializar, y el error tendría que
ir en el `status_code` —que es lo que se dejó de hacer a propósito, para que un agente no
confundiera un fallo de herramienta con un fallo de red—.

La consecuencia es que la validación de la respuesta pasa a ser de los **tests**, y no la hace
el framework. Es un coste real y es el precio de hablar un protocolo que no es REST.

## Por qué un único endpoint y no uno por herramienta

Porque es lo que define MCP. El cliente MCP tiene una URL, no un catálogo de rutas, y la
selección de herramienta viaja en el `method` del sobre. Exponer además rutas REST por
herramienta sería un segundo camino hacia las mismas acciones con otra sintaxis de
autorización, y son esos duplicados los que se desincronizan.

## Por qué la autenticación va por HTTP y los errores de protocolo no

Está razonado en `mcp_protocol`. Se resume: un `401` lo entiende cualquier cliente HTTP antes
de leer el JSON, y encerrarlo en un `200` haría que un cliente mal configurado no pudiera
distinguir "tu token no vale" de "la herramienta no existe".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.auth import (
    TenantPrincipal,
    require_all_scopes,
)
from backend.apps.api_access.mcp_protocol import (
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcRequest,
    handle_payload,
)
from backend.apps.api_access.mcp_tools import TOOLS, TOOLS_POR_NOMBRE, exigir_scope
from backend.apps.api_access.scopes import Scope
from backend.apps.pentests.service import get_dispatch_pentest_run
from backend.core.middleware import SessionDependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

#: Se exigen los **dos** scopes de MCP, no uno. `mcp:connect` es la puerta —poder hablar MCP—
#: y `mcp:invoke` es poder ejecutar sobre los recursos. Un token con el primero y sin el
#: segundo puede descubrir qué herramientas existen, que es lo que permite a un cliente bien
#: escrito decir "tu token no alcanza para esto" en lugar de fallar al primer uso.
RequireMcp = Annotated[
    TenantPrincipal,
    Depends(require_all_scopes([Scope.MCP_CONNECT, Scope.MCP_INVOKE])),
]

#: Cómo se despacha un pentest. **La misma** dependencia que usa `POST /api/v1/pentests/`, y
#: no una copia: las dos rutas tienen que encolar igual, y dos despachadores distintos
#: significarían que un escaneo lanzado por MCP puede acabar en una cola que el panel no
#: vigila, sin que nada avise.
DispatchDependency = Annotated[Callable[[str], str], Depends(get_dispatch_pentest_run)]

#: Que herramientas expone cada perfil.
#:
#: `core` es el conjunto minimo para el uso habitual de un asistente: leer el estado de
#: seguridad del workspace y lanzar un escaneo. Se deja fuera `list_repositories` porque solo
#: aporta nombres de repositorio —informacion que el panel ya muestra— y `get_asset_inventory`
#: porque devuelve el inventario de superficie, que es mas de lo que necesita un chat para
#: responder.
#:
#: Un perfil que no existe devuelve un `422` de FastAPI por el `Literal`, en vez de caerse en
#: silencio al perfil completo. Un `profile=cor` por error de tecleo es el caso mas probable, y
#: el resultado de ignorarlo es un token con mas permisos de los que el usuario cree haber
#: pedido: el fallo no se ve, se nota meses despues en un incidente.
PERFILES: dict[str, frozenset[str]] = {
    "core": frozenset({"get_vulnerability_summary", "trigger_pentest"}),
}

ProfileName = Literal["core"]


def _catalogo(profile: ProfileName | None) -> dict[str, Any]:
    """La definición formal del catálogo, en el formato que espera `tools/list`.

    El `required_scope` viaja **dentro** de la definición. No es información interna: un agente
    que va a pedir un token tiene que saber qué permisos necesita, y pedirlo por el mismo
    camino por el que se comprueba evita que el catálogo y la autorización se separen. Un
    tool que anuncie un scope que no se exige —o al revés— hace que el usuario configure un
    token que fallará en producción.
    """

    permitidas = PERFILES.get(profile) if profile is not None else None
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "annotations": {
                    "requiredScope": tool.required_scope.value,
                    "readOnlyHint": tool.name != "trigger_pentest",
                },
            }
            for tool in TOOLS
            if permitidas is None or tool.name in permitidas
        ]
    }


async def _despachar(
    peticion: JsonRpcRequest,
    principal: TenantPrincipal,
    session: AsyncSession,
    dispatch: Callable[[str], str],
    profile: ProfileName | None = None,
) -> Any:
    """Atiende un método MCP ya validado.

    El `try/except ValidationError` de aquí es distinto del del protocolo: el de
    `mcp_protocol` traduce lo que no se controla, y este traduce lo que las herramientas
    dejan escapar. Sin él, un `ValidationError` de Pydantic en el interior de una herramienta
    llegaría al agente como `-32603`, que dice "fallo interno del servidor" y no "has
    mandado un parámetro malo" — un diagnóstico que lleva a reintentar con lo mismo, que es
    la forma de gastar cuota sin conseguir nada.
    """

    try:
        if peticion.method == "tools/list":
            return _catalogo(profile)

        if peticion.method == "tools/call":
            return await _llamar_herramienta(peticion, principal, session, dispatch, profile)

        raise JsonRpcError(
            JsonRpcErrorCode.METHOD_NOT_FOUND,
            "Method not found",
            {
                "metodo": peticion.method,
                "disponibles": ["tools/list", "tools/call"],
            },
        )
    except JsonRpcError:
        raise
    except ValidationError as error:
        raise JsonRpcError(
            JsonRpcErrorCode.INVALID_PARAMS,
            "Invalid params",
            {"errores": error.errors(include_url=False)},
        ) from error


async def _llamar_herramienta(
    peticion: JsonRpcRequest,
    principal: TenantPrincipal,
    session: AsyncSession,
    dispatch: Callable[[str], str],
    profile: ProfileName | None = None,
) -> Any:
    params = peticion.params or {}
    nombre = params.get("name")
    argumentos = params.get("arguments") or {}

    if not isinstance(nombre, str) or not isinstance(argumentos, dict):
        raise JsonRpcError(
            JsonRpcErrorCode.INVALID_PARAMS,
            "Invalid params",
            {
                "esperado": "params.name es el nombre de la herramienta y "
                "params.arguments es un objeto con sus argumentos"
            },
        )

    herramienta = TOOLS_POR_NOMBRE.get(nombre)
    if herramienta is None:
        # No se distingue "herramienta que no existe" de "herramienta a la que no tiene
        # acceso": para quien llama, el resultado es el mismo, y revelar la diferencia
        # confirmaría la existencia de herramientas que su token no alcanza. El detalle sí
        # lista el catálogo, que es público para cualquier cliente conectado.
        raise JsonRpcError(
            JsonRpcErrorCode.METHOD_NOT_FOUND,
            "Tool not found",
            {
                "herramienta": nombre,
                "disponibles": sorted(TOOLS_POR_NOMBRE),
            },
        )

    if profile is not None and nombre not in PERFILES[profile]:
        # Mismo codigo y mismo detalle que una herramienta inexistente, y por el mismo motivo:
        # decir "existe pero tu perfil no la alcanza" confirma la existencia de una capacidad
        # que el cliente no tiene. El `perfil` si se devuelve, porque el cliente lo eligio.
        raise JsonRpcError(
            JsonRpcErrorCode.METHOD_NOT_FOUND,
            "Tool not found",
            {
                "herramienta": nombre,
                "perfil": profile,
                "disponibles": sorted(PERFILES[profile]),
            },
        )

    # El scope se comprueba **después** de resolver la herramienta y **antes** de ejecutarla.
    # Antes de ejecutarla, que es lo que importa. La comprobación va aquí y no en el catálogo
    # para que añadir una herramienta sin scope sea un error de pruebas, no una puerta
    # abierta en producción.
    exigir_scope(principal, herramienta.required_scope)

    logger.info(
        "Llamada MCP: herramienta=%s principal=%s",
        nombre,
        principal.organization.id,
    )
    return await herramienta.run(
        session=session,
        principal=principal,
        params=argumentos,
        dispatch=dispatch,
    )


@router.post("")
async def mcp_endpoint(
    principal: RequireMcp,
    session: SessionDependency,
    dispatch: DispatchDependency,
    body: Annotated[Any, Body()],
    profile: Annotated[ProfileName | None, Query()] = None,
) -> Any:
    """Atiende una llamada MCP: un sobre JSON-RPC o un lote de sobres.

    ## Por qué `Annotated[Any, Body()]` y no un modelo de Pydantic

    Porque un modelo rechazaría con `422` lo que tiene que responderse con un `-32600`. Un
    cliente MCP que mande un sobre mal formado tiene que recibir un error que pueda leer, no
    un `422` de FastAPI cuyo cuerpo ni siquiera es JSON-RPC. El coste es que **el contrato de
    entrada ya no lo valida el framework**; lo valida `mcp_protocol.parse_request`, y eso es lo
    que prueban los tests de este módulo.

    ## Por qué el `Body()` explícito no es opcional

    Porque FastAPI decide query-o-cuerpo según el tipo anotado, y un `Any` a secas se toma
    como parámetro de **query**. El endpoint entonces contestaba `422` a
    `{"loc": ["query", "body"], "msg": "Field required"}` a cualquier cliente MCP, con
    incluida la llamada correcta, y el síntoma es "el agente no se conecta" con un error que
    no menciona el cuerpo ni MCP. Es un fallo silencioso: la ruta existe, OpenAPI la publica
    y el `200` nunca llega. Lo detectó la prueba que manda un sobre real.
    """

    def despachador(peticion: JsonRpcRequest) -> Any:
        return _despachar(peticion, principal, session, dispatch, profile)

    return await handle_payload(body, despachador)


__all__ = ["PERFILES", "ProfileName", "router"]
