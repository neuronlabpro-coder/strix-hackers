"""Pruebas de la facturación dual: suscripción Pro y escalera de descuento por volumen.

## Qué se comprueba y por qué

La escalera de descuento es la parte más fácil de convertir en un agujero de margen, y las
pruebas están organizadas alrededor de las **propiedades** que la hacen segura, no de
ejemplos sueltos. Un ejemplo concreto pasa con una implementación que se rompe en otro
punto; una propiedad verificada sobre todo el rango no.

Las cuatro propiedades que se comprueban:

1. **`credits_for_spend` es monótona.** Gastar más da más créditos, sin excepciones. Es la
   que garantiza que el slider no pueda ofrecer una compra donde gastar más sale peor.
2. **`price_for_credits` es monótona.** Comprar más nunca cuesta menos. Sin esto, la
   frontera de cada umbral regala al cliente, y la primera versión de esta escalera
   fallaba justo aquí.
3. **Las dos funciones son coherentes entre sí en los dos sentidos.** Nunca se entregan más
   créditos de los que se pagan, y nunca se cobra más de lo que da lo comprado.
4. **Los packs fijos siguen cuadrando.** La escalera no puede haber movido un precio que
   el panel ya mostraba.

## Por qué se recorren todos los valores y no una muestra

Una muestra deja huecos justo donde están los fallos: los umbrales son 251, 1001, 3001 y
5001, y un `parametrize` con diez valores los rodearía sin tocarlos. Recorrer los 1.000.001
puntos de gasto y los 13.334 de créditos tarda menos de un segundo y no deja nada sin
mirar. El coste de un rango pequeño es que el siguiente tramo que alguien añada no lo
comprueba nadie.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.schemas import (
    CREDIT_PACKS,
    CUSTOM_CREDITS_MAXIMUM,
    CUSTOM_CREDITS_MINIMUM,
    PRO_SUBSCRIPTION_MONTHLY_USD,
    PRO_SUBSCRIPTION_PLAN,
    VOLUME_DISCOUNT_TIERS,
    CheckoutSessionRequest,
    credits_for_spend,
    discount_for_spend,
    price_for_credits,
    subscription_price_usd,
)
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

#: Céntimos de gasto que se recorren en las pruebas de monotonía. El rango completo son
#: 1.000.001 puntos ($0 a $10.000, de centavo en centavo).
CENTIMOS_MAXIMO = 1_000_000

#: Las URLs de retorno que el endpoint exige. No se validan contra el dominio real porque
#: `_validate_redirect` las compara con `settings.frontend_base_url`, y la prueba va al
#: endpoint de verdad.
URL_BASE = "http://test"


def _headers(user: User, organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _tenant(
    session: AsyncSession, *, plan: PlanTierEnum = PlanTierEnum.FREE
) -> tuple[User, Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Facturacion {suffix}",
        slug=f"facturacion-{suffix}",
        plan_tier=plan,
    )
    user = User(
        email=f"facturacion-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Admin de facturacion",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return user, organization, _headers(user, organization)


# --------------------------------------------------------------------------- #
# Propiedad 1: gastar mas da mas creditos
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gastar_mas_da_mas_creditos_en_todo_el_rango() -> None:
    """`credits_for_spend` no baja en ningún punto de $0 a $10.000.

    Se recorre **todo** el rango, de centavo en centavo. Una muestra con diez valores
    dejaría sin mirar justo los umbrales, que es donde una escalera mal construida da el
    salto hacia abajo.
    """

    anterior = -1
    retrocesos: list[tuple[int, int, int]] = []
    for centimos in range(CENTIMOS_MAXIMO + 1):
        actual = credits_for_spend(Decimal(centimos) / 100)
        if actual < anterior:
            retrocesos.append((centimos, anterior, actual))
        anterior = actual

    assert not retrocesos, f"la escalera retrocede en {len(retrocesos)} puntos: {retrocesos[:5]}"


@pytest.mark.asyncio
async def test_cada_frontera_sube_creditos() -> None:
    """Cruzar un umbral da **más** créditos, no menos.

    Es la propiedad que hace que el descuento signifique algo. Un umbral que no sube el
    salto significaría que el cliente paga más por lo mismo, que es la forma más
    sofisticada de perder un cliente sin darse cuenta.
    """

    for minimo, _descuento in VOLUME_DISCOUNT_TIERS:
        antes = credits_for_spend(minimo - Decimal("0.01"))
        despues = credits_for_spend(minimo)
        assert despues > antes, (
            f"en ${minimo} el saldo no sube: {antes} -> {despues}"
        )


# --------------------------------------------------------------------------- #
# Propiedad 2: comprar mas nunca sale mas barato
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_comprar_mas_nunca_cuesta_menos() -> None:
    """`price_for_credits` es monótona en todo el rango de créditos.

    Esta es la prueba que la primera versión de la escalera suspendía. El fallo era
    concreto: 278 créditos costaban $278,00 y 279 créditos costaban $251,10, veintisiete
    dólares menos por un crédito más. Se colaba porque el tramo base no tenía cota
    superior y absorbía todas las cantidades por encima de su mínimo.
    """

    anterior = Decimal("-1")
    retrocesos: list[tuple[int, Decimal, Decimal]] = []
    for creditos in range(CUSTOM_CREDITS_MINIMUM, CUSTOM_CREDITS_MAXIMUM + 1):
        actual = price_for_credits(creditos)
        if actual < anterior:
            retrocesos.append((creditos, anterior, actual))
        anterior = actual

    assert not retrocesos, (
        f"comprar mas sale mas barato en {len(retrocesos)} puntos: {retrocesos[:5]}"
    )


# --------------------------------------------------------------------------- #
# Propiedad 3: coherencia entre las dos funciones, en los dos sentidos
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nunca_se_entregan_mas_creditos_de_los_que_se_pagan() -> None:
    """Comprar con un gasto X nunca da créditos que cuesten más de X.

    Es la propiedad de seguridad del cobro. Si se cumpliera al revés, el sistema regalar
    saldo en cada compra, y la pérdida se acumularía en cada una de las miles de
    operaciones de un tenant grande sin aparecer en ningún sitio hasta el cierre de mes.
    """

    excedentes: list[tuple[Decimal, int, Decimal]] = []
    for centimos in range(1000, CENTIMOS_MAXIMO + 1):
        gasto = Decimal(centimos) / 100
        creditos = credits_for_spend(gasto)
        precio = price_for_credits(creditos)
        if precio > gasto:
            excedentes.append((gasto, creditos, precio))

    assert not excedentes, (
        f"se regalan creditos en {len(excedentes)} puntos: {excedentes[:5]}"
    )


@pytest.mark.asyncio
async def test_nunca_se_cobra_mas_de_lo_que_da_lo_comprado() -> None:
    """Comprar N créditos nunca cuesta más de lo que cuesta N créditos por el catálogo.

    La dirección contraria de la anterior. Esta falla si la función de precio se queda
    corta en un tramo y el cliente recibe un saldo menor del que pagó.
    """

    cortas: list[tuple[int, Decimal, int]] = []
    for creditos in range(CUSTOM_CREDITS_MINIMUM, CUSTOM_CREDITS_MAXIMUM + 1):
        precio = price_for_credits(creditos)
        entregados = credits_for_spend(precio)
        if entregados < creditos:
            cortas.append((creditos, precio, entregados))

    assert not cortas, f"se cobra por mas de lo que se entrega en {len(cortas)}: {cortas[:5]}"


# --------------------------------------------------------------------------- #
# La escalera, en sus valores concretos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("gasto", "descuento_esperado"),
    [
        (Decimal("10.00"), Decimal("0.00")),
        (Decimal("250.00"), Decimal("0.00")),
        (Decimal("250.99"), Decimal("0.00")),
        (Decimal("251.00"), Decimal("0.10")),
        (Decimal("1000.00"), Decimal("0.10")),
        (Decimal("1000.99"), Decimal("0.10")),
        (Decimal("1001.00"), Decimal("0.15")),
        (Decimal("3000.99"), Decimal("0.15")),
        (Decimal("3001.00"), Decimal("0.20")),
        (Decimal("5000.99"), Decimal("0.20")),
        (Decimal("5001.00"), Decimal("0.25")),
        (Decimal("10000.00"), Decimal("0.25")),
    ],
)
@pytest.mark.asyncio
async def test_el_tramo_se_elige_por_el_gasto(
    gasto: Decimal, descuento_esperado: Decimal
) -> None:
    """El tramo se decide por cuánto se gasta, con umbrales y no rangos cerrados.

    Los valores a uno céntimo del umbral están a propósito: `$250,99` y `$251,00` son
    casos distintos y tienen que resolver a tramos distintos. Con rangos cerrados
    (`$251 ≤ x ≤ $1000`) habría que tratar los bordes, y ese es el sitio donde una
    escalera se vuelve inconsistente.
    """

    assert discount_for_spend(gasto) == descuento_esperado


@pytest.mark.asyncio
async def test_el_descuento_del_tope_es_el_25_por_ciento() -> None:
    """A $10.000 el cliente paga $0,75 por crédito, y el ahorro es del 25%.

    Se comprueba el ahorro y **no** solo el precio unitario, porque son dos cosas distintas
    y una de ellas puede estar bien con la otra mal: un catálogo que mostrara «$0,75 por
    crédito» y cobrase $10.000 sería coherente en la letra y no en el hecho. La aserción
    mira las dos cifras.
    """

    creditos = CUSTOM_CREDITS_MAXIMUM
    precio = price_for_credits(creditos)
    lista = Decimal(creditos)
    ahorro = lista - precio

    assert precio == Decimal("9999.75"), "el tope debe cobrar $9.999,75"
    assert (precio / creditos).quantize(Decimal("0.01")) == Decimal("0.75")
    assert ahorro == Decimal("3333.25")
    assert (ahorro / lista * 100).quantize(Decimal("0.1")) == Decimal("25.0")


@pytest.mark.asyncio
async def test_los_packs_fijos_no_se_han_movido() -> None:
    """Los tres packs del catálogo siguen costando lo que costaban.

    La escalera no puede haber movido un precio que el panel ya muestra. Los tres están en
    el tramo base —25, 100 y 250 créditos son todos menos de $251— así que su precio sigue
    siendo su cantidad, y esta prueba lo fija.
    """

    for creditos, precio in CREDIT_PACKS.items():
        assert price_for_credits(creditos) == precio, (
            f"el pack de {creditos} créditos cambió de {precio} a "
            f"{price_for_credits(creditos)}"
        )


@pytest.mark.asyncio
async def test_un_presupuesto_por_debajo_del_minimo_no_compra_creditos() -> None:
    """$9.99 no da créditos. Devolver 10 haría prometer algo que el checkout rechazaría.

    El mínimo del catálogo es $10. El panel pinta ese mínimo, así que un slider que dejara
    llegar a $9,99 tiene que mostrar «no se puede comprar», no una cantidad de créditos que
    el endpoint después rechazaría con un `422`.
    """

    assert credits_for_spend(Decimal("9.99")) == 0
    assert credits_for_spend(Decimal("10.00")) == CUSTOM_CREDITS_MINIMUM


# --------------------------------------------------------------------------- #
# Los dos modos de checkout
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_una_recarga_nunca_lleva_creditos_por_sobre_del_maximo(
    integration_session: AsyncSession,
) -> None:
    """El esquema recorta la compra al máximo del catálogo.

    El tope de 13.333 créditos es el máximo comprable con el slider acotado a $10.000. Aceptar
    un millón dejaría pasar compras que la escalera no explica, y el cliente vería un saldo
    que no puede comprar ni convertir en nada.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/billing/checkout-session",
            json={
                "mode": "credits",
                "credits": CUSTOM_CREDITS_MAXIMUM + 1,
                "success_url": f"{URL_BASE}/billing",
                "cancel_url": f"{URL_BASE}/billing",
            },
            headers=cabeceras,
        )

    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.asyncio
async def test_una_suscripcion_no_puede_llevar_creditos(
    integration_session: AsyncSession,
) -> None:
    """`mode="subscription"` con `credits` es un `422`, no un campo ignorado.

    Ignorarlo dejaría al cliente pagando la cuota **y** creyendo que ha comprado créditos.
    Rechazarlo dice exactamente qué hacer: quitar el campo, o usar `mode="credits"`.
    """

    with pytest.raises(ValueError, match="suscripción"):
        CheckoutSessionRequest(
            mode="subscription",
            credits=100,
            success_url=f"{URL_BASE}/billing",
            cancel_url=f"{URL_BASE}/billing",
        )


@pytest.mark.asyncio
async def test_una_recarga_exige_creditos(integration_session: AsyncSession) -> None:
    """`mode="credits"` sin `credits` es un `422`.

    La otra mitad del discriminante. Un `credits` ausente con un `default` de `None` sería
    un `0`, y un `0` reaches `apply_credit_delta` como un asiento de valor cero.
    """

    with pytest.raises(ValueError, match="cantidad de créditos"):
        CheckoutSessionRequest(
            mode="credits",
            success_url=f"{URL_BASE}/billing",
            cancel_url=f"{URL_BASE}/billing",
        )


@pytest.mark.asyncio
async def test_el_importe_lo_decide_el_catalogo_no_el_cliente() -> None:
    """El cuerpo no admite un campo de importe: `extra="forbid"` lo rechaza.

    Es la propiedad de seguridad del cobro, comprobada en el esquema. Un endpoint que
    aceptara `amount` del cliente sería un endpoint donde quien llama decide cuánto paga por
    cuántos créditos. Manda `credits`, y el importe sale de `price_for_credits`.

    Se valida desde un **diccionario** y no con argumentos de palabra clave. Un campo que el
    esquema no declara no se puede pasar por parámetro —el constructor ni lo acepta—, y
    escribirlo en la llamada no probaría el `extra="forbid"` sino un error de Python. El
    diccionario es además lo que FastAPI recibe de verdad, así que la prueba mide el camino
    real.
    """

    with pytest.raises(ValueError, match="amount"):
        CheckoutSessionRequest.model_validate(
            {
                "mode": "credits",
                "credits": 100,
                "amount": "1.00",
                "success_url": f"{URL_BASE}/billing",
                "cancel_url": f"{URL_BASE}/billing",
            }
        )


@pytest.mark.asyncio
async def test_el_cliente_no_puede_elegir_el_descuento() -> None:
    """El cuerpo no admite un campo de descuento.

    La misma propiedad por el otro lado. La escalera la decide el servidor; si el cliente
    pudiera mandar el porcentaje, el endpoint sería un descuento libre.
    """

    with pytest.raises(ValueError, match="discount"):
        CheckoutSessionRequest.model_validate(
            {
                "mode": "credits",
                "credits": 10_000,
                "discount": "0.90",
                "success_url": f"{URL_BASE}/billing",
                "cancel_url": f"{URL_BASE}/billing",
            }
        )


@pytest.mark.asyncio
async def test_la_suscripcion_cuesta_el_precio_de_la_escalera() -> None:
    """La cuota mensual es el precio de la suscripción, no el de los créditos.

    La comprobación fija el número: si la cuota se derivara de la escalera, subir el precio
    de la suscripción cambiaría el de los créditos, y los dos quedarían acoplados sin que
    nadie lo quisiera.
    """

    assert subscription_price_usd() == PRO_SUBSCRIPTION_MONTHLY_USD
    assert subscription_price_usd() == Decimal("29.00")
    assert PRO_SUBSCRIPTION_PLAN is PlanTierEnum.PRO


# --------------------------------------------------------------------------- #
# El resumen de facturación lleva la escalera
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_resumen_trae_la_escalera_y_la_oferta(
    integration_session: AsyncSession,
) -> None:
    """`/billing/summary` expone los tramos y la suscripción para que el panel no invente.

    Se comprueba la forma de la respuesta, no el contenido exacto de cada tramo: el
    contenido lo fijan las pruebas de la escalera, y duplicarlo aquí solo daría dos sitios
    que se desincronizan.

    Lo que sí se comprueba aquí es que `is_current_plan` distingue los dos casos, porque de
    eso depende que el botón aparezca activo o no y no hay otra prueba que lo cubra.
    """

    _user, org_free, cab_free = await _tenant(integration_session, plan=PlanTierEnum.FREE)
    _user2, _org_pro, cab_pro = await _tenant(
        integration_session, plan=PlanTierEnum.PRO
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        resumen_free = (await cliente.get("/api/v1/billing/summary", headers=cab_free)).json()
        resumen_pro = (await cliente.get("/api/v1/billing/summary", headers=cab_pro)).json()

    assert resumen_free["subscription"]["is_current_plan"] is False
    assert resumen_pro["subscription"]["is_current_plan"] is True
    assert resumen_free["subscription"]["monthly_usd"] == "29.00"
    assert resumen_free["subscription"]["plan_tier"] == "PRO"

    escalera = resumen_free["volume"]
    assert escalera["tiers"], "la escalera no puede estar vacía"
    assert escalera["minimum_credits"] == CUSTOM_CREDITS_MINIMUM
    assert escalera["maximum_credits"] == CUSTOM_CREDITS_MAXIMUM
    # Los cinco tramos del catálogo, en orden, con descuento creciente.
    descuentos = [t["discount"] for t in escalera["tiers"]]
    assert descuentos == sorted(descuentos), "los tramos no van de menor a mayor descuento"
    assert escalera["tiers"][-1]["discount"] == "0.25"
    del org_free, _user2
