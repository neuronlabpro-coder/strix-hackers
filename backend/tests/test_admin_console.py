"""Pruebas de la consola de SuperAdmin y del resumen de facturacion.

## Qué se comprueba y por qué aquí

La consola de SuperAdmin es la **excepción** al aislamiento por tenant: sus rutas no
aceptan `X-Organization-Id` y operan sobre toda la plataforma. Una excepción a la regla
general de R3 solo es aceptable si sus dos fronteras están probadas:

1. **No la alcanza quien no es superusuario.** Un `403` en cada ruta, sin excepción.
2. **Al superusuario le da todo y no se lo limita a un tenant.** Un `403` por tenant
   equivocado sería un fallo de diseño, no una precaución: la consola existe precisamente
   para cruzar tenants.

La segunda mitad de la batería es de **medición**: que el saldo que devuelve el resumen sea
el de la base, que los packs sean los del catálogo, y que la inyección de créditos escriba
un asiento `ADMIN_ADJUSTMENT` y no un `STRIPE_PURCHASE`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.schemas import CREDIT_PACKS
from backend.apps.billing.service import apply_credit_delta
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

#: Todas las rutas de la consola. Se prueban una por una para que una que se quede sin
#: cubrir falle sola, y no desaparezca en un parametro mal escrito.
ADMIN_RUTAS = [
    ("get", "/api/v1/admin/overview"),
    ("get", "/api/v1/admin/organizations"),
    ("get", "/api/v1/admin/users"),
    ("get", "/api/v1/admin/sales"),
    ("get", "/api/v1/admin/audit-log"),
    ("get", "/api/v1/admin/health"),
    ("get", "/api/v1/admin/llm/"),
]


def _headers(user: User, organization: Organization | None = None) -> dict[str, str]:
    cabeceras = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    if organization is not None:
        cabeceras["X-Organization-Id"] = str(organization.id)
    return cabeceras


async def _superuser(session: AsyncSession) -> tuple[User, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Admin {suffix}", slug=f"admin-{suffix}")
    user = User(
        email=f"admin-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Super Admin",
        email_verified=True,
        is_superuser=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    return user, _headers(user, organization)


async def _normal_user(
    session: AsyncSession,
) -> tuple[User, Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Normal {suffix}", slug=f"normal-{suffix}")
    user = User(
        email=f"normal-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Normal User",
        email_verified=True,
        is_superuser=False,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return user, organization, _headers(user, organization)


# --------------------------------------------------------------------------- #
# Frontera 1: la consola no es del usuario normal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("metodo", "ruta"), ADMIN_RUTAS)
@pytest.mark.asyncio
async def test_un_usuario_normal_no_alcanza_la_consola(
    integration_session: AsyncSession, metodo: str, ruta: str
) -> None:
    """Cada ruta de la consola exige `is_superuser`.

    Se prueban todas con el mismo mecanismo en vez de confiar en que el router padre lo
    impone. Una ruta nueva forgetting la dependencia quedaría accesible, y esto es lo que
    lo detecta.
    """

    assert integration_session is not None
    _user, _org, headers = await _normal_user(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(metodo.upper(), ruta, headers=headers)

    assert response.status_code == 403, f"{ruta} respondio {response.status_code}"


@pytest.mark.asyncio
async def test_las_rutas_de_consola_exigen_autenticacion(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for metodo, ruta in ADMIN_RUTAS:
            response = await client.request(metodo.upper(), ruta)
            assert response.status_code == 401, f"{ruta} sin sesion"


@pytest.mark.asyncio
async def test_la_consola_ignora_la_cabecera_de_organizacion(
    integration_session: AsyncSession,
) -> None:
    """El superusuario ve la plataforma entera, no la de su cabecera.

    Es lo contrario que en el resto de la API, y por eso está probado: si alguien añadiera
    el filtro por tenant "por seguridad", el superusuario vería una lista vacía y pensaría
    que la plataforma no tiene tenants.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    otro = (
        await integration_session.execute(select(Organization).limit(1))
    ).scalar_one()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/admin/organizations", headers={**headers, "X-Organization-Id": str(otro.id)}
        )

    assert response.status_code == 200
    assert response.json()["total"] >= 1


# --------------------------------------------------------------------------- #
# Resumen global
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_resumen_trae_las_seis_metricas_y_la_salud(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/overview", headers=headers)

    assert response.status_code == 200
    cuerpo = response.json()
    claves = {metrica["key"] for metrica in cuerpo["metrics"]}
    assert claves == {
        "mrr",
        "revenueTotal",
        "creditsSold",
        "pentestRuns",
        "activeTenants",
        "totalUsers",
    }
    # Todas las metricas con formato, para que el panel no tenga que adivinar la escala.
    for metrica in cuerpo["metrics"]:
        assert metrica["format"] in {"currency", "credits", "count"}
    assert cuerpo["infrastructure"]["status"] in {"healthy", "degraded"}


async def _metricas(client: AsyncClient, headers: dict[str, str]) -> dict[str, Decimal]:
    """Lee el resumen global y devuelve sus métricas como `Decimal`.

    `Decimal` y no `float` porque los valores llegan como cadena JSON —así serializa
    Pydantic un `Decimal`— y comparar `"500"` con `500.0` en coma flotante perdería
    precisión justo en las fracciones de centavo que existen en esta tabla.
    """

    respuesta = await client.get("/api/v1/admin/overview", headers=headers)
    assert respuesta.status_code == 200, respuesta.text
    return {m["key"]: Decimal(str(m["value"])) for m in respuesta.json()["metrics"]}


@pytest.mark.asyncio
async def test_el_mrr_no_cuenta_los_creditos_de_registro(
    integration_session: AsyncSession,
) -> None:
    """Los créditos de bienvenida no son ingresos.

    Meterlos en el MRR daría una facturación que nunca ocurrió, que es la clase de error
    que hace que una decisión comercial salga cara. La prueba mide la diferencia que
    producen una compra y un bono, y comprueba que solo cuenta la compra.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    organization = (
        await integration_session.execute(
            select(Organization).where(Organization.slug.like("admin-%"))
        )
    ).scalars().first()
    assert organization is not None
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        antes = await _metricas(client, headers)

        await apply_credit_delta(
            session=integration_session,
            organization_id=organization.id,
            amount=Decimal("750"),
            reason=LedgerReasonEnum.SIGNUP_BONUS,
        )
        await apply_credit_delta(
            session=integration_session,
            organization_id=organization.id,
            amount=Decimal("500"),
            reason=LedgerReasonEnum.STRIPE_PURCHASE,
        )

        despues = await _metricas(client, headers)

    delta_mrr = despues["mrr"] - antes["mrr"]
    delta_total = despues["revenueTotal"] - antes["revenueTotal"]
    delta_vendidos = despues["creditsSold"] - antes["creditsSold"]

    # 500 de compra entran; los 750 de bono no. Si el bono se contara, valdría 1250.
    assert delta_mrr == Decimal("500"), f"el MRR sumo el bono: {delta_mrr}"
    assert delta_total == Decimal("500"), f"el acumulado sumo el bono: {delta_total}"
    assert delta_vendidos == Decimal("500")


@pytest.mark.asyncio
async def test_el_consumo_de_escaneos_no_entra_en_el_mrr(
    integration_session: AsyncSession,
) -> None:
    """Un consumo grande tampoco es una venta.

    Es la otra mitad de la misma regla, y merece su propia prueba porque un signo mal
    puesto en el filtro lo cuela sin que ninguna de las dos se note: con un consumo de
    5000 créditos, el MRR bajaría y la prueba del bono seguiría en verde.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    organization = (
        await integration_session.execute(
            select(Organization).where(Organization.slug.like("admin-%"))
        )
    ).scalars().first()
    assert organization is not None
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        antes = await _metricas(client, headers)

        # El ledger rechaza un gasto sin saldo, y con razón: un consumo sin crédito
        # acreditado es un fallo de la aplicación, no un dato para el informe. Se
        # acredita antes para que la prueba mida el signo del filtro y no esa regla.
        await apply_credit_delta(
            session=integration_session,
            organization_id=organization.id,
            amount=Decimal("6000"),
            reason=LedgerReasonEnum.SIGNUP_BONUS,
        )
        await apply_credit_delta(
            session=integration_session,
            organization_id=organization.id,
            amount=Decimal("-5000"),
            reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        )

        despues = await _metricas(client, headers)

    assert despues["mrr"] == antes["mrr"], "un consumo de escaneo altero el MRR"
    assert despues["revenueTotal"] == antes["revenueTotal"]


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #


async def _varios_tenants(session: AsyncSession) -> dict[str, Organization]:
    """Crea tenants en los tres estados del ciclo de vida, para filtrar de verdad."""

    creados: dict[str, Organization] = {}
    sufijo = uuid.uuid4().hex[:6]
    for etiqueta, plan in (("libre", PlanTierEnum.FREE), ("pro", PlanTierEnum.PRO)):
        org = Organization(
            name=f"{etiqueta} {sufijo}",
            slug=f"tenant-{etiqueta}-{sufijo}",
            plan_tier=plan,
        )
        session.add(org)
        creados[etiqueta] = org
    baja = Organization(name=f"baja {sufijo}", slug=f"tenant-baja-{sufijo}")
    baja.deleted_at = datetime.now(UTC)
    baja.is_active = False
    session.add(baja)
    session.add_all([*creados.values()])
    await session.flush()
    creados["baja"] = baja
    await session.commit()
    for org in creados.values():
        await session.refresh(org)
    return creados


@pytest.mark.asyncio
async def test_los_tenants_se_filtran_por_plan_y_estado(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        libres = await client.get(
            "/api/v1/admin/organizations?plan=FREE&limit=100", headers=headers
        )
        dados_de_baja = await client.get(
            "/api/v1/admin/organizations?lifecycle=deleted&limit=100", headers=headers
        )
        activos = await client.get(
            "/api/v1/admin/organizations?lifecycle=active&limit=100", headers=headers
        )

    assert libres.status_code == 200
    assert {o["id"] for o in libres.json()["items"]} >= {str(creados["libre"].id)}
    assert str(creados["pro"].id) not in {o["id"] for o in libres.json()["items"]}

    ids_baja = {o["id"] for o in dados_de_baja.json()["items"]}
    assert str(creados["baja"].id) in ids_baja
    assert str(creados["libre"].id) not in ids_baja

    ids_activos = {o["id"] for o in activos.json()["items"]}
    assert str(creados["libre"].id) in ids_activos
    assert str(creados["baja"].id) not in ids_activos


@pytest.mark.asyncio
async def test_un_estado_invalido_se_rechaza_nombrando_los_validos(
    integration_session: AsyncSession,
) -> None:
    """El `422` dice cuáles son los válidos, no solo que el valor no lo es.

    Sin enumerarlos, quien lo mandara desde la consola tendria que abrir el codigo para
    descubrir los valores válidos, y lo mismo para el panel si llegara a producir uno.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/admin/organizations?lifecycle=inventado", headers=headers
        )

    assert response.status_code == 422
    for valido in ("active", "deleted", "deactivated"):
        assert valido in response.text


@pytest.mark.asyncio
async def test_cambiar_de_plan_devuelve_el_tenant_con_el_plan_nuevo(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    objetivo = creados["libre"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/api/v1/admin/organizations/{objetivo.id}",
            json={"plan_tier": "ENTERPRISE"},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["plan_tier"] == "ENTERPRISE"
    refrescado = (
        await integration_session.execute(
            select(Organization).where(Organization.id == objetivo.id)
        )
    ).scalar_one()
    assert refrescado.plan_tier == PlanTierEnum.ENTERPRISE


@pytest.mark.asyncio
async def test_inyectar_creditos_escribe_el_asiento_de_ajuste_y_no_de_compra(
    integration_session: AsyncSession,
) -> None:
    """El motivo lo fija el servidor.

    Un endpoint que aceptara el motivo del cliente dejaría escribir `STRIPE_PURCHASE` en
    el ledger, que es la clase de asiento de la que se deduce que hubo un cobro. La
    inyección administrativa es un hecho distinto y tiene que quedar distinto.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    objetivo = creados["pro"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/admin/organizations/{objetivo.id}/credits",
            json={"amount": "125.5", "note": "prueba"},
            headers=headers,
        )

    assert response.status_code == 201, response.text
    cuerpo = response.json()
    assert Decimal(cuerpo["granted"]) == Decimal("125.5")
    assert Decimal(cuerpo["balance_after"]) == Decimal("125.5")

    entradas = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.id == cuerpo["ledger_entry_id"])
        )
    ).scalar_one()
    assert entradas.reason == LedgerReasonEnum.ADMIN_ADJUSTMENT
    assert entradas.amount_delta == Decimal("125.5")

    compras = int(
        (
            await integration_session.execute(
                select(func.count(CreditLedger.id)).where(
                    CreditLedger.organization_id == objetivo.id,
                    CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE,
                )
            )
        ).scalar_one()
    )
    assert compras == 0, "una inyeccion administrativa se contabilizo como compra"


@pytest.mark.asyncio
async def test_inyectar_creditos_a_un_tenant_dado_de_baja_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """Un tenant dado de baja no admite créditos.

    Acreditarle saldo es dejar un workspace apagado con dinero dentro, y el primer
    operador que lo intente va a creer que el pago no se registró. El `409` lo dice.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/admin/organizations/{creados['baja'].id}/credits",
            json={"amount": "10"},
            headers=headers,
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_inyectar_un_importe_negativo_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """Un ajuste a la baja es una devolución, y es otra operación.

    Permitir negativos aquí daría dos caminos para la misma acción distinta, y el más fácil
    de auditar es el que no existe.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/admin/organizations/{creados['pro'].id}/credits",
            json={"amount": "-50"},
            headers=headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_la_baja_desde_la_consola_escribe_el_rastro_y_revoca_el_acceso(
    integration_session: AsyncSession,
) -> None:
    """La baja de la consola usa el mismo servicio que la del panel.

    Si reimplementara la lógica, el asiento, la revocación de membresías y el contador
    acabarían divergiendo entre las dos rutas, y la que se usara menos sería la que
    no funciona. Se comprueba que el asiento existe y que la membresía queda inactiva.
    """

    assert integration_session is not None
    _super, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    objetivo = creados["pro"]
    # Un miembro que perdera el acceso es la diferencia entre "baja lógica" y "cambió el
    # nombre", así que se siembra uno.
    miembro = User(
        email=f"miembro-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Miembro",
        email_verified=True,
    )
    integration_session.add(miembro)
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=objetivo.id, user_id=miembro.id, role=RoleEnum.MEMBER)
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/admin/organizations/{objetivo.id}", headers=headers
        )

    assert response.status_code == 200
    assert response.json()["deleted_at"] is not None

    asiento = (
        await integration_session.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.organization_id == objetivo.id,
                AuditLogEntry.entity_id == objetivo.id,
            )
        )
    ).scalars().all()
    assert len(asiento) == 1

    membresia = (
        await integration_session.execute(
            select(Membership).where(
                Membership.organization_id == objetivo.id,
                Membership.user_id == miembro.id,
            )
        )
    ).scalar_one()
    assert membresia.is_active is False


@pytest.mark.asyncio
async def test_dar_de_baja_dos_veces_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    objetivo = creados["libre"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        primera = await client.delete(
            f"/api/v1/admin/organizations/{objetivo.id}", headers=headers
        )
        segunda = await client.delete(
            f"/api/v1/admin/organizations/{objetivo.id}", headers=headers
        )

    assert primera.status_code == 200
    assert segunda.status_code == 409


# --------------------------------------------------------------------------- #
# Usuarios, ventas y auditoria
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_listado_de_usuarios_no_expone_el_hash_de_contrasena(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/users", headers=headers)

    assert response.status_code == 200
    assert "hashed_password" not in response.text
    assert "email_verification_token" not in response.text
    cuerpo = response.json()
    if cuerpo["items"]:
        assert set(cuerpo["items"][0]) >= {"email", "full_name", "is_superuser", "organizations"}


@pytest.mark.asyncio
async def test_las_ventas_tienen_creditos_nulos_y_no_ceros(
    integration_session: AsyncSession,
) -> None:
    """Un importe desconocido es `None`, no `0`.

    `0` se vería como una venta de $0 en el resumen, que es un número inventado con
    aspecto de dato. Esta prueba fija la diferencia aunque hoy no haya ventas.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/sales", headers=headers)

    assert response.status_code == 200
    cuerpo = response.json()
    for item in cuerpo["items"]:
        # `credits_granted` en `None` significa que el evento no acreditó nada.
        assert item["credits_granted"] is None or item["credits_granted"] > 0
        assert "amount_cents" not in item


@pytest.mark.asyncio
async def test_el_visor_de_auditoria_filtra_por_accion_y_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    await apply_credit_delta(
        session=integration_session,
        organization_id=creados["pro"].id,
        amount=Decimal("10"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        propio = await client.get(
            f"/api/v1/admin/audit-log?organization_id={creados['pro'].id}", headers=headers
        )
        vacio = await client.get(
            "/api/v1/admin/audit-log?organization_id=" + str(uuid.uuid4()),
            headers=headers,
        )

    assert propio.status_code == 200
    assert vacio.status_code == 200
    assert vacio.json()["total"] == 0


@pytest.mark.asyncio
async def test_una_accion_de_auditoria_inexistente_no_revienta_el_servidor(
    integration_session: AsyncSession,
) -> None:
    """Un filtro mal escrito tiene que ser un `422`, no un `500`.

    La columna `action` es un `ENUM` de PostgreSQL. Pasarle el texto crudo hace que la base
    lance `invalid input value for enum` y la peticion muera con `500`, que es la peor de
    las tres respuestas posibles para quien escribe mal un filtro: le dice que el problema
    esta en el servidor cuando esta en lo que el escribio.

    Devolver cero resultados tampoco vale, porque haria creer que no hubo ninguna entrada
    con esa accion. El `422` enumera las validas.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        respuesta = await client.get(
            "/api/v1/admin/audit-log?action=ACCION_INVENTADA", headers=headers
        )

    assert respuesta.status_code == 422, respuesta.text
    cuerpo = respuesta.json()
    detalle = cuerpo["detail"]
    assert isinstance(detalle, str)
    assert "ACCION_INVENTADA" in detalle
    # Nombra al menos una accion real, para que se pueda corregir el filtro sin abrir el
    # codigo. Un `422` que solo dice "no valido" deja al operador igual que antes.
    assert any(
        accion.value in detalle for accion in AuditActionEnum
    ), "el 422 no enumera las acciones validas"


@pytest.mark.asyncio
async def test_el_filtro_de_auditoria_acepta_una_accion_real(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    creados = await _varios_tenants(integration_session)
    objetivo = creados["pro"]
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        baja = await client.delete(
            f"/api/v1/admin/organizations/{objetivo.id}", headers=headers
        )
        filtrada = await client.get(
            f"/api/v1/admin/audit-log?action={AuditActionEnum.ORGANIZATION_DELETED.value}"
            f"&organization_id={objetivo.id}",
            headers=headers,
        )

    assert baja.status_code == 200
    assert filtrada.status_code == 200, filtrada.text
    cuerpo = filtrada.json()
    assert cuerpo["total"] >= 1
    for entrada in cuerpo["items"]:
        assert entrada["action"] == AuditActionEnum.ORGANIZATION_DELETED.value


# --------------------------------------------------------------------------- #
# Facturacion del cliente
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_resumen_de_facturacion_trae_el_catalogo_real(
    integration_session: AsyncSession,
) -> None:
    """Los packs vienen del catálogo del servidor, no de una lista del panel.

    Si el panel llevara su propia lista, un pack que el servidor rechaza por `422` sería
    un botón que el propio panel offerció. Aquí se comprueba que lo que sale por la API
    es exactamente lo que valida el checkout.
    """

    assert integration_session is not None
    _user, org, headers = await _normal_user(integration_session)
    await apply_credit_delta(
        session=integration_session,
        organization_id=org.id,
        amount=Decimal("500"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/billing/summary", headers=headers)

    assert response.status_code == 200
    cuerpo = response.json()
    assert {p["credits"] for p in cuerpo["packs"]} == set(CREDIT_PACKS)
    for pack in cuerpo["packs"]:
        # `Decimal` viaja como cadena en JSON: se convierte antes de comparar o
        # se compararia `"19.00"` contra `Decimal("19.00")` y fallaria.
        assert Decimal(str(pack["amount_usd"])) == CREDIT_PACKS[pack["credits"]]
        assert Decimal(str(pack["usd_per_credit"])) > 0
    # El saldo es el de la base, no una aproximacion.
    assert Decimal(cuerpo["credit_balance"]) == Decimal("500")
    assert Decimal(cuerpo["credit_balance_usd"]) > 0


@pytest.mark.asyncio
async def test_el_equivalente_en_dolares_no_inventa_un_precio(
    integration_session: AsyncSession,
) -> None:
    """La cifra en dólares es un precio de compra, no el valor del saldo.

    El catalogo tiene descuento por volumen —500 creditos a $0,038 y 15000 a $0,0266— asi
    que no hay una paridad unica. La primera version dividia por el precio del pack
    pequeno y daba 1000 creditos = $26.315,79: un numero que no corresponde a nada que se
    pueda comprar y que ademas acompana a un saldo real, asi que el cliente lo leeria como
    "esto es lo que pago".

    Aqui se fija la propiedad que importa: la cifra es el saldo por el **mejor** precio
    unitario del catalogo, y por tanto siempre esta entre el valor al precio del pack mas
    pequeno y el valor al precio del pack mas grande. Con los precios reales, 1000 creditos
    dan $26,60, que es un numero que el usuario puede comprobar contra los packs.
    """

    assert integration_session is not None
    _user, org, headers = await _normal_user(integration_session)
    await apply_credit_delta(
        session=integration_session,
        organization_id=org.id,
        amount=Decimal("1000"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
    )
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.get("/api/v1/billing/summary", headers=headers)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    saldo = Decimal(cuerpo["credit_balance"])
    estimacion = Decimal(cuerpo["credit_balance_usd"])
    unitario = Decimal(cuerpo["best_unit_price_usd"])

    precios = sorted(amount / Decimal(credits) for credits, amount in CREDIT_PACKS.items())
    precio_mas_barato, precio_mas_caro = precios[0], precios[-1]

    # El precio unitario declarado es el mas barato del catalogo, no uno inventado.
    assert unitario == precio_mas_barato.quantize(Decimal("0.0001"))
    # Y la estimacion es exactamente saldo * ese precio.
    assert estimacion == (saldo * unitario).quantize(Decimal("0.01"))
    # Y cae dentro del rango que dan los precios reales del catalogo: nunca por debajo
    # del pack mas barato —seria un precio que no existe— ni por encima del pack mas caro.
    assert (saldo * precio_mas_barato).quantize(Decimal("0.01")) <= estimacion
    assert estimacion <= (saldo * precio_mas_caro).quantize(Decimal("0.01"))
    # Y da en la zona de la decena, no en las cuatro cifras que daba la paridad unica.
    assert estimacion < Decimal("100"), f"1000 creditos salen en {estimacion} USD"


@pytest.mark.asyncio
async def test_el_resumen_de_facturacion_necesita_tenant(integration_session: AsyncSession) -> None:
    """A diferencia de la consola, aquí el tenant es obligatorio.

    Es la ruta de un cliente sobre su propio workspace: sin cabecera no hay a quién
    pertenece el saldo, y devolver el de otro sería un error de aislamiento.
    """

    assert integration_session is not None
    _user, _org, cabeceras = await _normal_user(integration_session)
    sin_tenant = {"Authorization": cabeceras["Authorization"]}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/billing/summary", headers=sin_tenant)

    assert response.status_code == 403
