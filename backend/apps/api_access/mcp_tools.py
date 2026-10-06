"""Las cuatro herramientas que el servidor MCP expone a un agente.

## Por qué cada herramienta declara el scope que exige

Porque `mcp:invoke` no es un permiso para usar el servidor: es un permiso para **llamarlo**. Lo
que una herramienta puede hacer lo decide el recurso que toca, y por eso cada una lleva su
propio scope. Sin esa segunda comprobación, un token con `mcp:connect`, `mcp:invoke` y
`repositories:read` —el token que se crea para "que el agente lea mis repositorios"— podría
también lanzar pentests, porque las dos cosas pasan por la misma puerta.

La comprobación está **en** la herramienta y no en el catálogo, y esa colocación es lo que la
hace segura: si un día se añade una herramienta nueva sin.scope, la puerta sigue cerrada por
defecto, porque `tools/list` no anuncia nada que no sepa ejecutar y `tools/call` rechaza
cualquier nombre que no esté en el catálogo.

## Por qué el resultado es texto y no un objeto

Porque es lo que espera un LLM, y porque la alternativa rompe la mitad de los clientes. Un
agente lee texto; si le devuelves JSON tiene que decidir si lo interpreta, y muchos lo
concatenan literalmente en el contexto sin parsearlo, con lo que el modelo acaba leyendo
`{'total': 12}` como si fuera una frase. El texto formateado es más largo en tokens y mucho
más fiable en la práctica.

## Por qué los resultados están acotados

Porque un agente no pide una lista para leerla entera: pide lo que necesita para decidir. Sin
topes, `get_asset_inventory` sobre un dominio con diez mil activos devuelve diez mil filas que
llenan la ventana de contexto y empujan fuera la conversación que llevó a pedirlas. Cada
resultado lleva su propio "se ha truncado", porque un inventario que se corta sin decirlo es
peor que uno que devuelve un error: parece completo.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.auth import TenantPrincipal, TokenPrincipal
from backend.apps.api_access.mcp_protocol import JsonRpcError, JsonRpcErrorCode
from backend.apps.api_access.scopes import Scope
from backend.apps.assets.models import AssetTypeEnum, DiscoveredAsset, VerifiedDomain
from backend.apps.pentests.models import PentestRun, ScanModeEnum, TargetTypeEnum
from backend.apps.pentests.schemas import PentestCreate
from backend.apps.repositories.models import Repository
from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.core.filtros_texto import coincide

logger = logging.getLogger(__name__)

#: Tope de filas por herramienta.
#:
#: No es un detalle de paginación sino un límite de **contexto**: el consumidor es un modelo
#: con una ventana finita, y una respuesta que la desborda hace que el agente pierda lo que
#: estaba haciendo para atender la petición que él mismo hizo. Todos declarados aquí y no
#: repartidos porque el mismo número en cuatro sitios se desincroniza sin que nadie lo note.
MAX_ROWS = 50

#: Tope del texto de salida. Va por encima de `MAX_ROWS` porque una fila puede ser larga, y lo
#: que se protege es la ventana del modelo, no el número de filas.
MAX_TEXT_CHARS = 24_000


# --------------------------------------------------------------------------- #
# Contrato de las herramientas
# --------------------------------------------------------------------------- #


class ToolParams(BaseModel):
    """Base de los parámetros de entrada de una herramienta.

    `extra="forbid"` es lo que hace que un agente no pueda colar un parámetro. Sin eso, un
    cliente que mande `{"target": "..."}` a `trigger_pentest` —un nombre razonable pero
    equivocado— obtendría un `-32602` solo si el esquema declarara `target`; con el esquema
    abierto se aceptaría en silencio y el escaneo se lanzaría sin destino o contra el
    equivocado.
    """

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class Tool:
    """Una herramienta: su nombre, su esquema, su scope y su implementación."""

    name: str
    description: str
    #: Esquema JSON de entrada, en el formato que declara MCP. Se escribe a mano y no se
    #: deriva del modelo de Pydantic porque el agente lo lee, y lo que lee tiene que ser
    #: legible para un modelo: los `title` y los `$defs` que genera Pydantic no lo son.
    input_schema: dict[str, Any]
    required_scope: Scope
    run: Callable[..., Any]


def _esquema(
    propiedades: dict[str, Any], requeridos: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": propiedades,
        "required": requeridos or [],
        "additionalProperties": False,
    }


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="list_repositories",
        description=(
            "Lista los repositorios vigilados del espacio de trabajo con su identificador, "
            "proveedor y rama base. Es el primer paso para lanzar un escaneo sobre código "
            "propio: devuelve los valores de `repository_id` que acepta `trigger_pentest`."
        ),
        input_schema=_esquema(
            {
                "search": {
                    "type": "string",
                    "description": "Filtra por nombre, total o parcial. Opcional.",
                },
            }
        ),
        required_scope=Scope.REPOSITORIES_READ,
        run=lambda **kwargs: _list_repositories(**kwargs),
    ),
    Tool(
        name="get_vulnerability_summary",
        description=(
            "Recuento de vulnerabilidades por severidad y por estado. Devuelve solo los "
            "hallazgos que siguen abiertos: un resumen para decidir no incluye los ya "
            "resueltos, porque contarlos infla el número y con ello la urgencia que transmite."
        ),
        input_schema=_esquema(
            {
                "repository_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Limita el recuento a un repositorio. Opcional.",
                },
            }
        ),
        required_scope=Scope.VULNERABILITIES_READ,
        run=lambda **kwargs: _vulnerability_summary(**kwargs),
    ),
    Tool(
        name="trigger_pentest",
        description=(
            "Lanza un escaneo y lo encola. Consume créditos del saldo del espacio de "
            "trabajo igual que el panel. Admite `target_type` DOMAIN o REPOSITORY; con "
            "REPOSITORY, `target_identifier` debe ser el `repository_id` que devuelve "
            "`list_repositories`."
        ),
        input_schema=_esquema(
            {
                "target_type": {
                    "type": "string",
                    "description": "DOMAIN o REPOSITORY.",
                },
                "target_identifier": {
                    "type": "string",
                    "description": (
                        "El dominio a escanear, sin esquema ni ruta, o el `repository_id` "
                        "devuelto por `list_repositories`."
                    ),
                },
                "scan_mode": {
                    "type": "string",
                    "description": (
                        "STANDARD, DEEP o QUICK. Opcional; STANDARD si no se indica."
                    ),
                },
            },
            requeridos=["target_type", "target_identifier"],
        ),
        required_scope=Scope.PENTESTS_CREATE,
        run=lambda **kwargs: _trigger_pentest(**kwargs),
    ),
    Tool(
        name="get_asset_inventory",
        description=(
            "Dominios verificados y activos descubiertos bajo ellos. Filtra por `domain_id` "
            "y por `asset_type` (SUBDOMAIN, IP_ADDRESS, API_ENDPOINT). La respuesta indica "
            "cuántos hay en total aunque devuelva menos, para que el agente sepa que está "
            "viendo una parte."
        ),
        input_schema=_esquema(
            {
                "domain_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Limita a los activos de un dominio. Opcional.",
                },
                "asset_type": {
                    "type": "string",
                    "description": "SUBDOMAIN, IP_ADDRESS o API_ENDPOINT. Opcional.",
                },
            }
        ),
        required_scope=Scope.ASSETS_READ,
        run=lambda **kwargs: _asset_inventory(**kwargs),
    ),
)

TOOLS_POR_NOMBRE: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


# --------------------------------------------------------------------------- #
# Autorización por herramienta
# --------------------------------------------------------------------------- #


def exigir_scope(principal: TenantPrincipal, scope: Scope) -> None:
    """Comprueba el scope de la herramienta, o aborta con `-32001`.

    ## Por qué un token con `mcp:invoke` no lo lleva todo

    Porque `mcp:invoke` es la puerta, no el permiso. El permiso es el de la herramienta: leer
    repositorios y lanzar escaneos son cosas distintas con consecuencias distintas, y un token
    que puede ver el inventario de la superficie de ataque de una empresa no debería poder
    lanzar un pentest contra ella por el mismo hecho de poder hablar MCP.

    ## Por qué el usuario web **no** pasa por aquí, y por qué la comprobación es solo de token

    Entra por sesión y su autorización es el rol, que ya se comprobó al resolver el sujeto: un
    `ADMIN` del panel tiene todas las herramientas, igual que tiene todas las pantallas, y un
    `MEMBER` no tiene ninguna.

    Y la línea de abajo solo mira `TokenPrincipal` a propósito. Un token de API tiene **scopes**,
    y el scope es la granularidad fina: `mcp:invoke` abre la puerta y el scope de la herramienta
    decide qué se puede hacer. Un usuario web no tiene scopes —`UserPrincipal` ni siquiera tiene
    el método—, así que para él esta comprobación sería un no-op y su autorización es el rol.

    ## Por qué antes esto decía otra cosa

    Porque el docstring afirmaba que el servidor MCP exige credencial de servicio y que el
    usuario web no alcanzaba este camino, y **no era cierto**: `allow_admin_user` venía con
    `True` por defecto en `require_scope` y `require_all_scopes`, de modo que una sesión web de
    `ADMIN` sí pasaba `require_all_scopes([MCP_CONNECT, MCP_INVOKE])`.

    La consecuencia no era una escalada dentro del tenant —ya era admin— sino una **segunda vía
    no declarada** a una operación que encola escaneos y descuenta créditos, con un perfil `core`
    que no la restringía. Y el efecto sobre la revisión era peor que el fallo: un revisor que
    confiara en el comentario no lo vería nunca.
    """

    if isinstance(principal, TokenPrincipal) and not principal.has_scope(scope):
        raise JsonRpcError(
            JsonRpcErrorCode.INSUFFICIENT_SCOPE,
            "Insufficient scope",
            {
                "required_scope": scope.value,
                "tool": "herramienta que requiere un permiso que el token no tiene",
            },
        )


# --------------------------------------------------------------------------- #
# Envoltura de salida
# --------------------------------------------------------------------------- #


def bloque_de_texto(texto: str) -> dict[str, Any]:
    """Envuelve un texto en el bloque de contenido que espera MCP.

    ## Por qué el JSON va con `ensure_ascii=False` y con salto de línea

    Porque un LLM lee mucho mejor un JSON indentado que una sola línea de mil caracteres: la
    estructura se ve. Y porque un acento escapado en notacion Unicode es ilegible
    para quien depura un registro, y un nombre de repositorio en espanol se vuelve ruido.
    registro, y un nombre de repositorio o una descripción en español se vuelven ruido.
    """

    recortado = texto[:MAX_TEXT_CHARS]
    suprimido = len(texto) - len(recortado)
    # La anotacion es necesaria: sin ella el tipo se estrecha al primer literal --una lista
    # de mapas de cadena-- y anadir `meta` debajo da un error que no existe en ejecucion.
    cuerpo: dict[str, Any] = {"content": [{"type": "text", "text": recortado}]}
    if suprimido > 0:
        # Se declara fuera del bloque de texto para que un cliente pueda distinguir el corte
        # del contenido sin parsear el texto. Un inventario truncado sin aviso se lee como
        # completo, que es peor que un error.
        cuerpo["isError"] = False
        cuerpo["meta"] = {"truncated": True, "omitted_chars": suprimido}
    return cuerpo


def _tabla(titulo: str, filas: list[dict[str, Any]], total: int) -> str:
    """Compone una tabla legible con su recuento real.

    El recuento va **siempre**, no solo cuando hay recorte. Un total de 3 con 3 filas y un
    total de 300 con 3 filas son respuestas distintas, y un agente que no puede distinguirlas
    conclude que la superficie de ataque son tres máquinas.
    """

    encabezado = f"{titulo}: {len(filas)} de {total}"
    if not filas:
        return f"{encabezado}\n(sin resultados)"
    cuerpo = "\n".join(json.dumps(fila, ensure_ascii=False) for fila in filas)
    if len(filas) < total:
        encabezado += (
            f" — mostrando los primeros {len(filas)}. "
            "Usa un filtro para acotar el resultado."
        )
    return f"{encabezado}\n{cuerpo}"


# --------------------------------------------------------------------------- #
# Herramientas
# --------------------------------------------------------------------------- #


class _ListRepositories(ToolParams):
    search: str | None = Field(default=None, max_length=200)


async def _list_repositories(
    session: AsyncSession,
    principal: TenantPrincipal,
    params: dict[str, Any],
    dispatch: Callable[[str], str],
) -> dict[str, Any]:
    """Repositorios del workspace.

    Devuelve el `id` porque es lo que `trigger_pentest` necesita, y no solo el nombre: sin
    el identificador el agente tendría que adivinar el destino de un escaneo por coincidencia
    de texto, que es como se escanea el repositorio equivocado.
    """

    entrada = _ListRepositories.model_validate(params)
    filtros = [Repository.organization_id == principal.organization.id]
    if entrada.search:
        # Se busca en minusculas porque un agente escribirá `API` y el repositorio se
        # llama `api`, y una busqueda sensible a mayusculas devolveria cero filas ante una
        # peticion que es correcta. El `lower()` de `coincide` lo garantiza en todas.
        #
        # Y `coincide` escapa los comodines de `LIKE`, que aquí importa más que en cualquier
        # otro buscador del proyecto: quien escribe el término es un agente, no una persona. Un
        # filtro que en vez de filtrar devuelve la lista entera no produce un error que el
        # agente sepa leer —produce una lista que el agente se queda como verdad—, así que
        # acaba escaneando el repositorio equivocado. Sin escape, `search="%"` devuelve todos
        # los repositorios del tenant y `search="web_app"` también trae `webXapp`.
        filtros.append(coincide([Repository.full_name, Repository.name], entrada.search))

    total = int(
        (
            await session.execute(
                select(func.count(Repository.id)).where(*filtros)
            )
        ).scalar_one()
    )
    repos = (
        (
            await session.execute(
                select(Repository)
                .where(*filtros)
                .order_by(Repository.full_name)
                .limit(MAX_ROWS)
            )
        )
        .scalars()
        .all()
    )

    filas = [
        {
            "id": str(repo.id),
            "name": repo.name,
            "full_name": repo.full_name,
            "provider": str(repo.provider.value),
            "default_branch": repo.default_branch,
            "is_active": repo.is_active,
        }
        for repo in repos
    ]
    return bloque_de_texto(_tabla("Repositorios vigilados", filas, total))


class _VulnerabilitySummary(ToolParams):
    repository_id: UUID | None = None


async def _run_ids_de_repositorio(
    session: AsyncSession, organization_id: UUID, repository_id: UUID
) -> list[UUID]:
    """Los escaneos-launchados sobre un repositorio, resueltos a sus identificadores.

    ## Por qué hace falta una consulta aparte

    Porque `Vulnerability` no guarda el repositorio: guarda el `run_id` del escaneo que la
    produjo, y el repositorio vive en `PentestRun.target_identifier`. La relación es real pero
    pasa por la tabla intermedia, así que el filtro se resuelve en una subconsulta en vez de
    inventar una columna que no existe.

    Se filtra también por `organization_id` **y** por `target_type`. Sin lo segundo, un
    repositorio cuyo identificador coincidiera con el nombre de un dominio traería los
    hallazgos de ese dominio, que es un escaneo distinto y no lo que pidió quien filtró.
    """

    filas = await session.execute(
        select(PentestRun.id).where(
            PentestRun.organization_id == organization_id,
            PentestRun.target_type == TargetTypeEnum.REPOSITORY,
            PentestRun.target_identifier == str(repository_id),
        )
    )
    return list(filas.scalars().all())


async def _vulnerability_summary(
    session: AsyncSession,
    principal: TenantPrincipal,
    params: dict[str, Any],
    dispatch: Callable[[str], str],
) -> dict[str, Any]:
    """Hallazgos abiertos por severidad y por estado.

    Se cuentan en **dos consultas** y no una con `GROUP BY` sobre las dos dimensiones: una
    sola GroupBy no puede dar los dos marginales a la vez sin un `ROLLUP` que PostgreSQL no
    ofrece, y la alternativa —recorrer los hallazgos en Python para contarlos— trae la tabla
    entera a memoria. Con dos consultas la respuesta es la misma y el coste es fijo.
    """

    entrada = _VulnerabilitySummary.model_validate(params)
    # El criterio de "abierto" sale de `IssueStatusEnum.is_open_for_closure` y no de una
    # lista propia. Con una lista aquí, añadir un estado obligaba a recordar este sitio: la
    # herramienta respondería "0 abiertas" con un PR de remediación abierto mientras el panel
    # lo contaba, y los dos dirían cosas distintas de la misma base de datos.
    estados_cerrados = [
        estado
        for estado in IssueStatusEnum
        if not estado.is_open_for_closure
    ]
    filtros = [
        Vulnerability.organization_id == principal.organization.id,
        Vulnerability.status.notin_(estados_cerrados),
    ]
    if entrada.repository_id is not None:
        run_ids = await _run_ids_de_repositorio(
            session, principal.organization.id, entrada.repository_id
        )
        if not run_ids:
            # Sin escaneos para ese repositorio el filtro sería `IN ()`, que en SQL no es
            # válido y en SQLAlchemy se traduciría a algo cuyo comportamiento cambia entre
            # versiones. Se devuelve el cero directamente.
            texto = (
                "No hay escaneos registrados para ese repositorio, así que no hay "
                "vulnerabilidades que contar. Lánzalo con `trigger_pentest` primero."
            )
            return bloque_de_texto(texto)
        filtros.append(Vulnerability.run_id.in_(run_ids))

    por_severidad = (
        await session.execute(
            select(Vulnerability.severity, func.count(Vulnerability.id))
            .where(*filtros)
            .group_by(Vulnerability.severity)
        )
    ).all()
    por_estado = (
        await session.execute(
            select(Vulnerability.status, func.count(Vulnerability.id))
            .where(*filtros)
            .group_by(Vulnerability.status)
        )
    ).all()

    severidades = {severity.value: 0 for severity in SeverityEnum}
    severidades.update({str(sev): int(cuenta) for sev, cuenta in por_severidad})
    estados = {str(est): int(cuenta) for est, cuenta in por_estado}
    total = sum(severidades.values())

    if total == 0:
        texto = (
            "No hay vulnerabilidades abiertas en el alcance consultado.\n"
            "El recuento se limita a los estados OPEN, IN_PROGRESS y SNOOZED."
        )
    else:
        texto = json.dumps(
            {"total_abiertas": total, "por_severidad": severidades, "por_estado": estados},
            ensure_ascii=False,
            indent=2,
        )
    return bloque_de_texto(texto)


class _TriggerPentest(ToolParams):
    target_type: str
    target_identifier: str = Field(min_length=1, max_length=512)
    scan_mode: str | None = None


async def _trigger_pentest(
    session: AsyncSession,
    principal: TenantPrincipal,
    params: dict[str, Any],
    dispatch: Callable[[str], str],
) -> dict[str, Any]:
    """Encola un escaneo con **el mismo** camino y el mismo cobro que el panel.

    ## Por qué no se llama al endpoint por HTTP

    Un salto de red dentro del servidor, un token de servicio por el medio y el doble de la
    reserva de créditos. Y una dependencia de que la ruta siga siendo una ruta. La función de
    dominio es la misma en los dos casos porque las dos llaman a `queue_pentest`.

    ## Por qué el fallo de cobro es `-32000` y no un error de protocolo

    Porque el sobre era válido y la herramienta se ejecutó: un error de saldo no es un
    problema de JSON-RPC, es un resultado. El agente que lo recibe sabe que reintentar sin
    recargar no va a funcionar, que es justo lo que distingue este código de un `-32601`.
    """

    entrada = _TriggerPentest.model_validate(params)
    # El enum se resuelve **antes** de construir el payload y con su propio `-32602`, en vez
    # de dejar que `PentestCreate` rechace la cadena. El mensaje es la diferencia: un
    # `ValidationError` de Pydantic habla de un campo del backend, y un agente no sabe qué
    # hacer con eso. Aquí se le da la lista de lo que sí admite.
    try:
        target_type = TargetTypeEnum(entrada.target_type)
    except ValueError as error:
        raise JsonRpcError(
            JsonRpcErrorCode.INVALID_PARAMS,
            "Invalid params",
            {"target_type_admitidos": [tipo.value for tipo in TargetTypeEnum]},
        ) from error

    kwargs: dict[str, Any] = {}
    if entrada.scan_mode is not None:
        try:
            kwargs["scan_mode"] = ScanModeEnum(entrada.scan_mode)
        except ValueError as error:
            raise JsonRpcError(
                JsonRpcErrorCode.INVALID_PARAMS,
                "Invalid params",
                {"scan_mode_admitidos": [modo.value for modo in ScanModeEnum]},
            ) from error

    try:
        payload = PentestCreate(
            target_type=target_type,
            target_identifier=entrada.target_identifier,
            **kwargs,
        )
    except ValidationError as error:
        # Aquí sí se propaga como `-32602`: el error es sobre el contenido del destino —un
        # dominio con ruta, un destino privado— y el detalle de Pydantic dice cuál, que es
        # lo que el agente necesita para corregir la llamada.
        raise JsonRpcError(
            JsonRpcErrorCode.INVALID_PARAMS,
            "Invalid params",
            {"errores": error.errors(include_url=False)},
        ) from error

    from backend.apps.billing.service import InsufficientCreditsError
    from backend.apps.pentests.service import (
        PentestDispatchError,
        TargetNotOwnedError,
        queue_pentest,
    )

    try:
        run = await queue_pentest(
            session, principal.organization, payload, dispatch
        )
    except TargetNotOwnedError as error:
        # Código **propio** y no el genérico de herramienta. Un destino que no es del
        # workspace es un dato que el agente no puede corregir: probar otros
        # identificadores solo produce el mismo error, y un agente que reintenta en bucle
        # con identificadores inventados es denegación de servicio contra la plataforma.
        # Por eso dice "no reintentar" y por eso el mensaje no confirma si el
        # identificador existe en otro sitio: eso confirmaría la existencia de recursos
        # ajenos.
        raise JsonRpcError(
            JsonRpcErrorCode.INSUFFICIENT_SCOPE,
            "El destino no pertenece a este espacio de trabajo",
            {
                "reintentable": False,
                "accion": (
                    "Usa un repository_id devuelto por list_repositories con este mismo "
                    "token. No pruebes otros identificadores."
                ),
            },
        ) from error
    except InsufficientCreditsError as error:
        raise JsonRpcError(
            JsonRpcErrorCode.TOOL_ERROR,
            "Saldo de créditos insuficiente",
            {
                "requeridos": str(error.required),
                "disponibles": str(error.available),
                "accion": "Recarga créditos en el panel antes de reintentar",
            },
        ) from error
    except PentestDispatchError as error:
        raise JsonRpcError(
            JsonRpcErrorCode.TOOL_ERROR,
            "No se pudo encolar el escaneo",
            {
                "run_id": str(error.run_id),
                # Se dice explícitamente si el dinero está devuelto. Sin este dato el agente
                # informaría de que no se cobró cuando sí se cobró, y el cliente pediría un
                # reembolso que ya se ha hecho, o al contrario.
                "creditos_reembolsados": error.refunded,
            },
        ) from error

    return bloque_de_texto(
        json.dumps(
            {
                "run_id": str(run.id),
                "estado": run.status.value,
                "destino": run.target_identifier,
                "tipo_destino": run.target_type.value,
                "modo": run.scan_mode.value,
                "task_id": run.celery_task_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


class _AssetInventory(ToolParams):
    domain_id: UUID | None = None
    asset_type: str | None = None


async def _asset_inventory(
    session: AsyncSession,
    principal: TenantPrincipal,
    params: dict[str, Any],
    dispatch: Callable[[str], str],
) -> dict[str, Any]:
    """Dominios verificados y activos del workspace.

    ## Por qué devuelve también los dominios aunque se filtre por activos

    Porque un activo sin saber a qué dominio pertenece no se puede interpretar. Una IP en el
    inventario es un dato; esa IP **en el dominio del cliente X** es un hallazgo, y es la
    única forma de distinguir una dirección de su propio portal de la dirección de otra
    empresa que resuelve a lo mismo.
    """

    entrada = _AssetInventory.model_validate(params)

    dominios = (
        (
            await session.execute(
                select(VerifiedDomain)
                .where(VerifiedDomain.organization_id == principal.organization.id)
                .order_by(VerifiedDomain.is_verified.desc(), VerifiedDomain.domain_name)
            )
        )
        .scalars()
        .all()
    )
    filtros = [DiscoveredAsset.organization_id == principal.organization.id]
    if entrada.domain_id is not None:
        filtros.append(DiscoveredAsset.domain_id == entrada.domain_id)
    if entrada.asset_type:
        try:
            filtros.append(DiscoveredAsset.asset_type == AssetTypeEnum(entrada.asset_type))
        except ValueError as error:
            raise JsonRpcError(
                JsonRpcErrorCode.INVALID_PARAMS,
                "Invalid params",
                {
                    "asset_type_admitidos": [tipo.value for tipo in AssetTypeEnum],
                },
            ) from error

    total = int(
        (
            await session.execute(
                select(func.count(DiscoveredAsset.id)).where(*filtros)
            )
        ).scalar_one()
    )
    activos = (
        (
            await session.execute(
                select(DiscoveredAsset)
                .where(*filtros)
                .order_by(DiscoveredAsset.last_scanned_at.desc().nullslast(), DiscoveredAsset.value)
                .limit(MAX_ROWS)
            )
        )
        .scalars()
        .all()
    )

    dominios_por_id = {dominio.id: dominio.domain_name for dominio in dominios}
    filas = [
        {
            "valor": activo.value,
            "tipo": activo.asset_type.value,
            "dominio": dominios_por_id.get(activo.domain_id, "—"),
            "servicio": activo.service_name,
            "tecnologias": list(activo.technologies or []),
        }
        for activo in activos
    ]

    texto = _tabla("Activos descubiertos", filas, total)
    texto += "\n\n" + _tabla(
        "Dominios registrados",
        [
            {
                "id": str(dominio.id),
                "dominio": dominio.domain_name,
                "verificado": dominio.is_verified,
            }
            for dominio in dominios
        ],
        len(dominios),
    )
    return bloque_de_texto(texto)


__all__ = [
    "MAX_ROWS",
    "MAX_TEXT_CHARS",
    "TOOLS",
    "TOOLS_POR_NOMBRE",
    "Tool",
    "bloque_de_texto",
    "exigir_scope",
]
