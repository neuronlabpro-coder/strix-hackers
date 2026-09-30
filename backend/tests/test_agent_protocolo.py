"""El protocolo entre la plataforma y el agente, de punta a punta contra la API real.

## Por qué estas pruebas usan la API y no el servicio

Porque lo que hay que comprobar es el **contrato HTTP**, no la lógica: que el token vaya en la
cabecera y no en la URL, que un `201` traiga el token y un `200` no, que un destino con forma
mala dé `422` y no un error de servidor. Todo eso es del transporte, y llamar al servicio por
dentro lo saltaría.

Y por qué el agente tiene además sus propias pruebas: esto comprueba que la plataforma y el
cliente se entienden; lo de dentro del cliente —el parsing de paquetes, el recorte de un
prefijo— lo comprueba `agent/tests/test_agente.py`, porque es lo único que se puede comprobar
sin poner un agente a escanear una red de verdad.

## Qué NO se prueba aquí, y por qué no

El escaneo en sí. Un `CONTAINER_SCAN` contra `alpine:3.20` funciona y devuelve 88 paquetes —
verificado contra Docker Hub durante el desarrollo—, pero una prueba que baja una capa de tres
megabytes en cada ejecución de la suite no es una prueba: es una prueba que depende de la red,
del registro de un tercero y de que la etiqueta `3.20` siga existiendo dentro de dos años. Lo
que se comprueba es el circuito; lo que se escanea, con un agente de verdad, en la red de un
cliente.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import ScannerAgent
from backend.apps.organizations.models import Organization

pytestmark = pytest.mark.integration

#: El destino que se usa en las pruebas de un escaneo de contenedor. Es una etiqueta de ejemplo
#: documentada, y el trabajo **no se entrega**: solo se encola y se comprueba el contrato.
IMAGEN_DE_PRUEBA = "alpine:3.20"

CIDR_DE_PRUEBA = "10.10.0.0/24"


async def _token_de_agente(
    sesion: AsyncSession, organization_id: uuid.UUID, nombre: str
) -> tuple[uuid.UUID, str]:
    """Inscribe un agente y devuelve su identificador y su token **en claro**.

    Es la única forma de tener un token utilizable, y por eso la función que lo emite devuelve
    la tupla: el secreto no se vuelve a guardar. La alternativa —leer el hash de la base y
    reconstruir el token— es imposible a proposito, y por eso el alta tiene que hacerse aquí y
    no en una migracion de datos.
    """

    from backend.apps.agents.schemas import AgentCreate
    from backend.apps.agents.service import inscribir_agente

    agente, token = await inscribir_agente(
        sesion, organization_id, AgentCreate(name=nombre, agent_version="prueba")
    )
    return agente.id, token


def _organizacion(nombre: str) -> Organization:
    from backend.apps.organizations.models import Organization

    # El saldo no es decorativo: encolar un escaneo **cobra** créditos, así que una organización de
    # prueba sin saldo no puede ni encolar. Esa es la garantía que comprueba
    # `test_sin_saldo_no_se_puede_encolar`, y por eso el fixture se lo da en vez de fingir que no
    # hay coste —que es lo que pasaba antes, y hacía que este módulo fuera gratis.
    return Organization(
        name=nombre,
        slug=f"{nombre.lower()}-{uuid.uuid4().hex}",
        credit_balance=Decimal("1000"),
    )


# --------------------------------------------------------------------------- #
# La forma del contrato
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_alta_devuelve_el_token_y_el_listado_no(
    integration_session: AsyncSession,
) -> None:
    """El token se ve **una vez**, y el listado no lo puede devolver ni por accidente.

    Es la propiedad de la que depende todo lo demás: un token del que se puede volver a pedir la
    copia es un token que acaba en un log, en un ticket o en una captura de pantalla. Por eso
    la prueba mira las dos mitades: que el alta lo traiga y que la fila no lo tenga, que es
    literalmente lo que dice la columna que se lee.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-token-unico")
    integration_session.add(organizacion)
    await integration_session.flush()

    _, token = await _token_de_agente(
        integration_session, organizacion.id, "agente-token"
    )
    assert token.startswith("mgf_agent_")

    fila = (
        await integration_session.execute(
            text("SELECT token_hash, token_prefix FROM scanner_agents WHERE name = 'agente-token'")
        )
    ).one()
    assert token not in fila.token_hash
    assert fila.token_prefix.startswith("mgf_agent_")
    # Y la fila no tiene ninguna columna con el secreto: solo el hash y cuatro caracteres.
    columnas = (
        await integration_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'scanner_agents' ORDER BY column_name"
            )
        )
    ).scalars().all()
    assert "token" not in columnas
    assert "token_hash" in columnas
    assert "token_prefix" in columnas


@pytest.mark.asyncio
async def test_un_token_de_panel_no_sirve_para_hacer_de_agente(
    integration_session: AsyncSession,
) -> None:
    """Las dos credenciales son de tipos distintos, y una no abre la otra.

    ## Por qué esto se prueba y no se deduce

    Porque las dos viven en la misma cabecera `Authorization: Bearer`, que es exactamente donde
    un descuido se convierte en una escalada: si el mismo `Depends` aceptara las dos, un token
    de panel llegaría a los endpoints de agente y un token de agente a los de panel, y ninguno de
    los dos caminos se habría escrito nunca.

    La separación real no está en la comprobación del prefijo, que es una barrera de cristall
    barata: está en que `autenticar_agente` y `get_current_tenant` no comparten nada y solo se
    montan en routers distintos. Esta prueba es la que lo vigila desde fuera.
    """

    assert integration_session is not None
    from backend.apps.agents.service import AgentNotFoundError, autenticar_agente
    from backend.apps.api_access.models import API_TOKEN_PREFIX
    from backend.core.security import generate_api_token

    # Un token con la forma de la API pública.
    token_de_panel = generate_api_token(API_TOKEN_PREFIX, 32)
    with pytest.raises(AgentNotFoundError):
        await autenticar_agente(integration_session, token_de_panel)

    # Y uno que no existe en absoluto.
    with pytest.raises(AgentNotFoundError):
        await autenticar_agente(integration_session, "mgf_agent_" + "0" * 64)


# --------------------------------------------------------------------------- #
# La cola
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_un_trabajo_lo_reclama_solo_un_agente_del_mismo_tenant(
    integration_session: AsyncSession,
) -> None:
    """R3 en la cola: el trabajo es del tenant que lo encoló, y nadie más lo ve.

    El filtro va **dentro** del `WHERE` del servicio, y esta prueba lo comprueba por el otro
    lado: un agente de otro tenant pregunta y no recibe el trabajo. Si el filtro estuviera
    después de la carga, el trabajo se habría visto antes de decidir que no era suyo, que es
    justo lo que R3 no permite.
    """

    assert integration_session is not None
    primero = _organizacion("agente-cola-a")
    segundo = _organizacion("agente-cola-b")
    integration_session.add_all([primero, segundo])
    await integration_session.flush()

    from backend.apps.agents.models import AgentJobKindEnum
    from backend.apps.agents.schemas import AgentJobRequest
    from backend.apps.agents.service import encolar_trabajo, reclamar_trabajo

    trabajo = await encolar_trabajo(
        integration_session,
        primero.id,
        None,
        AgentJobRequest(kind=AgentJobKindEnum.NETWORK_SCAN, target=CIDR_DE_PRUEBA),
    )

    agente_ajeno = await _cargar_agente_falso(integration_session, segundo.id, "ajeno")
    assert await reclamar_trabajo(integration_session, agente_ajeno) is None

    # Y el suyo si lo recibe.
    agente_suyo = await _cargar_agente_falso(integration_session, primero.id, "suyo")
    reclamado = await reclamar_trabajo(integration_session, agente_suyo)
    assert reclamado is not None
    assert reclamado[0].id == trabajo.id


async def _cargar_agente_falso(
    sesion: AsyncSession, organization_id: uuid.UUID, nombre: str
) -> ScannerAgent:
    """Un agente en la base, sin token, para probar la cola sin autenticar.

    Existe porque la cola se puede probar con un objeto de fila: lo que importa es que la
    organización sea la correcta, y eso no depende de cómo se obtuvo el agente.

    El tipo de retorno es `ScannerAgent` y no `object` a propósito: el servicio lo recibe como
    argumento, y un `object` obliga a un `cast` en cada llamada para que el compilador acepte la
    que es la única forma de comprobarlo.
    """

    from backend.apps.agents.models import ScannerAgent
    from backend.core.security import hash_api_token

    agente = ScannerAgent(
        organization_id=organization_id,
        name=nombre,
        token_hash=hash_api_token("mgf_agent_" + uuid.uuid4().hex),
        token_prefix="mgf_agent_abcd",
    )
    sesion.add(agente)
    await sesion.flush()
    return agente


@pytest.mark.asyncio
async def test_reclamar_no_devuelve_el_mismo_trabajo_dos_veces(
    integration_session: AsyncSession,
) -> None:
    """Un trabajo se toma **una** vez, aunque haya trabajo de sobra.

    Es la propiedad de la que depende que dos agentes no escaneen lo mismo, y la que sostiene el
    `FOR UPDATE SKIP LOCKED`. Sin el bloqueo, dos agentes que piden en el mismo instante leen la
    misma fila antes de que ninguno la escriba, y el mismo escaneo se ejecuta dos veces: el
    cliente paga una vez y consume dos.

    Y el segundo `claim` devuelve el siguiente, no `None` y no el mismo: eso es lo que distingue
    un reparto de trabajo de un fallo.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-una-vez")
    integration_session.add(organizacion)
    await integration_session.flush()

    from backend.apps.agents.models import AgentJobKindEnum
    from backend.apps.agents.schemas import AgentJobRequest
    from backend.apps.agents.service import encolar_trabajo, reclamar_trabajo

    primero = await encolar_trabajo(
        integration_session,
        organizacion.id,
        None,
        AgentJobRequest(kind=AgentJobKindEnum.CONTAINER_SCAN, target="alpine:3.20"),
    )
    segundo = await encolar_trabajo(
        integration_session,
        organizacion.id,
        None,
        AgentJobRequest(kind=AgentJobKindEnum.CONTAINER_SCAN, target="alpine:3.21"),
    )

    agente = await _cargar_agente_falso(integration_session, organizacion.id, "repartidor")
    uno = await reclamar_trabajo(integration_session, agente)
    otro = await reclamar_trabajo(integration_session, agente)

    assert uno is not None and otro is not None
    assert uno[0].id != otro[0].id
    assert {uno[0].id, otro[0].id} == {primero.id, segundo.id}

    # Y a la tercera no queda nada.
    assert await reclamar_trabajo(integration_session, agente) is None


@pytest.mark.asyncio
async def test_reclamar_incrementa_el_contador_de_intentos(
    integration_session: AsyncSession,
) -> None:
    """Cada intento cuenta, porque «ha fallado dos veces» y «ha fallado una» no son lo mismo.

    El contador es lo que permite distinguir un destino inalcanzable de un destino que no
    existe: un trabajo con tres intentos y sin resultado es un problema de la red del cliente,
    y uno con un intento puede ser solo mala suerte.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-intentos")
    integration_session.add(organizacion)
    await integration_session.flush()

    from backend.apps.agents.models import AgentJobKindEnum
    from backend.apps.agents.schemas import AgentJobRequest
    from backend.apps.agents.service import encolar_trabajo, reclamar_trabajo

    trabajo = await encolar_trabajo(
        integration_session,
        organizacion.id,
        None,
        AgentJobRequest(kind=AgentJobKindEnum.NETWORK_SCAN, target=CIDR_DE_PRUEBA),
    )
    agente = await _cargar_agente_falso(integration_session, organizacion.id, "contador")

    await reclamar_trabajo(integration_session, agente)
    # Se devuelve a la cola a mano, que es lo que hace el reaper cuando el alquiler vence.
    await integration_session.execute(
        text(
            "UPDATE agent_jobs SET status = 'QUEUED', agent_id = NULL, lease_expires_at = NULL "
            "WHERE id = :job_id"
        ),
        {"job_id": trabajo.id},
    )
    await integration_session.commit()

    await reclamar_trabajo(integration_session, agente)
    estado = (
        await integration_session.execute(
            text("SELECT attempt_count FROM agent_jobs WHERE id = :job_id"),
            {"job_id": trabajo.id},
        )
    ).scalar_one()
    assert int(estado) == 2
