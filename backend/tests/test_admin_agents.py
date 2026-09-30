"""La consola de plataforma ve los agentes de todos los clientes, y solo puede cortarlos.

## Qué se cubre

Dos cosas que la consola de agentes necesita y que no se ven en la pantalla:

- La **visibilidad transversal**: el operador ve los agentes de todos los tenants, con el nombre
  del cliente resuelto, y la vista no se acota si le mandan una cabecera de organización.
- La **frontera de la revocación**: el operador puede cortar el acceso de un cliente, pero solo
  con un motivo escrito, y la baja es de una sola vez.

## Por qué estas pruebas no son de la vista

Porque la vista no decide nada de esto. Lo que decide es si la ruta cruza tenants, si el motivo
es opcional y si una revocación repetida cambia algo. Eso se comprueba por HTTP contra la
aplicación montada, que es donde se puede comprobar de verdad.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import AgentStatusEnum, ScannerAgent
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(
    session: AsyncSession,
    *,
    is_superuser: bool,
    name: str = "Consola",
) -> tuple[Organization, User, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{name} {suffix}",
        slug=f"consola-{suffix}",
        plan_tier=PlanTierEnum.PRO,
        credit_balance=0,
    )
    user = User(
        email=f"consola-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Console User",
        email_verified=True,
        is_superuser=is_superuser,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    return organization, user, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _agente(
    organizacion: Organization, nombre: str, *, visto_hace: float | None
) -> ScannerAgent:
    """Un agente dado de alta, conectado hace `visto_hace` segundos o nunca.

    `visto_hace=None` es «nunca ha conectado», que es un estado distinto de «se cayó hace una
    hora» y merece un caso propio en las pruebas.
    """

    return ScannerAgent(
        organization_id=organizacion.id,
        name=nombre,
        token_prefix="fx_abcd",
        # El hash es unico en la base, y por eso lleva el nombre dentro: dos agentes de prueba
        # con el mismo hash chocarían contra `uq_scanner_agents_token_hash` y el fallo seria una
        # violacion de restriccion, que dice mucho menos que lo que dice el nombre del agente que
        # se estaba dando de alta.
        token_hash=f"hash-de-prueba::{nombre}",
        status=AgentStatusEnum.ACTIVE,
        platform_hint="docker/amd64",
        agent_version="0.1.0",
        last_seen_at=(
            None if visto_hace is None else datetime.now(UTC) - timedelta(seconds=visto_hace)
        ),
    )


@pytest.mark.asyncio
async def test_la_consola_ve_los_agentes_de_todos_los_clientes(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    primero, _u1, cabeceras = await _tenant(integration_session, is_superuser=True, name="Uno")
    segundo, _u2, _cab2 = await _tenant(integration_session, is_superuser=True, name="Dos")
    agente_a = _agente(primero, "agente-de-uno", visto_hace=30)
    agente_b = _agente(segundo, "agente-de-dos", visto_hace=None)
    integration_session.add_all([agente_a, agente_b])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/admin/agents", headers=cabeceras)

    assert response.status_code == 200
    payload = response.json()
    por_nombre = {item["name"]: item for item in payload["items"]}
    assert {"agente-de-uno", "agente-de-dos"} <= set(por_nombre)

    # El nombre del cliente viene resuelto. Sin él, el operador ve UUIDs y tiene que abrir cada
    # fila para averiguar de quién es el agente, que es la mitad del trabajo de esta vista.
    assert por_nombre["agente-de-uno"]["organization_name"] == primero.name
    assert por_nombre["agente-de-dos"]["organization_id"] == str(segundo.id)

    # Y el estado de conexión lo decide el servidor con una sola ventana, para que la insignia
    # de la consola y el KPI del panel no se contradigan.
    assert por_nombre["agente-de-uno"]["connected"] is True
    assert por_nombre["agente-de-dos"]["connected"] is False
    assert payload["ventana_de_vida"] == 300


@pytest.mark.asyncio
async def test_un_agente_viejo_deja_de_contar_como_conectado(
    integration_session: AsyncSession,
) -> None:
    """El mismo agente, con la última visita dentro y fuera de la ventana.

    Es el caso que hace que la vista sea útil: un agente que se conectó hace seis minutos está
    igual de caído que uno que nunca se conectó, y la insignia tiene que decirlo.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session, is_superuser=True)
    integration_session.add(_agente(organizacion, "agente-viejo", visto_hace=3600))
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/admin/agents", headers=cabeceras)

    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["name"] == "agente-viejo")
    assert item["connected"] is False
    assert item["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_la_consola_no_se_acota_a_un_tenant(integration_session: AsyncSession) -> None:
    """La cabecera `X-Organization-Id` no reduce el resultado.

    Es lo contrario de lo que se comprueba en el panel, y a propósito: aquí una vista acotada
    sería una vista inútil, porque justo lo que se busca es el agente de un cliente distinto al
    que tiene la sesión abierta. Lo que se vigila es que **no** se filtre, porque si algún día
    alguien añade el filtro creyendo que es una Mejora de aislamiento, esta prueba lo dice.
    """

    assert integration_session is not None
    primero, _u1, cabeceras = await _tenant(integration_session, is_superuser=True, name="Uno")
    segundo, _u2, _cab2 = await _tenant(integration_session, is_superuser=True, name="Dos")
    integration_session.add_all(
        [
            _agente(primero, "solo-en-el-otro", visto_hace=10),
            _agente(segundo, "tambien-en-el-otro", visto_hace=10),
        ]
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        con_cabecera = await client.get("/api/v1/admin/agents", headers=cabeceras)
        cabecera_ajena = dict(cabeceras)
        cabecera_ajena["X-Organization-Id"] = str(primero.id)
        con_otra = await client.get("/api/v1/admin/agents", headers=cabecera_ajena)

    assert con_cabecera.status_code == 200
    assert con_otra.status_code == 200
    nombres = {i["name"] for i in con_cabecera.json()["items"]}
    assert {"solo-en-el-otro", "tambien-en-el-otro"} <= nombres
    assert con_otra.json()["total"] == con_cabecera.json()["total"]


@pytest.mark.asyncio
async def test_la_consola_no_muestra_el_token(integration_session: AsyncSession) -> None:
    """Ni siquiera el prefijo se devuelve entero, y el secreto no está en ninguna forma."""

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session, is_superuser=True)
    integration_session.add(_agente(organizacion, "agente-secreto", visto_hace=5))
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/admin/agents", headers=cabeceras)

    item = next(i for i in response.json()["items"] if i["name"] == "agente-secreto")
    assert "token" not in item
    assert "token_hash" not in item
    assert item["token_prefix"] == "fx_abcd"
    assert "hash-de-prueba" not in response.text


@pytest.mark.asyncio
async def test_revocar_exige_motito_escrito(integration_session: AsyncSession) -> None:
    """Sin motivo, `422`. Un corte sobre la red de otro cliente sin explicación es
    indistinguible de una intervención sin justificar, y el registro de auditoría es la única
    forma de distinguirlo dentro de seis meses."""

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session, is_superuser=True)
    agente = _agente(organizacion, "agente-a-revocar", visto_hace=1)
    integration_session.add(agente)
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sin_cuerpo = await client.post(
            f"/api/v1/admin/agents/{agente.id}/revoke", headers=cabeceras
        )
        corto = await client.post(
            f"/api/v1/admin/agents/{agente.id}/revoke", json={"reason": "ab"}, headers=cabeceras
        )
        completo = await client.post(
            f"/api/v1/admin/agents/{agente.id}/revoke",
            json={"reason": "el cliente ha pedido el corte"},
            headers=cabeceras,
        )

    assert sin_cuerpo.status_code == 422
    assert corto.status_code == 422
    assert completo.status_code == 200

    await integration_session.refresh(agente)
    assert agente.status is AgentStatusEnum.REVOKED
    assert agente.revoked_reason == "el cliente ha pedido el corte"


@pytest.mark.asyncio
async def test_revocar_dos_veces_no_vuelve_a_bajar_el_agente(
    integration_session: AsyncSession,
) -> None:
    """La segunda vez da `409` en vez de reescribir `revoked_at`.

    Sin el `409`, una doble pulsación reescribiría la fecha de baja con la hora de la segunda y
    el registro de auditoría daría una hora equivocada de cuando se cortó el acceso.
    """

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session, is_superuser=True)
    agente = _agente(organizacion, "agente-doble", visto_hace=1)
    integration_session.add(agente)
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = await client.post(
            f"/api/v1/admin/agents/{agente.id}/revoke",
            json={"reason": "primera"},
            headers=cabeceras,
        )
        await integration_session.refresh(agente)
        primera_fecha = agente.revoked_at
        segunda = await client.post(
            f"/api/v1/admin/agents/{agente.id}/revoke",
            json={"reason": "segunda"},
            headers=cabeceras,
        )
    await integration_session.refresh(agente)

    assert primera.status_code == 200
    assert segunda.status_code == 409
    assert agente.revoked_at == primera_fecha
    assert agente.revoked_reason == "primera"


@pytest.mark.asyncio
async def test_revocar_un_agente_inexistente_da_404(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    _organizacion, _usuario, cabeceras = await _tenant(integration_session, is_superuser=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/admin/agents/00000000-0000-0000-0000-000000000000/revoke",
            json={"reason": "no existe"},
            headers=cabeceras,
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_un_usuario_normal_no_tiene_esta_vista(integration_session: AsyncSession) -> None:
    """Sin `is_superuser`, ni leer ni cortar. Las dos cosas, porque poder ver los agentes de
    todos los clientes ya es información sensible; poderlos cortar sería el daño completo."""

    assert integration_session is not None
    organizacion, _usuario, cabeceras = await _tenant(integration_session, is_superuser=False)
    agente = _agente(organizacion, "agente-intacto", visto_hace=1)
    integration_session.add(agente)
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listado = await client.get("/api/v1/admin/agents", headers=cabeceras)
        corte = await client.post(
            f"/api/v1/admin/agents/{agente.id}/revoke",
            json={"reason": "no deberia llegar aqui"},
            headers=cabeceras,
        )

    assert listado.status_code == 403
    assert corte.status_code == 403
    await integration_session.refresh(agente)
    assert agente.status is AgentStatusEnum.ACTIVE


@pytest.mark.asyncio
async def test_sin_sesion_no_hay_consola(integration_session: AsyncSession) -> None:
    assert integration_session is not None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listado = await client.get("/api/v1/admin/agents")

    assert listado.status_code == 401
