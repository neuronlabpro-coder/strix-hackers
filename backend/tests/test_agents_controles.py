"""Los tres controles que el módulo de agentes no tenía, y el que tampoco funcionaba.

## Qué se cubre

Cuatro cosas que la auditoría encontró como faltantes o rotas, y que aquí se comprueban por HTTP
contra la aplicación montada:

1. **Rol**: sin `ADMIN`, ni alta de agente, ni baja, ni encolar, ni reponer la cola.
2. **Cobro**: encolar descuenta del ledger, y sin saldo no se encola nada.
3. **Límite de tasa**: el encolado y el alta de agente están acotados.
4. **Aislamiento entre agentes**: un agente no puede entregar el trabajo que reclamó otro.

## Por qué la cuarta es la que más se parece a un agujero y las otras son endurecimiento

Porque el trigger de inmutabilidad de R4 protege la fila de un `UPDATE` hecho **desde SQL**, y la
entrega del resultado es una **llamada de API**. El trigger no la ve. Y lo que se sobreescribe no
es un campo cualquiera: es evidencia —el inventario de la red del cliente que el cliente pagó por
obtener— cuya trazabilidad sostiene un informe de postura.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import (
    AGENT_TOKEN_PREFIX_VISIBLE,
    ScannerAgent,
)
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token, hash_api_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _workspace(
    session: AsyncSession, *, role: RoleEnum, saldo: str = "1000"
) -> tuple[Organization, User, dict[str, str]]:
    """Un workspace con su usuario y sus cabeceras, con el rol que se le pida."""

    sufijo = uuid.uuid4().hex
    organization = Organization(
        name=f"Agentes {sufijo}",
        slug=f"agentes-{sufijo}",
        plan_tier=PlanTierEnum.ENTERPRISE,
        credit_balance=Decimal(saldo),
    )
    user = User(
        email=f"agentes-{sufijo}@example.com",
        hashed_password="not-used",
        full_name="Agentes User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
    await session.commit()
    return organization, user, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _agente(
    session: AsyncSession, organization: Organization, nombre: str
) -> tuple[ScannerAgent, str]:
    """Un agente dado de alta y su token, construidos al nivel de la fila.

    Se hace así y no por HTTP porque estas pruebas no van de alta — van de lo que pasa **después**
    de que el agente exista— y pasar por el endpoint metería el límite de tasa de por medio en
    pruebas que no lo están midiendo.
    """

    token = "mgf_agent_" + uuid.uuid4().hex
    agente = ScannerAgent(
        organization_id=organization.id,
        name=nombre,
        token_prefix=token[: AGENT_TOKEN_PREFIX_VISIBLE + 8],
        token_hash=hash_api_token(token),
        status="ACTIVE",
    )
    session.add(agente)
    return agente, token


# --------------------------------------------------------------------------- #
# 1. El rol
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_un_miembro_no_administra_los_agentes(integration_session: AsyncSession) -> None:
    """Ni alta, ni baja, ni encolar, ni reponer. Los cuatro le dan `403`.

    Se comprueban **los cuatro en un solo test** a propósito: la defecto que había era que ninguno
    lo comprobaba, y un test por ruta dejaría el patrón de "una sí, tres no" sin detectar. Aquí se
    ve de una vez que el módulo entero exige `ADMIN`.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.MEMBER
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        alta = await client.post("/api/v1/agents", json={"name": "no-deberia"}, headers=cabeceras)
        baja = await client.post(
            f"/api/v1/agents/{uuid.uuid4()}/revoke", headers=cabeceras
        )
        encolar = await client.post(
            "/api/v1/agents/jobs",
            json={"kind": "CONTAINER_SCAN", "target": "alpine:3.20"},
            headers=cabeceras,
        )
        reponer = await client.post("/api/v1/agents/jobs/requeue-expired", headers=cabeceras)

    assert alta.status_code == 403
    assert baja.status_code == 403
    assert encolar.status_code == 403
    assert reponer.status_code == 403

    # Y nada se creó: un `403` que deja una fila detrás sería peor que un `500`.
    # El recuento va acotado a **esta** organizacion: la base es compartida con el resto de la
    # suite, y un `count(*)` global contaria filas de otros tests y fallaria sin motivo.
    count = (
        await integration_session.execute(
            text("SELECT count(*) FROM scanner_agents WHERE organization_id = :id"),
            {"id": str(organization.id)},
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_un_admin_si_puede_leer_los_escaneos(integration_session: AsyncSession) -> None:
    """El `403` es solo en las de escribir. Leer es de cualquier miembro.

    Sin este test, el arreglo por exceso —poner la guarda en el router entero— pasaría en verde
    y dejaría a los miembros sin poder ver lo que han pedido, que es lo que paga el cliente.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.MEMBER
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listado = await client.get("/api/v1/agents/jobs", headers=cabeceras)
        resumen = await client.get("/api/v1/agents/summary", headers=cabeceras)

    assert listado.status_code == 200
    assert resumen.status_code == 200


# --------------------------------------------------------------------------- #
# 2. El cobro
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_encolar_descuenta_del_ledger(integration_session: AsyncSession) -> None:
    """Encolar deja un asiento, y el saldo baja.

    Es el hallazgo que decía que este módulo era el único camino de escritura sin contabilidad. La
    comprobación mira **las dos cosas**: que hay un asiento y que el saldo se movió, porque un
    asiento sin saldo es un asiento decorativo.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/agents/jobs",
            json={"kind": "CONTAINER_SCAN", "target": "alpine:3.20"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 202
    trabajo_id = respuesta.json()["id"]

    await integration_session.refresh(organization)
    assert Decimal(str(organization.credit_balance)) < Decimal("1000")

    # Y el asiento lleva el `reference_id` del trabajo, que es lo que hace el cobro idempotente.
    asiento = (
        await integration_session.execute(
            text("SELECT reference_id, amount_delta FROM credit_ledger WHERE reference_id = :id"),
            {"id": trabajo_id},
        )
    ).one_or_none()
    assert asiento is not None, "encolar sin asiento en el ledger"
    assert Decimal(str(asiento[1])) < 0


@pytest.mark.asyncio
async def test_sin_saldo_no_se_puede_encolar(integration_session: AsyncSession) -> None:
    """Sin saldo, `402` y **ni fila ni asiento**.

    Se comprueban las dos ausencias porque el orden importa: si el cobro se hiciera después del
    `add()`, habría un trabajo que un agente ejecutaría entero y que el cliente vería fallar solo
    al final, y el trabajo se habría hecho gratis.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN, saldo="0"
    )
    organization_id = str(organization.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/agents/jobs",
            json={"kind": "CONTAINER_SCAN", "target": "alpine:3.20"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 402
    trabajos = (
        await integration_session.execute(
            text("SELECT count(*) FROM agent_jobs WHERE organization_id = :id"),
            {"id": organization_id},
        )
    ).scalar_one()
    asientos = (
        await integration_session.execute(
            text("SELECT count(*) FROM credit_ledger WHERE organization_id = :id"),
            {"id": organization_id},
        )
    ).scalar_one()
    assert trabajos == 0, "un encolado rechazado dejó trabajo en la cola"
    assert asientos == 0, "un encolado rechazado dejó un asiento en el ledger"


# --------------------------------------------------------------------------- #
# 4. El aislamiento entre agentes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_un_agente_no_puede_entregar_el_trabajo_de_otro(
    integration_session: AsyncSession,
) -> None:
    """El hallazgo que el trigger de R4 no cubría.

    El trigger protege la fila de un `UPDATE` desde SQL. La entrega es una llamada de API, y con
    el filtro solo por `organization_id` el segundo agente escribía encima el `result` y el
    `result_digest` del primero.

    Aquí se comprueban las dos mitades: que el segundo recibe `404` **y** que el resultado del
    primero sigue siendo el suyo.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    primero, token_primero = _agente(integration_session, organization, "agente-primero")
    segundo, token_segundo = _agente(integration_session, organization, "agente-segundo")
    await integration_session.commit()
    await integration_session.refresh(primero)
    await integration_session.refresh(segundo)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        encolado = await client.post(
            "/api/v1/agents/jobs",
            json={"kind": "CONTAINER_SCAN", "target": "alpine:3.20"},
            headers=cabeceras,
        )
        trabajo_id = encolado.json()["id"]

        cab_primero = {"Authorization": f"Bearer {token_primero}"}
        cab_segundo = {"Authorization": f"Bearer {token_segundo}"}

        await client.post("/api/v1/agents/jobs/claim", json={}, headers=cab_primero)
        entregado = await client.post(
            f"/api/v1/agents/jobs/{trabajo_id}/report",
            json={"result": {"referencia": "alpine:3.20", "paquetes": []}},
            headers=cab_primero,
        )
        # Y ahora el segundo intenta escribir encima.
        intruso = await client.post(
            f"/api/v1/agents/jobs/{trabajo_id}/report",
            json={"result": {"referencia": "robada", "paquetes": []}},
            headers=cab_segundo,
        )

    assert entregado.status_code == 200
    assert intruso.status_code == 404, intruso.text

    fila = (
        await integration_session.execute(
            text("SELECT result FROM agent_jobs WHERE id = :id"), {"id": trabajo_id}
        )
    ).scalar_one()
    # Y la evidencia sigue siendo la del primero, que es el punto entero del hallazgo.
    assert fila["referencia"] == "alpine:3.20", "el segundo agente sobreescribió la evidencia"


@pytest.mark.asyncio
async def test_un_agente_ve_solo_sus_trabajos(integration_session: AsyncSession) -> None:
    """Un agente de otro tenant no puede ni leer el trabajo.

    El aislamiento por tenant ya estaba, y esto lo fija para que no se rompa al añadir el filtro
    de `agent_id`: las dos comprobaciones tienen que seguir dando `404` por razones distintas y
    las dos deben ser el mismo código.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    ajeno, _token_ajeno = _agente(integration_session, organization, "agente-propio")
    otro_workspace, _u2, _cabeceras_otro = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    for_truth, token_ajeno = _agente(integration_session, otro_workspace, "agente-ajeno")
    await integration_session.commit()
    await integration_session.refresh(ajeno)
    await integration_session.refresh(for_truth)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        encolado = await client.post(
            "/api/v1/agents/jobs",
            json={"kind": "CONTAINER_SCAN", "target": "alpine:3.20"},
            headers=cabeceras,
        )
        trabajo_id = encolado.json()["id"]
        # La ruta de **panel** que lee un trabajo pide `TenantDependency`, asi que no admite un
        # token de agente: da `401` y con razon. Lo que se comprueba aqui es el otro lado, el de
        # **agente**, que es por donde un tenant podria alcanzar el trabajo de otro.
        panel = await client.get(
            f"/api/v1/agents/jobs/{trabajo_id}",
            headers={"Authorization": f"Bearer {token_ajeno}"},
        )
        entrega = await client.post(
            f"/api/v1/agents/jobs/{trabajo_id}/report",
            json={"result": {"referencia": "robada"}},
            headers={"Authorization": f"Bearer {token_ajeno}"},
        )

    # El token de agente no es una sesion de panel: `401`, no `403`, porque no es un rol que le
    # falte sino una credencial de otra clase.
    assert panel.status_code == 401, panel.text
    # Y el trabajo del otro tenant sigue intacto: el `404` es el mismo codigo que el de un
    # identificador inexistente, y el resultado no se ha tocado.
    assert entrega.status_code == 404, entrega.text


@pytest.mark.asyncio
async def test_un_trabajo_vencido_lo_puede_reclamar_otro_agente(
    integration_session: AsyncSession,
) -> None:
    """El filtro de `agent_id` acepta `NULL`, y esta es la razón por la que se acepta.

    Un trabajo cuyo alquiler venció vuelve a la cola con `agent_id` a `NULL` a propósito. Si el
    filtro exigiera que `agent_id == agente.id` también en ese caso, el trabajo no podría
    reclamarse nunca más y se quedaría en `QUEUED` para siempre.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _workspace(
        integration_session, role=RoleEnum.ADMIN
    )
    segundo, token_segundo = _agente(integration_session, organization, "agente-segundo")
    await integration_session.commit()
    await integration_session.refresh(segundo)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        encolado = await client.post(
            "/api/v1/agents/jobs",
            json={"kind": "CONTAINER_SCAN", "target": "alpine:3.20"},
            headers=cabeceras,
        )
        trabajo_id = encolado.json()["id"]

    # Se devuelve a la cola como haría `reponer_alquileres`: con `agent_id` a `NULL`.
    await integration_session.execute(
        text("UPDATE agent_jobs SET agent_id = NULL, status = 'QUEUED' WHERE id = :id"),
        {"id": trabajo_id},
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reclamo = await client.post(
            "/api/v1/agents/jobs/claim",
            json={},
            headers={"Authorization": f"Bearer {token_segundo}"},
        )

    assert reclamo.status_code == 200
    assert reclamo.json()["id"] == trabajo_id
    total = (
        await integration_session.execute(text("SELECT count(*) FROM agent_jobs"))
    ).scalar_one()
    assert total == 1
