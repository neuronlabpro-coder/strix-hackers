"""La evidencia de un escaneo de agente es inmutable, y el trabajo sobrevive al agente.

## Qué se prueba aquí y por qué en la base y no en el código

El resultado de un escaneo de red dice qué puertos había abiertos. Es lo que el cliente pagó
por obtener, y dentro de seis meses lo que vale es poder demostrar que el registro no se tocó
después. Si la protección viviera en el servicio, escribir SQL la esquivaría; por eso hay un
`trigger` de PostgreSQL, y estas pruebas lo atacan **por SQL**, que es la vía por la que
fallaría.

La clase de pruebas que importa es la negativa: cada una declara qué operación tiene que
**fallar**. Una prueba que solo confirmara que un UPDATE funciona pasaría igual con el trigger
sin instalar, y por eso la última de cada bloque comprueba también que lo legítimo sigue
funcionando. Sin esa comprobación, un trigger que rechazara todas las escrituras aprobaría
todas estas pruebas y dejaría el sistema inservible.

## Por qué la organización de un trabajo no se puede cambiar ni aunque se pueda reescribir

Está en la misma comprobación que el resultado, y por la misma razón: si se pudiera mover un
escaneo de un tenant a otro, el tenant que lo recibe tendría un informe de una red que no es
suya, y el que lo pagó se quedaría sin él.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import (
    AGENT_TOKEN_PREFIX,
    AgentJob,
    AgentJobKindEnum,
    AgentJobStatusEnum,
    AgentStatusEnum,
    ScannerAgent,
)
from backend.apps.organizations.models import Organization
from backend.core.security import generate_api_token, hash_api_token

pytestmark = pytest.mark.integration


def _organizacion(nombre: str) -> Organization:
    return Organization(name=nombre, slug=f"{nombre.lower()}-{uuid.uuid4().hex}")


def _agente(organization_id: uuid.UUID, nombre: str = "agente") -> ScannerAgent:
    crudo = generate_api_token(AGENT_TOKEN_PREFIX, 32)
    return ScannerAgent(
        organization_id=organization_id,
        name=nombre,
        token_hash=hash_api_token(crudo),
        token_prefix=crudo[:8],
        status=AgentStatusEnum.ACTIVE,
    )


async def _trabajo_terminado(
    sesion: AsyncSession, organization_id: uuid.UUID, agent_id: uuid.UUID | None
) -> AgentJob:
    trabajo = AgentJob(
        organization_id=organization_id,
        agent_id=agent_id,
        kind=AgentJobKindEnum.NETWORK_SCAN,
        target="10.10.0.0/24",
        status=AgentJobStatusEnum.COMPLETED,
        result={"hosts": [{"ip": "10.10.0.7", "ports": [22, 5432]}]},
        result_digest="a" * 64,
        completed_at=text("now()"),
    )
    sesion.add(trabajo)
    await sesion.flush()
    return trabajo


# --------------------------------------------------------------------------- #
# La credencial
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_agente_no_guarda_el_token_sino_su_hash(integration_session: AsyncSession) -> None:
    """La fila tiene que ser inútil para quien la lea, y no solo incómoda.

    Un `token` en claro sería un secreto en la base: un volcado, una copia de seguridad o una
    consulta de soporte bastarían para suplantar al agente, que corre con permisos sobre la
    red del cliente. Por eso se comprueba que el token en claro no aparece en la fila, y no
    solo que hay una columna de hash.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-credencial")
    integration_session.add(organizacion)
    await integration_session.flush()

    crudo = generate_api_token(AGENT_TOKEN_PREFIX, 32)
    agente = ScannerAgent(
        organization_id=organizacion.id,
        name="con-credencial",
        token_hash=hash_api_token(crudo),
        token_prefix=crudo[:8],
    )
    integration_session.add(agente)
    await integration_session.flush()

    fila = (
        await integration_session.execute(
            text(
                "SELECT token_hash, token_prefix FROM scanner_agents WHERE id = :agente_id"
            ),
            {"agente_id": agente.id},
        )
    ).one()

    assert fila.token_hash == hash_api_token(crudo)
    # El secreto no esta en ninguna de las dos columnas visibles.
    assert crudo not in fila.token_hash
    assert crudo.removeprefix(AGENT_TOKEN_PREFIX) not in fila.token_hash
    # Y el hash es determinista con el mismo secreto, que es lo que permite buscar por el.
    assert hash_api_token(crudo) == fila.token_hash


@pytest.mark.asyncio
async def test_dos_agentes_no_pueden_Compartir_nombre_en_la_misma_organizacion(
    integration_session: AsyncSession,
) -> None:
    """El duplicado es el mismo agente instalándose dos veces.

    La restricción tiene que estar en la tabla, no en la validación del esquema: el `INSERT`
    también puede venir de una integración, de un script de alta o de una migración. Y el
    error tiene que ser de integridad, no un `IntegrityError` sin contexto en un log.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-duplicado")
    integration_session.add(organizacion)
    await integration_session.flush()

    integration_session.add(_agente(organizacion.id, "mismo-nombre"))
    await integration_session.flush()

    integration_session.add(_agente(organizacion.id, "mismo-nombre"))
    with pytest.raises(IntegrityError):
        await integration_session.flush()


@pytest.mark.asyncio
async def test_otro_tenant_puede_registrar_un_agente_con_el_mismo_nombre(
    integration_session: AsyncSession,
) -> None:
    """La unicidad es **por organización**, no global.

    Dos clientes que han instalado su agente con el nombre `agente` no se estorban, y una
    restricción global los obligaría a inventarse nombres distintos para siempre, que es
    exactamente el tipo de colisión de espacio de nombres que se paga en soporte.
    """

    assert integration_session is not None
    primera = _organizacion("agente-tenant-a")
    segunda = _organizacion("agente-tenant-b")
    integration_session.add_all([primera, segunda])
    await integration_session.flush()

    integration_session.add(_agente(primera.id, "agente"))
    integration_session.add(_agente(segunda.id, "agente"))
    await integration_session.flush()

    total = (
        await integration_session.execute(
            text("SELECT count(*) FROM scanner_agents WHERE name = 'agente'")
        )
    ).scalar()
    assert int(total) == 2


# --------------------------------------------------------------------------- #
# La evidencia: R4
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_resultado_de_un_trabajo_terminado_no_se_puede_reescribir(
    integration_session: AsyncSession,
) -> None:
    """El `UPDATE` del resultado de un trabajo ya terminado tiene que fallar.

    Es el R4 aplicado al escaneo de red. El caso que evita no es un atacante: es la
    corrección posterior de un resultado, hecha por la propia plataforma con su propia API,
    cuando un cliente dice "en realidad no era así".
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-resultado")
    integration_session.add(organizacion)
    await integration_session.flush()
    trabajo = await _trabajo_terminado(integration_session, organizacion.id, None)

    with pytest.raises(DBAPIError, match="inmutable"):
        await integration_session.execute(
            text(
                "UPDATE agent_jobs SET result = CAST(:resultado AS jsonb) WHERE id = :trabajo_id"
            ),
            {
                # Serializado a mano y no pasado como `dict`: `asyncpg` no sabe codificar un
                # diccionario como parametro de texto, y el error que sale dice
                # "'dict' object has no attribute 'encode'", que no parece tener nada que ver con
                # una columna JSONB. El `CAST` de la sentencia hace el resto.
                "resultado": json.dumps(
                    {"hosts": [{"ip": "10.10.0.7", "ports": [22]}]}
                ),
                "trabajo_id": trabajo.id,
            },
        )


@pytest.mark.asyncio
async def test_la_huella_del_resultado_tampoco_se_puede_cambiar(
    integration_session: AsyncSession,
) -> None:
    """La huella es lo que verifica el resultado, así que se protege con él.

    Si la huella se pudiera reescribir sin tocar el `result`, la comprobación de integridad al
    dejaría de detectar nada: bastaría con recalcular la huella del resultado nuevo. Es el
    fallo clásico de poner el sello y el paquete en la misma caja y dejar solo la caja
    trancada.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-huella")
    integration_session.add(organizacion)
    await integration_session.flush()
    trabajo = await _trabajo_terminado(integration_session, organizacion.id, None)

    with pytest.raises(DBAPIError, match="inmutable"):
        await integration_session.execute(
            text("UPDATE agent_jobs SET result_digest = :huella WHERE id = :trabajo_id"),
            {"huella": "b" * 64, "trabajo_id": trabajo.id},
        )


@pytest.mark.asyncio
async def test_un_trabajo_terminado_no_se_puede_mover_de_tenant(
    integration_session: AsyncSession,
) -> None:
    """Mover la organización es cambiar de dueño la evidencia.

    Con la fila movida, el tenant que la recibe tiene un informe de una red que no es suya y
    el que pagó por el escaneo se queda sin él. Va en la misma comprobación que el resultado
    porque es el mismo R4 aplicado a otro campo.
    """

    assert integration_session is not None
    origen = _organizacion("agente-origen")
    destino = _organizacion("agente-destino")
    integration_session.add_all([origen, destino])
    await integration_session.flush()
    trabajo = await _trabajo_terminado(integration_session, origen.id, None)

    with pytest.raises(DBAPIError, match="inmutable"):
        await integration_session.execute(
            text("UPDATE agent_jobs SET organization_id = :destino WHERE id = :trabajo_id"),
            {"destino": destino.id, "trabajo_id": trabajo.id},
        )


@pytest.mark.asyncio
async def test_un_trabajo_terminado_no_se_puede_borrar(
    integration_session: AsyncSession,
) -> None:
    """`DELETE` se rechaza siempre, y no solo sobre lo terminado.

    El borrado de evidencia no tiene ningún caso legítimo en este producto: no hay un
    derecho de supresión que lo justifique, porque la fila no contiene datos personales del
    cliente, sino lo que se encontró en una red que el cliente pidió escanear.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-borrar")
    integration_session.add(organizacion)
    await integration_session.flush()
    trabajo = await _trabajo_terminado(integration_session, organizacion.id, None)

    with pytest.raises(DBAPIError, match="no pueden eliminarse"):
        await integration_session.execute(
            text("DELETE FROM agent_jobs WHERE id = :trabajo_id"),
            {"trabajo_id": trabajo.id},
        )


@pytest.mark.asyncio
async def test_la_tabla_no_se_puede_truncar(integration_session: AsyncSession) -> None:
    """`TRUNCATE` necesita su propio trigger, y es un olvido fácil.

    `TRUNCATE` no tiene filas, así que un trigger `FOR EACH ROW` no se dispara nunca para él.
    Un único trigger de fila parecería proteger la tabla y la dejaría entera: el borrado masivo
    pasaría limpio. Por eso la migración instala un segundo trigger con `FOR EACH STATEMENT`, y
    esta prueba es la que dice que existe.
    """

    assert integration_session is not None
    with pytest.raises(DBAPIError, match="truncarse"):
        await integration_session.execute(text("TRUNCATE agent_jobs"))


@pytest.mark.asyncio
async def test_un_trabajo_en_cola_sí_se_puede_gestionar(
    integration_session: AsyncSession,
) -> None:
    """Lo legítimo tiene que seguir funcionando, o el trigger es un incendio.

    La reserva, el intento y el paso a terminal son escrituras normales del ciclo de vida, y las
    hace el propio servicio. Si el trigger las rechazara, estas pruebas negativas pasarían
    todas y el sistema no escanearía nada. Por eso se comprueba explícitamente.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-ciclo")
    integration_session.add(organizacion)
    await integration_session.flush()

    trabajo = AgentJob(
        organization_id=organizacion.id,
        kind=AgentJobKindEnum.CONTAINER_SCAN,
        target="alpine:3.20",
        status=AgentJobStatusEnum.QUEUED,
    )
    integration_session.add(trabajo)
    await integration_session.flush()

    await integration_session.execute(
        text(
            "UPDATE agent_jobs SET status = 'CLAIMED', attempt_count = 1, "
            "claimed_at = now(), lease_expires_at = now() + interval '5 minutes' "
            "WHERE id = :trabajo_id"
        ),
        {"trabajo_id": trabajo.id},
    )
    await integration_session.flush()

    await integration_session.execute(
        text(
            "UPDATE agent_jobs SET status = 'COMPLETED', result = CAST(:resultado AS jsonb), "
            "result_digest = :huella, completed_at = now() WHERE id = :trabajo_id"
        ),
        {"resultado": json.dumps({"paquetes": 88}), "huella": "c" * 64, "trabajo_id": trabajo.id},
    )
    await integration_session.flush()
    await integration_session.commit()

    estado = (
        await integration_session.execute(
            text("SELECT status, attempt_count FROM agent_jobs WHERE id = :trabajo_id"),
            {"trabajo_id": trabajo.id},
        )
    ).one()
    assert estado.status == AgentJobStatusEnum.COMPLETED
    assert estado.attempt_count == 1


# --------------------------------------------------------------------------- #
# La evidencia sobrevive al agente
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_revocar_un_agente_no_borra_los_trabajos_que_ejecuto(
    integration_session: AsyncSession,
) -> None:
    """`ON DELETE SET NULL`, y la razón es que la evidencia no es del agente.

    Si revocar un agente —porque se perdió el portátil donde estaba instalado, o porque se
    sospecha que está comprometido— borrara los escaneos que hizo, la postura de un cliente
    desaparecería cada vez que su técnico se fuera de la empresa. Con `SET NULL`, el trabajo
    se queda sin autor atribuido y con su resultado intacto, que es justo lo que hace falta
    para auditarlo.
    """

    assert integration_session is not None
    organizacion = _organizacion("agente-revocacion")
    integration_session.add(organizacion)
    await integration_session.flush()

    agente = _agente(organizacion.id, "agente-revocable")
    integration_session.add(agente)
    await integration_session.flush()

    trabajo = await _trabajo_terminado(integration_session, organizacion.id, agente.id)
    trabajo_id = trabajo.id

    # Se revoca el agente, que es la operación de negocio, y luego se borra la fila del agente
    # como haria una limpieza de la tabla. El trabajo tiene que sobrevivir a las dos.
    await integration_session.execute(
        text("UPDATE scanner_agents SET status = 'REVOKED' WHERE id = :agente_id"),
        {"agente_id": agente.id},
    )
    await integration_session.execute(
        text("DELETE FROM scanner_agents WHERE id = :agente_id"),
        {"agente_id": agente.id},
    )
    await integration_session.flush()

    superviviente = (
        await integration_session.execute(
            text("SELECT agent_id, result, result_digest FROM agent_jobs WHERE id = :trabajo_id"),
            {"trabajo_id": trabajo_id},
        )
    ).one()
    assert superviviente.agent_id is None
    assert superviviente.result is not None
    assert superviviente.result_digest == "a" * 64
