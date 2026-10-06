"""Pruebas del servidor MCP: protocolo, autorizacion y aislamiento.

## Por qué el protocolo necesita pruebas propias

Porque el endpoint devuelve `body: Any` a proposito. Un modelo de Pydantic rechazaria con
`422` lo que JSON-RPC exige responder con un `-32600`, asi que la validacion de la entrada
**ya no la hace el framework**: la hace `mcp_protocol.parse_request`. Si esa funcion no
funciona, no hay un error de arranque ni una excepcion: el endpoint responde `200` con un
error que el cliente descarta, y el sintoma es "el agente no se conecta" sin causa.

## Por qué se prueban los codigos de error y no solo el camino feliz

Porque un codigo equivocado no rompe la llamada: rompe la **recuperacion**. Un agente que
recibe `-32601` sabe que el nombre de la herramienta no existe; si recibe `-32000` reintenta,
gasta cuota de escaneo y vuelve a fallar. El codigo es parte del contrato con el cliente, y
por eso cada uno tiene su prueba.

## Por qué el aislamiento se mide con `WHERE organization_id = ...`

Nunca contando filas globales. Es el antipatrón que ya fallo tres veces en este proyecto: la
base de pruebas es compartida, y un `count()` a secas cuenta lo que dejaron otros tests y
pasa o falla segun el orden de ejecucion.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.mcp_protocol import (
    JsonRpcError,
    JsonRpcErrorCode,
    handle_payload,
    parse_request,
)
from backend.apps.api_access.mcp_router import PERFILES
from backend.apps.api_access.mcp_tools import TOOLS_POR_NOMBRE
from backend.apps.api_access.models import (
    API_TOKEN_PREFIX,
    TOKEN_SECRET_BYTES,
    ApiToken,
    token_prefix_for,
)
from backend.apps.api_access.scopes import Scope
from backend.apps.assets.models import DiscoveredAsset, VerifiedDomain
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.pentests.service import get_dispatch_pentest_run
from backend.core.security import (
    generate_api_token,
    hash_api_token,
    hash_password,
)
from backend.main import app

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


async def _tenant_con_token(
    session: AsyncSession, scopes: tuple[Scope, ...], prefijo: str = "mcp"
) -> tuple[Organization, str]:
    """Un workspace con su admin y un token vivo con exactamente esos scopes.

    El token se crea **en la base** y se devuelve en claro, porque la columna es un hash y el
    cliente necesita el valor real para autenticarse. Es la única razón por la que esta
    función existe: el resto de pruebas del proyecto no necesitan presentar un token.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{prefijo} {suffix}", slug=f"{prefijo}-{suffix}"
    )
    user = User(
        email=f"{prefijo}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"Cliente de {prefijo}",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    crudo = generate_api_token(API_TOKEN_PREFIX, TOKEN_SECRET_BYTES)
    session.add(
        ApiToken(
            organization_id=organization.id,
            name=f"Token de {prefijo}",
            token_prefix=token_prefix_for(crudo),
            # Nunca el token en claro. Esta es la linea de la que depende todo el modulo.
            token_hash=hash_api_token(crudo),
            scopes=[scope.value for scope in scopes],
        )
    )
    await session.commit()
    return organization, crudo


async def _llamar(
    token: str,
    sobre: Any,
    status_esperado: int = 200,
    profile: str | None = None,
) -> Any:
    """Llama al endpoint MCP.

    `profile` va en la **query**, no en el cuerpo: es lo que hace el router y lo que haria un
    cliente. Ponerlo en el sobre JSON-RPC seria probar una ruta que no existe.
    """

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/mcp",
            json=sobre,
            headers={"Authorization": f"Bearer {token}"},
            params={"profile": profile} if profile is not None else None,
        )
    assert respuesta.status_code == status_esperado, respuesta.text
    return respuesta.json()


def _catalogo() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _invocar(nombre: str, argumentos: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": nombre, "arguments": argumentos or {}},
    }


# --------------------------------------------------------------------------- #
# Protocolo: se prueba sin HTTP, porque es donde no hay framework que lo haga
# --------------------------------------------------------------------------- #


def test_sobre_valido_se_parsea() -> None:
    parseado = parse_request(_catalogo())
    assert not isinstance(parseado, JsonRpcError)
    assert parseado.id == 1
    assert parseado.method == "tools/list"
    assert parseado.is_notification is False


@pytest.mark.parametrize(
    ("sobre", "codigo"),
    [
        # Sin `jsonrpc`, o con la versión equivocada.
        ({"id": 1, "method": "tools/list"}, JsonRpcErrorCode.INVALID_REQUEST),
        (
            {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
            JsonRpcErrorCode.INVALID_REQUEST,
        ),
        # `method` ausente, vacío o de otro tipo.
        ({"jsonrpc": "2.0", "id": 1}, JsonRpcErrorCode.INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": ""}, JsonRpcErrorCode.INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, JsonRpcErrorCode.INVALID_REQUEST),
        # Un `id` que no se puede devolver idéntico.
        ({"jsonrpc": "2.0", "id": 1.5, "method": "x"}, JsonRpcErrorCode.INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": True, "method": "x"}, JsonRpcErrorCode.INVALID_REQUEST),
        # `params` posicional: no se admite, por diseño.
        (
            {"jsonrpc": "2.0", "id": 1, "method": "x", "params": [1, 2]},
            JsonRpcErrorCode.INVALID_PARAMS,
        ),
    ],
)
def test_sobre_mal_formado_da_el_codigo_correcto(
    sobre: Any, codigo: JsonRpcErrorCode
) -> None:
    """Cada forma de sobre roto produce su código, no un genérico.

    Un `-32600` cuando el problema son los parámetros hace que el cliente los cambie y no
    arregle nada, y entra en un bucle. La distinción entre sobre y parámetros es la que le
    dice al cliente **qué** corregir.
    """

    parseado = parse_request(sobre)
    assert isinstance(parseado, JsonRpcError)
    assert parseado.code is codigo


@pytest.mark.asyncio
async def test_una_notificacion_no_lleva_id() -> None:
    """Sin `id` es una notificación, y una notificación no recibe respuesta.

    Es lo que permite que un cliente dispare acciones sin esperar contestación. Si el servidor
    respondiera con `id: null`, el cliente no podría distinguir su notificación de una
    respuesta perdida.
    """

    parseado = parse_request({"jsonrpc": "2.0", "method": "tools/list"})
    assert not isinstance(parseado, JsonRpcError)
    assert parseado.is_notification is True
    assert (
        await handle_payload(
            {"jsonrpc": "2.0", "method": "tools/list"}, lambda _: {"ok": True}
        )
        is None
    )


@pytest.mark.asyncio
async def test_el_id_se_devuelve_identico() -> None:
    """El `id` de la respuesta es el de la petición, sin normalizar.

    Normalizarlo rompe la correlación en cuanto hay dos peticiones en vuelo con tipos
    distintos, que es lo que hace un agente que manda varias llamadas en paralelo.
    """

    respuesta = await handle_payload(
        {"jsonrpc": "2.0", "id": "abc-123", "method": "x"}, lambda _: "r"
    )
    assert respuesta is not None
    assert respuesta["id"] == "abc-123"

    numerico = await handle_payload(
        {"jsonrpc": "2.0", "id": 42, "method": "x"}, lambda _: "r"
    )
    assert numerico is not None
    assert numerico["id"] == 42


@pytest.mark.asyncio
async def test_un_lote_devuelve_una_respuesta_por_elemento() -> None:
    """Un lote produce una lista de respuestas, en orden."""

    lote = [
        {"jsonrpc": "2.0", "id": 1, "method": "a"},
        {"jsonrpc": "2.0", "id": 2, "method": "b"},
    ]
    respuestas = await handle_payload(lote, lambda _: "r")
    assert isinstance(respuestas, list)
    assert [r["id"] for r in respuestas] == [1, 2]


@pytest.mark.asyncio
async def test_un_fallo_en_una_herramienta_no_tumba_el_lote() -> None:
    """Un elemento que falla no impide que los demas lleguen con su resultado.

    Es la diferencia entre "reintenta la que fallo" y "reintenta todo sin saber que ya
    funciono". Con el fallo propagado, el agente no sabe cuales de las cuatro llamadas se
    ejecutaron, y volver a lanzarlas duplica trabajo y consume cuota.
    """

    def despachador(peticion: Any) -> str:
        if peticion.method == "malo":
            raise JsonRpcError(JsonRpcErrorCode.TOOL_ERROR, "fallo")
        return "bien"

    respuestas = await handle_payload(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "bueno"},
            {"jsonrpc": "2.0", "id": 2, "method": "malo"},
            {"jsonrpc": "2.0", "id": 3, "method": "bueno"},
        ],
        despachador,
    )
    assert isinstance(respuestas, list)
    assert "result" in respuestas[0]
    assert "error" in respuestas[1]
    assert respuestas[1]["error"]["code"] == int(JsonRpcErrorCode.TOOL_ERROR)
    assert "result" in respuestas[2]


@pytest.mark.asyncio
async def test_un_lote_solo_de_notificaciones_no_responde() -> None:
    """Un lote sin ninguna peticion que esperar no genera lista vacia.

    Un `[]` se interpretaria como "tres sobres mal formados" en lugar de "tres
    notificaciones aceptadas", que es lo que ocurrio.
    """

    respuesta = await handle_payload(
        [
            {"jsonrpc": "2.0", "method": "a"},
            {"jsonrpc": "2.0", "method": "b"},
        ],
        lambda _: "r",
    )
    assert respuesta is None


@pytest.mark.asyncio
async def test_un_lote_vacio_es_invalido() -> None:
    """Un lote sin elementos es un error, no un no-op."""

    respuesta = await handle_payload([], lambda _: "r")
    assert isinstance(respuesta, dict)
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.INVALID_REQUEST)


@pytest.mark.asyncio
async def test_un_fallo_no_controlado_no_escapa_al_cliente() -> None:
    """Una excepcion inesperada da `-32603` y el detalle se queda en el log.

    El mensaje al cliente no puede incluir la excepcion: un `IntegrityError` de SQLAlchemy
    trae los nombres de tabla y de columna, que son informacion de la base de datos de otro
    cliente potencial.
    """

    def despachador(peticion: Any) -> str:
        raise RuntimeError("detalle interno que no debe salir")

    respuesta = await handle_payload(
        {"jsonrpc": "2.0", "id": 1, "method": "x"}, despachador
    )
    assert isinstance(respuesta, dict)
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.INTERNAL_ERROR)
    assert "detalle interno" not in str(respuesta)


# --------------------------------------------------------------------------- #
# Autorización
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sin_token_es_401(integration_session: AsyncSession) -> None:
    """Sin credencial no se habla MCP. El `401` es HTTP a propósito.

    Encarcelarlo en un `200` obligaría a un cliente mal configurado a no poder distinguir
    "tu token no vale" de "la herramienta no existe".
    """

    session = integration_session
    assert session is not None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post("/api/v1/mcp", json=_catalogo())
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_token_solo_con_connect_no_alcanza(
    integration_session: AsyncSession,
) -> None:
    """`mcp:connect` sin `mcp:invoke` da `403`.

    `connect` es la puerta —poder hablar MCP— y `invoke` es poder ejecutar. Un token con solo
    la primera puede descubrir que la herramienta existe, que es lo que permite a un cliente
    bien escrito decir "tu token no alcanza" en vez de fallar al primer uso.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(session, (Scope.MCP_CONNECT,))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/mcp",
            json=_catalogo(),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert respuesta.status_code == 403


@pytest.mark.asyncio
async def test_tools_list_no_exige_el_scope_de_cada_herramienta(
    integration_session: AsyncSession,
) -> None:
    """Un token con los dos scopes de MCP puede ver el catálogo sin los de los recursos.

    Es deliberado: el catálogo es público para cualquier cliente conectado, y anunciarlo es lo
    que permite al usuario pedir el token correcto. El scope de la herramienta se comprueba
    al invocarla, no al listarla.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE)
    )
    respuesta = await _llamar(token, _catalogo())
    nombres = {herramienta["name"] for herramienta in respuesta["result"]["tools"]}
    assert nombres == set(TOOLS_POR_NOMBRE)


@pytest.mark.asyncio
async def test_invocar_una_herramienta_sin_su_scope_da_32001(
    integration_session: AsyncSession,
) -> None:
    """El scope de la herramienta se comprueba aparte de la puerta MCP.

    Sin esta comprobación, un token creado para "que el agente lea mis repositorios" —que
    lleva `mcp:invoke` y `repositories:read`— podria tambien lanzar pentests, porque las dos
    cosas pasan por la misma puerta.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.REPOSITORIES_READ),
    )
    respuesta = await _llamar(
        token,
        _invocar(
            "get_asset_inventory",
            {"asset_type": "SUBDOMAIN"},
        ),
    )
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.INSUFFICIENT_SCOPE)


@pytest.mark.asyncio
async def test_una_herramienta_inexistente_da_32601(
    integration_session: AsyncSession,
) -> None:
    """Una herramienta que no existe es `-32601`, no un error genérico.

    El codigo importa: `-32601` significa "corrige el nombre", y un `-32000` haría que un
    agente reintentara con lo mismo.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.ASSETS_READ)
    )
    respuesta = await _llamar(token, _invocar("herramienta_que_no_existe"))
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.METHOD_NOT_FOUND)
    # Y la respuesta lista las disponibles: el agente puede corregir sin inventar.
    assert "disponibles" in respuesta["error"]["data"]


@pytest.mark.asyncio
async def test_argumentos_desconocidos_da_32602(
    integration_session: AsyncSession,
) -> None:
    """Un argumento que la herramienta no declara se rechaza.

    Sin `extra="forbid"`, un cliente que mande `{"target": "..."}` a `trigger_pentest`
    obtendria una llamada aceptada y un escaneo lanzado sin destino, que es el peor resultado
    posible: el agente cree que lanzo el escaneo y no lanzo nada.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.REPOSITORIES_READ),
    )
    respuesta = await _llamar(
        token, _invocar("list_repositories", {"parametro_inventado": 1})
    )
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.INVALID_PARAMS)


# --------------------------------------------------------------------------- #
# Herramientas: datos reales y aislamiento
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_repositories_solo_ve_los_suyos(
    integration_session: AsyncSession,
) -> None:
    """El filtro por organización va siempre, y la respuesta lo declara.

    Se mide con `WHERE organization_id = ...`: contar filas globales de la base compartida
    es el antipatrón que ya hizo fallar tres pruebas de este proyecto.
    """

    session = integration_session
    assert session is not None
    org_a, token_a = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.REPOSITORIES_READ),
        prefijo="dueno",
    )
    _, token_b = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.REPOSITORIES_READ),
        prefijo="ajeno",
    )

    from backend.apps.repositories.models import Repository

    session.add(
        Repository(
            organization_id=org_a.id,
            provider="GITHUB",
            remote_repo_id="1",
            name="secreto",
            full_name="dueno/secreto",
            clone_url="https://example.invalid/secreto.git",
            default_branch="main",
        )
    )
    await session.commit()

    respuesta = await _llamar(token_b, _invocar("list_repositories"))
    texto = respuesta["result"]["content"][0]["text"]
    assert "dueno/secreto" not in texto
    assert "0 de 0" in texto

    respuesta_a = await _llamar(token_a, _invocar("list_repositories"))
    texto_a = respuesta_a["result"]["content"][0]["text"]
    assert "dueno/secreto" in texto_a

    total = (
        await session.execute(
            select(func.count(Repository.id)).where(
                Repository.organization_id == org_a.id
            )
        )
    ).scalar_one()
    assert total == 1


@pytest.mark.asyncio
async def test_list_repositories_trata_los_comodines_del_search_como_literales(
    integration_session: AsyncSession,
) -> None:
    r"""`%` y `_` se buscan literales en `list_repositories(search=...)`.

    ## Por qué este buscador no es un caso más

    Porque quien escribe el término **no es una persona**: es un agente. Escribe `api` y espera
    repositorios, y un filtro que en vez de filtrar devuelve la lista entera produce exactamente
    el fallo que un agente no sabe detectar: se lo queda como verdad y escanea el repositorio
    equivocado. El `_` es el caso real aquí porque los repositorios llevan `_` en el nombre con
    frecuencia —`web_app`, `api_admin`—, así que sin escape `web_app` también trae `webXapp` y el
    agente elige entre dos que no son el que buscaba.

    Se comprueba sobre la herramienta MCP por su interfaz de verdad —`tools/call` con JSON-RPC—,
    porque lo que hay que proteger aquí es lo que el agente recibe, no la consulta interna.

    ## Por qué el recuento esperado es «1 de 1» y no «0 de 0»

    Porque el término `%` **sí aparece** en uno de los cuatro repositorios sembrados, y el
    escape lo que garantiza es que se busque literal: el símbolo como símbolo, no como comodín.
    Un `%` a secas tiene que devolver la fila que de verdad lleva el símbolo —una— y no las
    cuatro.

    Y el denominador del recuento es el total **de la respuesta**, no el de la siembra: así que
    lo que hay que comprobar es el numerador y qué filas viajan. La comprobación fuerte es que
    las otras tres no aparecen en el texto, porque un agente lee ese texto y no el `total`.
    """

    session = integration_session
    assert session is not None
    org, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.REPOSITORIES_READ),
        prefijo="comodin-mcp",
    )

    from backend.apps.repositories.models import Repository

    # Cada par se diferencia solo en el carácter que se va a buscar: `_` contra `X` y `%` contra
    # `0`. Y cada término aparece en **una sola** fila del par, para que un fallo de escape se
    # cuente como un número y no como dos.
    session.add_all(
        [
            Repository(
                organization_id=org.id,
                provider="GITHUB",
                remote_repo_id=f"1-{indice}",
                name=nombre,
                full_name=f"acme/{nombre}",
                clone_url=f"https://example.invalid/{nombre}.git",
                default_branch="main",
            )
            for indice, nombre in enumerate(
                (
                    "web_app",
                    "webXapp",
                    "descuento-100%off",
                    "descuento-1000off",
                ),
                start=1,
            )
        ]
    )
    await session.commit()

    # Cada búsqueda tiene que devolver **una** fila de las cuatro sembradas. El recuento de la
    # respuesta es «1 de 1» porque su denominador es el total de lo que casó, no el del
    # inventario: lo que importa es el numerador y qué filas viajan en el texto.
    solo_porcentaje = await _llamar(token, _invocar("list_repositories", {"search": "%"}))
    texto_porcentaje = solo_porcentaje["result"]["content"][0]["text"]
    # `%` a secas devuelve **el único** repositorio que lleva el símbolo. Sin escape serían los
    # cuatro, que es exactamente lo que el agente se habría creído.
    assert "1 de 1" in texto_porcentaje, texto_porcentaje
    assert "acme/descuento-100%off" in texto_porcentaje, texto_porcentaje
    assert "acme/descuento-1000off" not in texto_porcentaje, texto_porcentaje

    con_subrayado = await _llamar(
        token, _invocar("list_repositories", {"search": "web_app"})
    )
    texto_subrayado = con_subrayado["result"]["content"][0]["text"]
    assert "1 de 1" in texto_subrayado, texto_subrayado
    assert "acme/web_app" in texto_subrayado, texto_subrayado
    assert "acme/webXapp" not in texto_subrayado, texto_subrayado

    con_texto = await _llamar(
        token, _invocar("list_repositories", {"search": "100%off"})
    )
    texto = con_texto["result"]["content"][0]["text"]
    assert "1 de 1" in texto, texto
    assert "acme/descuento-100%off" in texto, texto
    assert "acme/descuento-1000off" not in texto, texto


@pytest.mark.asyncio
async def test_get_asset_inventory_no_enseña_activos_ajenos(
    integration_session: AsyncSession,
) -> None:
    """El inventario no cruza tenants, y el recuento de la respuesta lo delata.

    Se comprueba lo que el agente **ve**: si la respuesta dijera `1 de 0` o similar, el
    recuento y las filas estan desacoplados y la conclusion seria falsa.
    """

    session = integration_session
    assert session is not None
    org_a, token_a = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.ASSETS_READ),
        prefijo="con-activos",
    )
    _, token_b = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.ASSETS_READ),
        prefijo="sin-activos",
    )

    dominio = VerifiedDomain(
        organization_id=org_a.id,
        domain_name="a.example",
        verification_token=uuid.uuid4().hex,
        is_verified=True,
    )
    session.add(dominio)
    await session.flush()
    session.add(
        DiscoveredAsset(
            domain_id=dominio.id,
            organization_id=org_a.id,
            asset_type="SUBDOMAIN",
            value="secreto.a.example",
        )
    )
    await session.commit()

    texto_b = (
        await _llamar(token_b, _invocar("get_asset_inventory"))
    )["result"]["content"][0]["text"]
    assert "secreto.a.example" not in texto_b

    texto_a = (
        await _llamar(token_a, _invocar("get_asset_inventory"))
    )["result"]["content"][0]["text"]
    assert "secreto.a.example" in texto_a
    # El dominio aparece con su activo, que es la diferencia entre un dato y un hallazgo.
    assert "a.example" in texto_a

    total = (
        await session.execute(
            select(func.count(DiscoveredAsset.id)).where(
                DiscoveredAsset.organization_id == org_a.id
            )
        )
    ).scalar_one()
    assert total == 1


@pytest.mark.asyncio
async def test_get_vulnerability_summary_excluye_lo_resuelto(
    integration_session: AsyncSession,
) -> None:
    """El resumen cuenta solo lo que sigue abierto.

    Contar tambien los `FIXED` haria que el numero creciera sin que la postura cambie, que es
    exactamente lo contrario de lo que un resumen sirve para decidir.
    """

    session = integration_session
    assert session is not None
    org, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.VULNERABILITIES_READ),
    )

    from backend.apps.pentests.models import PentestRun
    from backend.apps.vulnerabilities.models import Vulnerability

    # `run_id` es obligatorio: un hallazgo sin escaneo que lo produjera no tiene de donde
    # sacarse, y sembrarlo con un UUID inventado crearia una fila que el esquema prohibe.
    escaneo = PentestRun(
        organization_id=org.id,
        target_type="REPOSITORY",
        target_identifier=str(uuid.uuid4()),
    )
    session.add(escaneo)
    await session.flush()

    for indice, estado in enumerate(("OPEN", "FIXED", "IGNORED")):
        session.add(
            Vulnerability(
                organization_id=org.id,
                run_id=escaneo.id,
                title=f"Hallazgo {indice}",
                description="Descripcion del hallazgo de prueba.",
                severity="HIGH" if indice == 0 else "LOW",
                cvss_score=7.0,
                affected_target="api.example",
                poc_reproduction_raw="curl -i https://api.example",
                status=estado,
            )
        )
    await session.commit()

    respuesta = await _llamar(token, _invocar("get_vulnerability_summary"))
    cuerpo = respuesta["result"]["content"][0]["text"]
    assert '"total_abiertas": 1' in cuerpo
    assert '"HIGH": 1' in cuerpo


@pytest.mark.asyncio
async def test_un_tipo_de_activo_invalido_da_los_admitidos(
    integration_session: AsyncSession,
) -> None:
    """Un `asset_type` desconocido responde con la lista, no con un error de validacion.

    El agente necesita saber que valores existen. Un `ValidationError` de Pydantic habla de
    un campo del backend y no le da nada con lo que corregir la llamada.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.ASSETS_READ)
    )
    respuesta = await _llamar(
        token, _invocar("get_asset_inventory", {"asset_type": "MAQUINA"})
    )
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.INVALID_PARAMS)
    assert respuesta["error"]["data"]["asset_type_admitidos"] == [
        "SUBDOMAIN",
        "IP_ADDRESS",
        "API_ENDPOINT",
    ]


@pytest.mark.asyncio
async def test_trigger_pentest_no_puede_escapar_del_tenant(
    integration_session: AsyncSession,
) -> None:
    """Un destino que no es del tenant no llega a la cola.

    Se comprueba que se rechaza **antes** del cobro: si el cobro ocurriera primero, el
    intento fallido ya habria costado credits, y un agente que reintenta con identificadores
    inventados podria vaciar el saldo probando.
    """

    session = integration_session
    assert session is not None
    org_ajeno, _ = await _tenant_con_token(
        session, (Scope.MCP_CONNECT,), prefijo="ajeno"
    )
    _, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.PENTESTS_CREATE),
    )

    from backend.apps.billing.models import CreditLedger
    from backend.apps.pentests.models import PentestRun

    respuesta = await _llamar(
        token,
        _invocar(
            "trigger_pentest",
            {"target_type": "REPOSITORY", "target_identifier": str(org_ajeno.id)},
        ),
    )
    assert "error" in respuesta, respuesta

    runs = (
        await session.execute(
            select(func.count(PentestRun.id)).where(
                PentestRun.organization_id == org_ajeno.id
            )
        )
    ).scalar_one()
    assert runs == 0
    asientos = (
        await session.execute(
            select(func.count(CreditLedger.id)).where(
                CreditLedger.organization_id == org_ajeno.id
            )
        )
    ).scalar_one()
    assert asientos == 0


@pytest.mark.asyncio
async def test_trigger_pentest_rechaza_una_url_que_no_es_repositorio_propio(
    integration_session: AsyncSession,
) -> None:
    """Un `REPOSITORY` que no está en la lista del workspace se rechaza.

    ## Por qué esta prueba y no solo la del repositorio ajeno

    Porque son **dos** agujeros distintos, y una comprobación que arregla uno puede dejar el
    otro. Antes de la corrección, `target_identifier` llegaba al worker tal cual y el worker lo
    clonaba: `REPOSITORY` no significaba "uno de tus repositorios" sino "este texto". Con un
    tenant sin ningún repositorio —el caso de esta prueba— el endpoint aceptaba cualquier URL,
    incluida la de un tercero, y el motor la clonaba con las credenciales de ese tenant.

    Un repositorio ajeno habría sido el síntoma visible; una URL suelta no lo era, porque
    "apuntar el escaneo donde yo quiera" parece justo para lo que sirve un escaneo. Distinguir
    los dos exige un workspace **sin** repositorios, y por eso el caso se monta así y no con un
    repositorio ajeno.
    """

    session = integration_session
    assert session is not None
    org, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.PENTESTS_CREATE),
    )
    # Los identificadores a UUID planos antes de la petición, por la misma razón que en el
    # caso anterior: la petición hace `commit` sobre la sesión compartida del fixture y deja
    # las instancias de ORM expiradas.
    id_org = org.id

    from backend.apps.billing.models import CreditLedger
    from backend.apps.pentests.models import PentestRun
    from backend.apps.repositories.models import Repository

    repositorios = (
        await session.execute(
            select(func.count(Repository.id)).where(
                Repository.organization_id == id_org
            )
        )
    ).scalar_one()
    assert repositorios == 0, "el caso exige un workspace sin repositorios"

    respuesta = await _llamar(
        token,
        _invocar(
            "trigger_pentest",
            {
                "target_type": "REPOSITORY",
                "target_identifier": "https://github.com/alguien/que-no-es-tu-yo",
            },
        ),
    )
    assert respuesta["error"]["code"] == int(JsonRpcErrorCode.INSUFFICIENT_SCOPE)
    assert respuesta["error"]["data"]["reintentable"] is False

    for tabla in (PentestRun, CreditLedger):
        filas = (
            await session.execute(
                select(func.count(tabla.id)).where(tabla.organization_id == id_org)
            )
        ).scalar_one()
        assert filas == 0


@pytest.mark.asyncio
async def test_trigger_pentest_acepta_un_repositorio_propio(
    integration_session: AsyncSession,
) -> None:
    """Un repositorio del propio workspace se encola, y por el mismo camino que el panel.

    Es la mitad positiva del caso anterior. Sin ella, "rechaza lo que no es suyo" se puede
    cumplir rechazando **todo**: si nada llegara nunca a encolarse, la prueba del rechazo
    pasaría igual. Aquí el despachador es un doble, así que la prueba no necesita broker, y se
    comprueba que el `task_id` llegó a la fila.
    """

    session = integration_session
    assert session is not None
    org, token = await _tenant_con_token(
        session,
        (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.PENTESTS_CREATE),
    )

    from backend.apps.pentests.models import PentestRun
    from backend.apps.repositories.models import GitProviderEnum, Repository

    # El workspace nace con saldo cero, y un escaneo de 10 créditos se rechaza con `-32000`
    # antes de llegar a la cola. Se le da saldo para que la prueba mida lo que dice medir —
    # que un repo propio sí se encola — y no el camino del cobro, que ya tiene sus pruebas.
    org.credit_balance = Decimal("100")
    await session.commit()

    repositorio = Repository(
        organization_id=org.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="9",
        name="app",
        full_name="atacante/app",
        clone_url="https://example.invalid/app.git",
        default_branch="main",
    )
    session.add(repositorio)
    await session.commit()
    id_repositorio = repositorio.clone_url
    id_organizacion = org.id

    app.dependency_overrides[get_dispatch_pentest_run] = (
        lambda: (lambda run_id: f"task-{run_id}")
    )
    try:
        respuesta = await _llamar(
            token,
            _invocar(
                "trigger_pentest",
                {"target_type": "REPOSITORY", "target_identifier": id_repositorio},
            ),
        )
    finally:
        app.dependency_overrides.pop(get_dispatch_pentest_run, None)

    assert "result" in respuesta, respuesta
    assert "task-" in respuesta["result"]["content"][0]["text"]

    encolados = (
        (
            await session.execute(
                select(PentestRun).where(
                    PentestRun.organization_id == id_organizacion
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(encolados) == 1
    assert encolados[0].celery_task_id is not None


# --------------------------------------------------------------------------- #
# Perfil `core`
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sin_perfil_se_exponen_todas_las_herramientas(
    integration_session: AsyncSession,
) -> None:
    """Sin `profile`, el catálogo es el completo. Es el comportamiento por defecto."""

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE)
    )
    respuesta = await _llamar(token, _catalogo())
    nombres = {herramienta["name"] for herramienta in respuesta["result"]["tools"]}
    assert nombres == set(TOOLS_POR_NOMBRE)


@pytest.mark.asyncio
async def test_el_perfil_core_solo_expone_las_esenciales(
    integration_session: AsyncSession,
) -> None:
    """`profile=core` recorta el catálogo a las dos herramientas del perfil."""

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE)
    )
    respuesta = await _llamar(token, _catalogo(), profile="core")
    nombres = {herramienta["name"] for herramienta in respuesta["result"]["tools"]}

    assert nombres == PERFILES["core"]
    # El recorte tiene que ser real, no un recorte de la mitad: las cuatro herramientas
    # declaradas y dos visibles.
    assert len(TOOLS_POR_NOMBRE) > len(PERFILES["core"])


@pytest.mark.asyncio
async def test_el_perfil_core_tambien_se_aplica_al_invocar(
    integration_session: AsyncSession,
) -> None:
    """Invocar una herramienta fuera del perfil da `-32601`, aunque el scope alcance.

    Es la prueba que sostiene el perfil. Si el filtro viviera solo en `tools/list`, esta
    prueba fallaria al quitarlo, que es justo lo que tiene que pasar: un perfil que se puede
    saltar llamando por el nombre no es un perfil, es un filtro de la lista de herramientas.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session,
        (
            Scope.MCP_CONNECT,
            Scope.MCP_INVOKE,
            Scope.REPOSITORIES_READ,
            Scope.ASSETS_READ,
        ),
    )
    fuera_del_perfil = next(
        nombre for nombre in TOOLS_POR_NOMBRE if nombre not in PERFILES["core"]
    )
    respuesta = await _llamar(
        token, _invocar(fuera_del_perfil, {}), profile="core"
    )
    assert respuesta["error"]["code"] == JsonRpcErrorCode.METHOD_NOT_FOUND
    # El detalle no puede confirmar que la herramienta existe: solo que el perfil no la trae.
    assert respuesta["error"]["data"]["perfil"] == "core"
    assert fuera_del_perfil not in respuesta["error"]["data"]["disponibles"]


@pytest.mark.asyncio
async def test_una_herramienta_del_perfil_sigue_funcionando(
    integration_session: AsyncSession,
) -> None:
    """Lo que el perfil deja pasar, lo deja pasar de verdad."""

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE, Scope.VULNERABILITIES_READ)
    )
    respuesta = await _llamar(
        token, _invocar("get_vulnerability_summary", {}), profile="core"
    )
    assert "error" not in respuesta


@pytest.mark.asyncio
async def test_un_perfil_inexistente_da_422(
    integration_session: AsyncSession,
) -> None:
    """Un perfil mal escrito no cae al perfil completo en silencio.

    El resultado de ignorarlo seria un token con mas herramientas de las que el usuario cree
    haber pedido, y eso no se detecta hasta un incidente.
    """

    session = integration_session
    assert session is not None
    _, token = await _tenant_con_token(
        session, (Scope.MCP_CONNECT, Scope.MCP_INVOKE)
    )
    await _llamar(token, _catalogo(), status_esperado=422, profile="cor")
