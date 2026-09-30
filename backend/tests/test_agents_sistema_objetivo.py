"""El sistema para el que el operador declara desplegar el agente.

## Qué se cubre

Que el alta acepte el sistema, que lo guarde, que lo devuelva en el listado, y que **no** lo
confunde con el que mide el agente.

## Por qué esto necesita un test propio y no una línea en el fichero de controles

Porque el defecto que justifica el campo no es un fallo de ejecución, es un campo que **no
existía**: sin él, la columna de plataforma de la tabla salía con «—» en un alta recién hecha, y
la pregunta que el operador tenía en ese momento —«¿esto corre en su Windows Server?»— no tenía
ninguna forma de contestarse porque no había dónde declarar la respuesta.

Un test de «el alta funciona» no lo habría detectado: el alta funcionaba.

## Por qué los dos sistemas se comprueban por separado

Porque son dos hechos y confundirlos es el error que hace inutilizable la columna:

- `sistema_objetivo` lo **dice** el operador, en el alta, y decide qué instrucciones ve.
- `platform_hint` lo **mide** el agente, en su primer latido, y sirve para diagnosticar.

Si el medido sobrescribiera al declarado, un operador que se equivocara al declarar perdería la
pista, y el agente que sí funciona acabaría con la columna diciendo que no.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import ScannerAgent
from backend.apps.organizations.models import RoleEnum
from backend.main import app
from backend.tests.test_agents_controles import _workspace

#: Sin esto la fixture `integration_session` devuelve `None` —porque comprueba el marker— y
#: todos los tests fallan con `assert None is not None`, que no dice nada del sistema objetivo.
#: El resto de los ficheros de integración del backend lo declaran igual.
pytestmark = pytest.mark.integration

SISTEMAS = ["linux", "windows", "macos", "desconocido"]


async def _alta(cabeceras: dict[str, str], nombre: str, sistema: str) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/agents",
            json={"name": nombre, "sistema_objetivo": sistema},
            headers=cabeceras,
        )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("sistema", SISTEMAS)
async def test_el_alta_guarda_el_sistema_declarado(
    integration_session: AsyncSession, sistema: str
) -> None:
    """Los cuatro valores del enum llegan a la columna, uno a uno.

    Se parametriza porque el defecto que de verdad se cuela aquí no es «no guarda el sistema»,
    que se vería en el primer caso, sino «guarda `linux` y se come los otros tres»: el `else` que
    mira el primero y devuelve el de por defecto. Un solo caso no lo ve.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )

    datos = await _alta(cabeceras, f"agente-{sistema}", sistema)

    assert datos["sistema_objetivo"] == sistema, "la respuesta no devuelve lo que se mandó"

    fila = (
        await integration_session.execute(
            select(ScannerAgent).where(ScannerAgent.id == uuid.UUID(datos["id"]))
        )
    ).scalar_one()
    assert fila.sistema_objetivo == sistema, "la columna no guarda lo que se mandó"


@pytest.mark.asyncio
async def test_un_alta_sin_sistema_usa_desconocido_y_no_falla(
    integration_session: AsyncSession,
) -> None:
    """Un alta antigua, o un cliente que no manda el campo, tiene que funcionar.

    Y tiene que acabar en `desconocido`, no en `NULL`: la columna es `NOT NULL`, y un `None`
    devolvería un `500` en un camino que antes funcionaba —el alta más simple de todas— por
    añadir un campo opcional.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/agents", json={"name": "sin-sistema"}, headers=cabeceras
        )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["sistema_objetivo"] == "desconocido"


@pytest.mark.asyncio
async def test_un_sistema_que_no_existe_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """El enum es cerrado a propósito: un sistema inventado no se guarda como texto libre.

    ## Por qué esto se comprueba con un `422` y no con un `500`

    Porque la respuesta tiene que distinguir «no es un sistema de los que conozco» de «el
    sistema se cayó», y un `422` lo dice sin que haya que leer el log. Con un `str` libre, un
    `Win2022` se guardaba, la guía no reconocía el valor y caía en la variante de Linux: un
    fallo que no da error en ninguna parte y solo se ve cuando el agente no arranca.

    El `422` concreto es el de validación del modelo de Pydantic, no el del `ReglaDeNegocio` del
    servicio: la lista de sistemas es una forma del contrato, y las formas las comprueba el
    esquema.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/agents",
            json={"name": "inventado", "sistema_objetivo": "Win2022"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.asyncio
async def test_el_sistema_declarado_no_lo_toca_el_latido_del_agente(
    integration_session: AsyncSession,
) -> None:
    """Un operador puede declarar `windows` y el agente medir otra cosa: los dos se guardan.

    ## Por qué el medido **no** sobrescribe al declarado

    Porque son preguntas distintas. El declarado decide qué instrucciones se le enseñan al
    operador; el medido sirve para diagnosticar. Si el medido ganara, un despliegue funcionando
    acabaría con la columna diciendo `linux` porque eso es lo que `platform.system()` devolvió en
    la máquina, y el operador leería que puso mal el sistema en un alta que funcionó.

    ## Por qué se comprueba con los dos declarados distintos

    Porque el caso interesante no es «el latido rellena el campo», sino «el latido **no** pisa el
    campo que no le toca». Un test que solo mira que `platform_hint` se rellena pasa aunque el
    servicio esté haciendo un `UPDATE` de todo lo que tiene delante.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    datos = await _alta(cabeceras, "declarado-windows", "windows")
    agente_id = datos["id"]
    token = datos["token"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        latido = await client.post(
            "/api/v1/agents/heartbeat",
            json={"agent_version": "0.1.0", "platform_hint": "linux/amd64"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert latido.status_code == 200, latido.text

    fila = (
        await integration_session.execute(
            select(ScannerAgent).where(ScannerAgent.id == uuid.UUID(agente_id))
        )
    ).scalar_one()
    # El medido se guarda.
    assert fila.platform_hint == "linux/amd64"
    # Y el declarado se queda como el operador lo puso.
    assert fila.sistema_objetivo == "windows", "el latido pisó el sistema declarado"


@pytest.mark.asyncio
async def test_el_listado_devuelve_el_sistema_de_cada_agente(
    integration_session: AsyncSession,
) -> None:
    """El listado trae el de cada fila, no el del primero ni el de la última.

    Un `else` que devuelva siempre `linux` pasa los tests de alta —que miran la respuesta del
    alta, no el listado— y falla aquí. Por eso el caso necesita dos filas de sistemas distintos.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    await _alta(cabeceras, "uno-windows", "windows")
    await _alta(cabeceras, "dos-linux", "linux")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listado = await client.get("/api/v1/agents", headers=cabeceras)

    assert listado.status_code == 200, listado.text
    por_nombre = {a["name"]: a["sistema_objetivo"] for a in listado.json()["items"]}
    assert por_nombre["uno-windows"] == "windows"
    assert por_nombre["dos-linux"] == "linux"


@pytest.mark.asyncio
async def test_la_columna_existe_y_no_admite_null(
    integration_session: AsyncSession,
) -> None:
    """La base dice `NOT NULL`, no solo el modelo.

    ## Por qué se pregunta a la base y no al ORM

    Porque son dos cosas distintas y el defecto estaba en una de ellas. La migración crea la
    columna con `NOT NULL` y **retira** el `server_default` después de rellenarla, que es lo
    correcto para producción. El modelo, por su parte, lleva un `default` de Python.

    Si alguien deja la columna nullable sin darse cuenta, la fila nace sin sistema y el único
    síntoma es una columna en blanco en la tabla del panel —el mismo síntoma que tenía el campo
    inexistente— y ningún test que mire el esquema se entera. Este lo mira.

    Y por eso se comprueba **también** que existe: una migración que no se aplicó deja la
    columna sin crear, y `information_schema` no devuelve ninguna fila en vez de devolverla
    nullable. Un `assert` sobre una fila que no existe pasa.
    """

    assert integration_session is not None
    fila = (
        await integration_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'scanner_agents' AND column_name = 'sistema_objetivo'"
            )
        )
    ).fetchone()

    assert fila is not None, "la columna no existe: la migracion no esta aplicada"
    assert fila[0] == "NO", "la columna admite null y una fila puede nacer sin sistema"

    # Y el modelo tiene que decir lo mismo.
    #
    # ## Por qué esta segunda comprobación es la que de verdad faltaba
    #
    # ## Por qué el modelo y la base tienen que coincidir
    #
    # Porque son dos declaraciones de lo mismo, y pueden divergir **sin que nada falle**: la
    # base es `NOT NULL` porque la migración lo puso, y el modelo es `nullable=False` porque
    # alguien lo escribió. Si uno dice `nullable=True` y el otro sigue en `NO`, hoy no pasa nada
    # —la columna de la base manda— y el día que alguien genere una migración automática, esa
    # herramienta se lee el modelo, ve `nullable=True` y **suelta un `DROP NOT NULL`** que nadie
    # ha revisado, porque la línea que lo provoca está en un fichero que no se ha tocado.
    #
    # ## Por qué se compara el texto tal cual
    #
    # ## Por qué se comparan los dos valores de la consulta
    #
    # Porque `information_schema` devuelve `'NO'` y `'YES'` en mayúsculas, y el modelo declara un
    # `bool`. Comparar la cadena con el booleano sería comparar cosas de dos tipos distintos, y
    # eso es exactamente el tipo de comprobación que se equivoca sin avisar.
    declarado_en_el_modelo = ScannerAgent.__table__.c.sistema_objetivo.nullable
    esperado = "YES" if declarado_en_el_modelo else "NO"
    assert fila[0] == esperado, (
        f"el modelo dice nullable={declarado_en_el_modelo} pero la base dice {fila[0]}: "
        "una migracion autogenerada leeria el modelo y soltaria un DROP NOT NULL que nadie "
        "ha pedido"
    )


@pytest.mark.asyncio
async def test_una_fila_creada_a_mano_nace_con_sistema(
    integration_session: AsyncSession,
) -> None:
    """El `default` del modelo cubre las rutas que no pasan por el esquema de Pydantic.

    ## Por qué se inserta y no se mira el atributo

    Porque el `default` de una `mapped_column` se aplica en el `INSERT`, no en el
    `__init__`: una fila construida y todavía no volcada tiene el atributo a `None`, y comprobar
    eso daría verde con el `default` quitado. Lo que importa es lo que acaba en la tabla, y eso
    hay que insertarlo para verlo.

    ## Por qué este test y no un `assert` sobre la constante

    Porque hay caminos que crean un `ScannerAgent` sin pasar por `AgentCreate`: los scripts de
    demostración, los tests que montan una fila a mano, y cualquier futuro endpoint interno. Con
    la columna `NOT NULL` y sin `server_default`, esos caminos fallaban con
    «null value in column "sistema_objetivo"» y el fallo salía en el primer `flush`, lejos de la
    causa. Comprobar que insertar así funciona es lo que dice que el hueco está cerrado.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    assert cabeceras  # La alta de verdad necesita cabeceras; esta fila, no.

    fila = ScannerAgent(
        organization_id=organization.id,
        name="creada-a-mano",
        token_hash="a" * 64,
        token_prefix="mgf_agent_0000",
    )
    assert fila.sistema_objetivo is None, (
        "el atributo se rellena en el INSERT, no al construir: si ya trae valor aqui, el "
        "default se ha puesto en el sitio equivocado"
    )

    integration_session.add(fila)
    await integration_session.flush()

    assert fila.sistema_objetivo == "desconocido", (
        "una fila creada sin nombrar el sistema debe nacer con 'desconocido'"
    )
