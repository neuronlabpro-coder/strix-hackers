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
from backend.apps.billing.models import CreditLedger, LedgerReasonEnum, StripeEvent
from backend.apps.billing.schemas import CREDIT_PACKS
from backend.apps.billing.service import apply_credit_delta
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.core.config import settings
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

#: Todas las rutas de la consola. Se prueban una por una para que una que se quede sin
#: cubrir falle sola, y no desaparezca en un parametro mal escrito.
ADMIN_RUTAS = [
    ("get", "/api/v1/admin/overview"),
    ("get", "/api/v1/admin/tenants"),
    ("get", "/api/v1/admin/users"),
    ("get", "/api/v1/admin/sales"),
    ("get", "/api/v1/admin/audit"),
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
            "/api/v1/admin/tenants", headers={**headers, "X-Organization-Id": str(otro.id)}
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
            "/api/v1/admin/tenants?plan=FREE&limit=100", headers=headers
        )
        dados_de_baja = await client.get(
            "/api/v1/admin/tenants?lifecycle=deleted&limit=100", headers=headers
        )
        activos = await client.get(
            "/api/v1/admin/tenants?lifecycle=active&limit=100", headers=headers
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
            "/api/v1/admin/tenants?lifecycle=inventado", headers=headers
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
            f"/api/v1/admin/tenants/{objetivo.id}",
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
            f"/api/v1/admin/tenants/{objetivo.id}/credits",
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
            f"/api/v1/admin/tenants/{creados['baja'].id}/credits",
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
            f"/api/v1/admin/tenants/{creados['pro'].id}/credits",
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
            f"/api/v1/admin/tenants/{objetivo.id}", headers=headers
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
            f"/api/v1/admin/tenants/{objetivo.id}", headers=headers
        )
        segunda = await client.delete(
            f"/api/v1/admin/tenants/{objetivo.id}", headers=headers
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
        respuesta = await client.get("/api/v1/admin/users", headers=headers)

    assert respuesta.status_code == 200
    assert "hashed_password" not in respuesta.text
    assert "email_verification_token" not in respuesta.text
    cuerpo = respuesta.json()
    if cuerpo["items"]:
        # `roles` sustituye a `organizations`: el rol viaja **con** el nombre del workspace,
        # porque un usuario puede ser admin en uno y miembro en otro, y una columna con un
        # único rol obligaría al operador a adivinar cuál de los dos.
        assert set(cuerpo["items"][0]) >= {
            "email",
            "full_name",
            "is_superuser",
            "roles",
        }


@pytest.mark.asyncio
async def test_las_ventas_tienen_creditos_nulos_y_no_ceros(
    integration_session: AsyncSession,
) -> None:
    """Un importe desconocido es `None`, no `0`.

    `0` se vería como una venta de $0 en el resumen, que es un número inventado con
    aspecto de dato. Esta prueba fija la diferencia aunque hoy no haya ventas.

    ## Por qué comprueba `amount_cents` y antes afirmaba que no existía

    Porque la columna se añadió después, a propósito, y esta prueba se quedó atrás.

    La tabla de eventos de Stripe guardaba **qué** se procesó, a qué organización y cuántos
    créditos acreditó, pero no cuánto se cobró. La elección fue añadir la columna y rellenarla
    en la ingestión del webhook —el único momento en que el payload está disponible— en vez de
    preguntar a Stripe por cada fila al pintar la vista.

    Y la prueba, escrita antes de esa decisión, afirmaba que `amount_cents` no debía aparecer en
    la respuesta. Seguía siendo cierta cuando no había ninguna venta que leer: el bucle no se
    ejecutaba. En cuanto el seeder sembró una venta, la afirmación dejó de cumplirse.

    La prueba no se limitó a borrarse: lo que hace ahora es fijar la regla que el esquema sí
    sostiene, que es la que importa. Un evento sin importe lleva `None`; un cobro lleva un entero
    positivo. Lo que no puede aparecer es un `0` con aspecto de cobro, porque Stripe no cobra
    cero y ese `0` sería un número inventado con forma de dato.
    """

    assert integration_session is not None
    _user, headers = await _superuser(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/sales", headers=headers)

    assert response.status_code == 200
    cuerpo = response.json()
    for item in cuerpo["items"]:
        # `credits_granted` llega como **cadena**, y no como número: es un `Decimal` de
        # PostgreSQL con cuatro decimales, y en JSON un decimal no tiene tipo propio.
        #
        # La prueba comparaba esa cadena con un entero. Pasaba mientras no hubiera ninguna fila
        # con créditos acreditados —porque el bucle no se ejecutaba— y en cuanto el seeder de
        # demostración sembró una venta, reventó con `'>' not supported between instances of
        # 'str' and 'int'`. La comparación no era incorrecta por los datos: lo era siempre, y
        # solo la ausencia de datos la tapaba.
        #
        # Convertir a `Decimal` es lo que hace falta para comparar en el mismo tipo en que el
        # servidor los guarda, y además hace que `"0.0000" > 0` dé `False` en vez de fallar.
        if item["credits_granted"] is not None:
            assert Decimal(str(item["credits_granted"])) > Decimal("0")

        # El importe de un cobro es un entero positivo, y el de un evento que no cobra es
        # `None`. Nunca `0`: no hay cobro de cero en Stripe, y un `0` ahí se leería en el panel
        # como una venta real de nada.
        if item["amount_cents"] is not None:
            assert item["amount_cents"] > 0


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
            f"/api/v1/admin/audit?organization_id={creados['pro'].id}", headers=headers
        )
        vacio = await client.get(
            "/api/v1/admin/audit?organization_id=" + str(uuid.uuid4()),
            headers=headers,
        )

    assert propio.status_code == 200
    assert vacio.status_code == 200
    assert vacio.json()["total"] == 0


@pytest.mark.asyncio
async def test_el_buscador_de_tenants_no_trata_los_comodines_como_comodines(
    integration_session: AsyncSession,
) -> None:
    r"""`%` y `_` se buscan literales en `/api/v1/admin/tenants?search=`.

    Este es el peor de los nueve, por una razón concreta: la consola lista **todos** los tenants
    de la plataforma, y la base de demostración acumula miles de organizaciones de sesiones
    anteriores. Sin escape, `?search=%` devolvía todas: el operador veía una lista enorme que no
    había pedido y no tenía forma de saber que su filtro no había filtrado nada.

    ## Por qué el `_` no es una rareza en esta tabla

    Porque los tenants de prueba llevan `_` en el nombre y en el slug —`Stripe E2E _algo_`,
    `UI _algo_`— y los de integración también. Buscar `web_app` sin escapar devolvería también
    `webXapp`, y buscar el sufijo exacto de un tenant devolvería los que solo coinciden en una
    posición.

    ## Por qué el `total` se compara con una lista sembrada y no con la base entera

    Porque el catálogo de tenants tiene filas de sesiones anteriores y su número cambia. La
    comprobación se apoya en un sufijo único: el término que lleva `%` solo puede casar con las
    dos filas que esta prueba ha sembrado, así que el número esperado es cerrado y el defecto se
    ve aunque la base tenga diez mil tenants.
    """

    session = integration_session
    assert session is not None
    _user, headers = await _superuser(session)
    sufijo = uuid.uuid4().hex[:8]

    def _tenant(nombre: str, slug: str) -> Organization:
        return Organization(name=nombre, slug=slug)

    # Cada par se diferencia solo en el carácter que se va a buscar: `_` contra `X` y `%` contra
    # `0`. Y cada término aparece en **una sola** fila, para que un fallo de escape se cuente
    # como un número y no como dos.
    session.add_all(
        [
            _tenant(f"Cliente web_app {sufijo}", f"web_app-{sufijo}"),
            _tenant(f"Cliente webXapp {sufijo}", f"webXapp-{sufijo}"),
            _tenant(f"Descuento 100%off {sufijo}", f"descuento-100off-{sufijo}"),
            _tenant(f"Descuento 1000off {sufijo}", f"descuento-1000off-{sufijo}"),
        ]
    )
    await session.commit()
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        con_porcentaje = await client.get(
            "/api/v1/admin/tenants", params={"search": "%", "limit": 100}, headers=headers
        )
        con_subrayado = await client.get(
            "/api/v1/admin/tenants",
            params={"search": f"web_app {sufijo}", "limit": 100},
            headers=headers,
        )
        con_texto = await client.get(
            "/api/v1/admin/tenants",
            params={"search": f"100%off {sufijo}", "limit": 100},
            headers=headers,
        )

    assert con_porcentaje.status_code == 200, con_porcentaje.text
    # `%` a secas devuelve **una** fila —la que lleva el símbolo— y no los miles de tenants de
    # la plataforma. Sin escape el total sería el número completo de organizaciones.
    cuerpo = con_porcentaje.json()
    assert cuerpo["total"] == 1, cuerpo["total"]
    assert cuerpo["items"][0]["name"] == f"Descuento 100%off {sufijo}"
    # `_` no es comodín de un carácter.
    cuerpo_subrayado = con_subrayado.json()
    assert cuerpo_subrayado["total"] == 1, cuerpo_subrayado["total"]
    assert cuerpo_subrayado["items"][0]["name"] == f"Cliente web_app {sufijo}"
    # Y el `%` en medio se busca literal, sin arrastrar al `1000off`.
    cuerpo_texto = con_texto.json()
    assert cuerpo_texto["total"] == 1, cuerpo_texto["total"]
    assert cuerpo_texto["items"][0]["name"] == f"Descuento 100%off {sufijo}"


@pytest.mark.asyncio
async def test_el_buscador_de_escaneos_no_trata_los_comodines_como_comodines(
    integration_session: AsyncSession,
) -> None:
    r"""`%` y `_` se buscan literales en `/api/v1/admin/operations/scans?busqueda=`.

    Sin escapar, `busqueda=%` devuelve **todos los escaneos de la plataforma**: el comodín va
    también en los dos extremos del patrón, así que `%\%` casa con cualquier objetivo. Aquí el
    fallo es peor que en un listado de cliente, porque el operador está mirando la consola de
    toda la instalación y el filtro que ha escrito no ha filtrado nada.

    ## Por qué `_` también aparece en esta pantalla

    Porque los objetivos que se escanean llevan `_` con frecuencia: un objetivo de pruebas se
    llama `web_app.staging.example.com` y el entorno de preproducción `webXapp.staging.example.com`
    es otra cosa. Sin escape, buscar el primero devuelve también el segundo.

    Se siembran cuatro escaneos con un sufijo único y se comprueba el `total` de cada respuesta. El
    `total` esperado vale 1, así que la prueba no depende de cuántas filas hay en la base: con
    escape, `%` a secas solo encuentra el escaneo que de verdad lleva el símbolo; sin escape,
    devolvería los miles de escaneos que la base de demostración arrastra de sesiones anteriores.
    """

    session = integration_session
    assert session is not None
    _user, headers = await _superuser(session)
    sufijo = uuid.uuid4().hex[:8]

    for objetivo in (
        f"web_app-{sufijo}.example.com",
        f"webXapp-{sufijo}.example.com",
        f"descuento-100%off-{sufijo}.example.com",
        f"descuento-1000off-{sufijo}.example.com",
    ):
        session.add(
            PentestRun(
                organization_id=uuid.UUID(headers["X-Organization-Id"]),
                target_type=TargetTypeEnum.DOMAIN,
                target_identifier=objetivo,
                scan_mode=ScanModeEnum.STANDARD,
                status=ScanStatusEnum.COMPLETED,
            )
        )
    await session.commit()
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        con_porcentaje = await client.get(
            "/api/v1/admin/operations/scans",
            params={"busqueda": "%", "limite": 100},
            headers=headers,
        )
        con_subrayado = await client.get(
            "/api/v1/admin/operations/scans",
            params={"busqueda": f"web_app-{sufijo}", "limite": 100},
            headers=headers,
        )
        con_texto = await client.get(
            "/api/v1/admin/operations/scans",
            params={"busqueda": f"100%off-{sufijo}", "limite": 100},
            headers=headers,
        )

    assert con_porcentaje.status_code == 200, con_porcentaje.text
    assert con_subrayado.status_code == 200, con_subrayado.text
    assert con_texto.status_code == 200, con_texto.text

    # `%` a secas devuelve **uno**, y la plataforma tenía ya `sesion_antes` escaneos más los
    # cuatro recién sembrados. Sin escape, el total sería ese número entero.
    cuerpo_porcentaje = con_porcentaje.json()
    assert cuerpo_porcentaje["total"] == 1, cuerpo_porcentaje["total"]
    assert cuerpo_porcentaje["items"][0]["target_identifier"] == (
        f"descuento-100%off-{sufijo}.example.com"
    )
    # `_` no es comodín de un carácter.
    cuerpo_subrayado = con_subrayado.json()
    assert cuerpo_subrayado["total"] == 1, cuerpo_subrayado["total"]
    assert cuerpo_subrayado["items"][0]["target_identifier"] == f"web_app-{sufijo}.example.com"
    # Y el `%` en medio se busca literal, sin arrastrar al `1000off`.
    cuerpo_texto = con_texto.json()
    assert cuerpo_texto["total"] == 1, cuerpo_texto["total"]
    assert cuerpo_texto["items"][0]["target_identifier"] == (
        f"descuento-100%off-{sufijo}.example.com"
    )


@pytest.mark.asyncio
async def test_el_buscador_de_usuarios_no_trata_el_porcentaje_como_comodin(
    integration_session: AsyncSession,
) -> None:
    r"""`?search=%` busca un símbolo de porcentaje, no devuelve todos los usuarios.

    Es el fallo más silencioso de un buscador: la pantalla responde, enseña filas y el operador
    da por bueno un filtro que no ha filtrado nada. Sin escapar el comodín, el patrón es
    `%\%%`, que casa con cualquier valor.
    """

    session = integration_session
    assert session is not None
    _user, headers = await _superuser(session)
    await _varios_tenants(session)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.get("/api/v1/admin/users", params={"search": "%"}, headers=headers)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    # El término sale por la URL sin decodificar, así que se escribe el valor ya codificado.
    assert cuerpo["total"] == 0, cuerpo
    assert cuerpo["items"] == []


@pytest.mark.asyncio
async def test_el_buscador_de_usuarios_no_trata_el_subrayado_como_comodin(
    integration_session: AsyncSession,
) -> None:
    """Un correo con `_` se busca literal, no como comodín de un carácter.

    El `_` no es una rareza en esta tabla: los correos de prueba y de integración llevan `_` en
    el prefijo, y un nombre de persona puede llevarlo. Sin escapar, `web_app` también devolvería
    `webXapp`.
    """

    session = integration_session
    assert session is not None
    suffix = uuid.uuid4().hex
    con_subrayado = User(
        email=f"web_app-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Con underscore",
        email_verified=True,
    )
    session.add(con_subrayado)
    await session.commit()
    _user, headers = await _superuser(session)
    transporte = ASGITransport(app=app)

    # El usuario **no se borra** en un `finally`, y no es un descuido: la propia creación del
    # superusuario y de los tenants escribe asientos en `audit_log`, que apunta a `actor_user_id`
    # con clave foránea y es *append-only* por R4. Un `DELETE FROM users` choca con las dos
    # cosas y la prueba falla por el estado que ella misma ha creado. Se deja la cuenta, con un
    # sufijo único: no molesta a ninguna otra prueba porque nadie la busca.
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.get(
            "/api/v1/admin/users",
            params={"search": f"web_app-{suffix}"},
            headers=headers,
        )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert cuerpo["items"][0]["id"] == str(con_subrayado.id)


@pytest.mark.asyncio
async def test_el_rango_de_auditoria_usa_la_fecha_de_alta_y_es_inclusivo(
    integration_session: AsyncSession,
) -> None:
    """Del 1 al 3 son tres días, y el tercero entra entero.

    `created_at` no puede ser `NULL` nunca, así que un rango sobre ella recorta filas. Sobre una
    columna anulable el rango las **borraría**, que es el motivo por el que no se usa aquí
    `updated_at`.
    """

    session = integration_session
    assert session is not None
    _user, headers = await _superuser(session)
    organization = await _varios_tenants(session)
    entrada = AuditLogEntry(
        organization_id=organization["pro"].id,
        actor_user_id=None,
        action=AuditActionEnum.ORGANIZATION_RENAMED,
        entity_type="organization",
        entity_id=str(organization["pro"].id),
        created_at=datetime(2026, 3, 3, 23, 30, tzinfo=UTC),
    )
    session.add(entrada)
    await session.commit()
    transporte = ASGITransport(app=app)

    # La entrada **no se borra** en el `finally`, y no es un descuido: `audit_log` es
    # *append-only* por R4 y la base tiene un trigger que rechaza `DELETE` con un `RaiseError`.
    # Intentar limpiarla haría fallar la prueba por la regla que está comprobando. Se deja la
    # fila: es una entrada de auditoría más, con una fecha de 2026 que ninguna ventana de
    # rango del resto de la batería solapa.
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        dentro = await client.get(
            "/api/v1/admin/audit",
            params={
                "created_from": "2026-03-01",
                "created_to": "2026-03-03",
            },
            headers=headers,
        )
        invertido = await client.get(
            "/api/v1/admin/audit",
            params={"created_from": "2026-03-05", "created_to": "2026-03-01"},
            headers=headers,
        )

    assert dentro.status_code == 200, dentro.text
    ids = {item["id"] for item in dentro.json()["items"]}
    assert str(entrada.id) in ids
    # Un rango invertido no es un `422`: las dos condiciones son incompatibles por
    # construcción y la lista vacía ya es la respuesta.
    assert invertido.status_code == 200, invertido.text
    assert invertido.json()["total"] == 0


@pytest.mark.asyncio
async def test_las_ventas_se_filtran_por_texto_y_por_fecha(
    integration_session: AsyncSession,
) -> None:
    """El buscador de ventas alcanza al evento y al nombre del tenant.

    Con solo el `event_id` el operador no encontraría una venta por el nombre del cliente que
    la pagó, que es por lo que se busca más de la mitad de las veces. Y el rango va sobre
    `created_at`, la única columna de fecha de la tabla.
    """

    session = integration_session
    assert session is not None
    _user, headers = await _superuser(session)
    creados = await _varios_tenants(session)
    objetivo = creados["pro"]
    evento = StripeEvent(
        organization_id=objetivo.id,
        event_id="evt_filtro_texto_001",
        event_type="checkout.session.completed",
        created_at=datetime(2026, 3, 10, 12, 0, tzinfo=UTC),
    )
    session.add(evento)
    await session.commit()
    transporte = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transporte, base_url="http://test") as client:
            por_evento = await client.get(
                "/api/v1/admin/sales",
                params={"query": "evt_filtro_texto_001"},
                headers=headers,
            )
            por_tenant = await client.get(
                "/api/v1/admin/sales",
                params={"query": objetivo.name},
                headers=headers,
            )
            por_fecha = await client.get(
                "/api/v1/admin/sales",
                params={"created_from": "2026-03-10", "created_to": "2026-03-10"},
                headers=headers,
            )
            fuera_de_rango = await client.get(
                "/api/v1/admin/sales",
                params={"created_from": "2026-03-11", "created_to": "2026-03-12"},
                headers=headers,
            )

        assert por_evento.status_code == 200, por_evento.text
        ids = {item["id"] for item in por_evento.json()["items"]}
        assert ids == {str(evento.id)}

        assert por_tenant.status_code == 200, por_tenant.text
        assert str(evento.id) in {item["id"] for item in por_tenant.json()["items"]}

        assert por_fecha.status_code == 200, por_fecha.text
        assert str(evento.id) in {item["id"] for item in por_fecha.json()["items"]}

        assert fuera_de_rango.status_code == 200, fuera_de_rango.text
        assert str(evento.id) not in {item["id"] for item in fuera_de_rango.json()["items"]}
    finally:
        await session.delete(evento)
        await session.commit()


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
            "/api/v1/admin/audit?action=ACCION_INVENTADA", headers=headers
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
            f"/api/v1/admin/tenants/{objetivo.id}", headers=headers
        )
        filtrada = await client.get(
            f"/api/v1/admin/audit?action={AuditActionEnum.ORGANIZATION_DELETED.value}"
            f"&organization_id={objetivo.id}",
            headers=headers,
        )

    assert baja.status_code == 200
    assert filtrada.status_code == 200, filtrada.text
    cuerpo = filtrada.json()
    assert cuerpo["total"] >= 1
    for entrada in cuerpo["items"]:
        assert entrada["action"] == AuditActionEnum.ORGANIZATION_DELETED.value


async def _sesion_de_usuario(
    session: AsyncSession, organization: Organization
) -> dict[str, str]:
    """Une un usuario nuevo a un tenant y devuelve sus cabeceras autenticadas."""

    user = User(
        email=f"miembro-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Miembro",
        email_verified=True,
    )
    session.add(user)
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.MEMBER)
    )
    await session.commit()
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


@pytest.mark.asyncio
async def test_los_organizaciones_derivadas_llegan_al_cliente(
    integration_session: AsyncSession,
) -> None:
    """`organizations` se deriva con `@computed_field`, y por eso **sí** viaja.

    Un `@property` a secas existe en Python pero Pydantic no lo serializa: el cliente
    recibía `organizations: null` mientras el backend creía que lo estaba mandando. Solo
    se descubrió leyendo el JSON de una respuesta real, y por eso esta prueba mira el JSON
    y no el modelo en memoria —que sí tendría el atributo y no lo delataría.
    """

    assert integration_session is not None
    organization = (await _varios_tenants(integration_session))["pro"]
    await _sesion_de_usuario(integration_session, organization)
    _super, headers = await _superuser(integration_session)

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.get("/api/v1/admin/users?limit=100", headers=headers)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["items"], "no hay usuarios que comprobar"
    propias = [u for u in cuerpo["items"] if organization.name in " ".join(u["roles"])]
    assert propias, "el usuario de la prueba no salio en la pagina"
    usuario = propias[0]
    # El rol es `member`: `_sesion_de_usuario` une al usuario como miembro, y el texto
    # debe llevar el rol real y no uno supuesto.
    assert usuario["roles"] == [f"{organization.name} (member)"]
    assert usuario["organizations"] == [organization.name]


@pytest.mark.asyncio
async def test_editar_usuarios_exige_superusuario(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = (await _varios_tenants(integration_session))["pro"]
    cabeceras = await _sesion_de_usuario(integration_session, organization)
    objetivo = (await integration_session.execute(select(User).limit(1))).scalar_one()
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/admin/users/{objetivo.id}",
            json={"is_superuser": True},
            headers=cabeceras,
        )

    assert respuesta.status_code == 403


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
async def test_el_equivalente_en_dolares_es_comprobable_por_el_cliente(
    integration_session: AsyncSession,
) -> None:
    """La cifra en dólares es la **paridad declarada**, no una estimación.

    El catálogo tuvo descuento por volumen —500 créditos a $0,038 y 15000 a $0,0266— y en
    medio de un descuento no existe un precio por crédito: «100 créditos» no tenía un
    precio, tenía un rango. La primera versión de esta vista dividía por el precio del
    pack pequeño y daba 1000 créditos = **$26.315,79**: un número que no correspondía a
    nada comprable y que además acompañaba a un saldo real, así que el cliente lo leía
    como «esto es lo que pago por lo que ya compré».

    Ahora el catálogo está a la paridad de `settings.credits_per_usd`, y sin descuento el
    precio de cualquier cantidad es su cantidad. Eso permite una afirmación mucho más
    fuerte que «está dentro de un rango»: el saldo en dólares es exactamente el saldo por
    la paridad, y el cliente puede comprobarlo.
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
    importe = Decimal(cuerpo["credit_balance_usd"])
    paridad = Decimal(cuerpo["credits_per_usd"])

    # La paridad que viaja es la de la configuración, no una constante del módulo.
    assert paridad == settings.credits_per_usd
    # Y el importe es exactamente saldo por la paridad. Comprobable, no estimable.
    assert importe == (saldo * paridad).quantize(Decimal("0.01"))
    # Con la paridad 1:1, 1000 créditos son 1000 dólares. Y no $26.315.
    assert importe == Decimal("1000.00")
    # Los packs respetan la misma paridad, que es lo que hace coherente la pantalla.
    for pack in cuerpo["packs"]:
        assert Decimal(pack["amount_usd"]) == Decimal(pack["credits"]) * paridad


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
