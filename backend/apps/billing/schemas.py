"""Esquemas de la API de facturación."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.apps.billing.catalogo import catalogo_vigente
from backend.apps.billing.pricing import credits_per_usd
from backend.apps.organizations.models import PlanTierEnum

# Paquetes de créditos comercializables. R1 prohíbe precios en el código, así que
# viven aquí como catálogo versionable y no en el frontend: el panel los lee de
# esta respuesta y jamás inventa un importe.
CREDIT_PACKS: dict[int, Decimal] = {
    25: Decimal("25.00"),
    100: Decimal("100.00"),
    250: Decimal("250.00"),
}

#: Mínimo del pack a medida, en **dólares**. Con la paridad 1:1 del catálogo base, $10 son
#: 10 créditos, que es el mínimo que cubre el coste fijo de una sesión de Checkout.
#:
#: Vive aquí y no en la validación del endpoint por la misma razón que el resto del
#: catálogo: es política comercial versionable. El esquema lo lee, no lo decide.
CUSTOM_SPEND_MINIMUM_USD = Decimal("10.00")

#: Máximo del pack a medida, en **dólares**. Cierra el slider: por encima de esto hay que
#: hablar con ventas, y el panel dice eso en vez de mostrar un tramo más.
CUSTOM_SPEND_MAXIMUM_USD = Decimal("10000.00")

#: Precio de la suscripción Pro, en dólares al mes.
#:
#: R1 lo prohíbe en el código igual que los precios de los packs, con la misma excepción: es
#: **catálogo**, y vive aquí para que el endpoint de checkout, el panel y la documentación
#: lean el mismo número. El único sitio que lo lee es `subscription_price_usd()`.
PRO_SUBSCRIPTION_MONTHLY_USD = Decimal("29.00")

#: Plan que se activa al pagar la suscripción Pro.
PRO_SUBSCRIPTION_PLAN = PlanTierEnum.PRO


#: La escalera de descuento por volumen, **`(gasto_mínimo_usd, descuento)`**.
#:
#: ## Por qué el tramo se elige por el **gasto** y no por los créditos
#:
#: Es la decisión de diseño que hace coherente toda la escalera, y no es un detalle:
#:
#: ```
#: gasto $250   -> tramo 0%  -> 250 / 1.00 =  250 creditos
#: gasto $251   -> tramo 10% -> 251 / 0.90 =  278 creditos   <- mas creditos por mas dinero
#: gasto $1000  -> tramo 10% -> 1000 / 0.90 = 1111 creditos
#: gasto $1001  -> tramo 15% -> 1001 / 0.85 = 1177 creditos  <- mas creditos por mas dinero
#: gasto $5001  -> tramo 25% -> 5001 / 0.75 = 6668 creditos
#: ```
#:
#: El resultado es que `credits_for_spend` es **monotona creciente**: gastar mas da
#: siempre mas creditos, y en cada frontera hay un salto **hacia arriba**. Esa es la
#: propiedad que hace que comprar mas nunca salga contraproducente, y es comprobable
#: recorriendo los 9.991 valores del rango.
#:
#: Si el tramo se eligiera por creditos, la frontera daria un precio **menor** para mas
#: creditos —251 creditos a $0,90 son $225,90, menos que 250 creditos a $1,00 que son
#: $250,00— y el catalogo se convertiria en un regalo con un punto de inflexion
#: accidental. Ese fue el primer intento de esta escalera y salio con **cero** de ahorro
#: en todo el rango, que es otra forma de decir que no era un descuento.
#:
#: Por eso los umbrales estan en dolares, y por eso la unidad va en el comentario y no en
#: el nombre de la tupla: una tupla de dos `Decimal` sin unidad es la clase de cosa que
#: alguien lee al reves un martes por la noche.
VOLUME_DISCOUNT_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (CUSTOM_SPEND_MINIMUM_USD, Decimal("0.00")),
    (Decimal("251.00"), Decimal("0.10")),
    (Decimal("1001.00"), Decimal("0.15")),
    (Decimal("3001.00"), Decimal("0.20")),
    (Decimal("5001.00"), Decimal("0.25")),
)


def discount_for_spend(spend: Decimal) -> Decimal:
    """El descuento aplicable a un **gasto** en dólares.

    Tramos por umbral: el último cuyo `gasto_mínimo` es menor o igual que el gasto. Con
    umbrales y no rangos cerrados, el cliente no tiene que acertar en qué intervalo cae su
    compra: $250,99 y $251,01 se resuelven igual y sin casos especiales.
    """

    return catalogo_vigente().descuento_para(spend)


def _credits_for_spend(spend: Decimal) -> int:
    """Los créditos que da un gasto exacto, con el suelo entero.

    Es el cálculo central, y va **antes** de las funciones públicas porque los límites del
    catálogo se derivan de él: si el mínimo de gasto subiera, el mínimo de créditos tiene
    que moverse con él, y eso no se consigue con dos constantes escritas al lado.
    """

    descuento = discount_for_spend(spend)
    # `spend / (1 - descuento)` es lo mismo que esto con la paridad en 1,00, y es lo unico
    # que es cuando la paridad deja de ser 1: un dolar son `paridad` creditos, asi que un
    # gasto compra `spend * paridad` creditos antes del descuento. Sin este factor, con
    # paridad 2,50 el slider entregaria dos veces y media menos de lo que el cliente paga.
    unidades = spend * credits_per_usd() / (Decimal("1.00") - descuento)
    return int(unidades.to_integral_value(rounding="ROUND_FLOOR"))


def credits_for_spend(spend: Decimal) -> int:
    """Los créditos que compra un gasto dado. Es lo que usa el slider.

    ## Por qué el redondeo es hacia abajo

    Un crédito es la unidad indivisible del producto. Con decimales, darían saldos que no se
    pueden gastar enteros ni explicar en un soporte, y un saldo de $250,00 con 278,888
    créditos es un número que el cliente no puede usar para nada.

    El suelo es lo que hace segura la función: **nunca** se entregan más créditos de los que
    el gasto paga. Redondear hacia arriba sería regalar saldo en cada compra, y la pérdida
    se acumularía en cada una de las miles de operaciones de un tenant grande.

    ## Por qué un gasto por debajo del mínimo devuelve `0`

    Porque el mínimo del catálogo es un **gasto**, no una cantidad de créditos. Un gasto de
    $9,99 cae en el tramo base y la división da 9 créditos, que es una cantidad que existe
    en el mundo pero que **no se puede comprar**: `price_for_credits(9)` sale del rango, el
    esquema la rechaza con un `422` y el panel no podría ofrecerla.

    Devolver `0` es la respuesta que permite pintar «compra mínima $10» sin una aritmética
    especial en el cliente. El `0` no es un error ni un caso raro: es lo que el slider tiene
    que saber para deshabilitar su botón antes de que el usuario llegue al checkout.
    """

    if spend < catalogo_vigente().gasto_minimo_usd:
        return 0
    return _credits_for_spend(spend)


def price_for_credits(credits: int) -> Decimal:
    """El precio de una cantidad de créditos, en dólares.

    Es la **inversa** de `credits_for_spend`, y está definida como el mínimo gasto que
    entrega esa cantidad de créditos.

    ## La fórmula, y por qué lleva un `max`

    Para una cantidad `n` en un tramo de unidad `u` cuyo gasto mínimo es `m`, el gasto
    mínimo que entrega `n` créditos es `n * u` —el puro coste unitario—, **pero** no puede
    estar por debajo del umbral `m` del tramo, o el gasto caería en el tramo anterior. De
    ahí el `max(m, n * u)`.

    ```
    250 creditos: tramo 0%  -> max(10,  250.00) =  250.00   (y 250 < 251, valido)
    251 creditos: tramo 10% -> max(251, 225.90) =  251.00   (y 251 < 1001, valido)
    278 creditos: tramo 10% -> max(251, 250.20) =  251.00
    279 creditos: tramo 10% -> max(251, 251.10) =  251.10
    1.110:        tramo 10% -> max(1001, 999.00) = 1001.00
    1.113:        tramo 10% -> max(1001, 1001.70) = 1001.70
    ```

    ## Por qué el tramo tiene cota **superior**

    Este es el detalle que hace que la función sea monótona, y salta a la vista al mirar lo
    que pasa sin él. El tramo base no tiene techo natural, así que si se aceptara
    cualquier gasto por encima de su mínimo, absorbería todas las cantidades:

    ```
    sin cota superior:  278 creditos -> tramo 0% da 278.00, tramo 10% da 250.20 < 251 -> se descarta
                        279 creditos -> tramo 0% da 279.00, tramo 10% da 251.10 -> minimo 251.10
    ```

    278 créditos costarían $278,00 y 279 créditos costarían $251,10. Veintisiete dólares
    **menos** por un crédito más. Con la cota, el tramo base solo vale hasta el umbral
    siguiente, los dos precios salen del tramo 10% y la serie crece.

    Y el `max` es lo que hace que el precio no retroceda en la frontera: sin él, 251
    créditos caerían en el tramo 10% a $225,90, por debajo de los $250,00 de los 250
    créditos, y el mismo salto de $27,80 hacia abajo.

    ## Por qué el mínimo entre tramos, y no el primero que cuadre

    Un crédito concreto puede ser alcanzable en varios tramos con gastos distintos, porque
    el suelo al entero hace que el mismo crédito entre en dos tramos. El mínimo es el
    precio honesto —el más barato que el catálogo permite pagar por esa cantidad—; tomar
    el primero que cuadre daría un precio arbitrario entre varios válidos, y el mismo
    crédito costaría dos cosas según por dónde se mirara.
    """

    catalogo = catalogo_vigente()
    mejor: Decimal | None = None
    for indice, (minimo, descuento) in enumerate(catalogo.tramos):
        unidad = catalogo.usd_por_credito_con_descuento(descuento)
        candidato = max(minimo, Decimal(credits) * unidad)
        # La cota superior del tramo es el umbral del siguiente. El último tramo no tiene
        # cota: por encima del tope del catalogo no hay otro tramo, y ese camino lo corta la
        # validacion del esquema.
        if indice + 1 < len(catalogo.tramos):
            siguiente_minimo = catalogo.tramos[indice + 1][0]
            if candidato >= siguiente_minimo:
                continue
        if mejor is None or candidato < mejor:
            mejor = candidato
    if mejor is None:
        # Ningún tramo alcanza esta cantidad: se cobra a la paridad base. Solo pasa por
        # encima del tope del catálogo, y ese camino lo corta el esquema, no esta función.
        # Se devuelve un número en vez de fallar para que un valor inesperado no convierta
        # una compra legítima en un error interno.
        return (Decimal(credits) / credits_per_usd()).quantize(Decimal("0.01"))
    return mejor.quantize(Decimal("0.01"))


def subscription_price_usd() -> Decimal:
    """El precio mensual de la suscripción Pro, en dólares.

    Función y no constante directa en los llamadores, por el mismo motivo que
    `price_for_credits`: un único punto de lectura. El frontend no recibe este número de
    ningún lado —lo pide al endpoint de checkout—, de modo que R1 se respeta sin depender
    de que nadie se acuerde de no duplicarlo.
    """

    return catalogo_vigente().suscripcion_mensual_usd


#: Mínimo de créditos, **derivado** del mínimo de gasto y no escrito al lado.
#:
#: Es `credits_for_spend($10)`. Se deriva porque los dos números tienen que moverse juntos:
#: si el mínimo de gasto subiera a $50 y el mínimo de créditos se quedara en 10, el
#: endpoint aceptaría una compra que el catálogo no tiene. Escribirlos como dos constantes
#: sueltas es la misma clase de error que R1 prohíbe, con dos números en vez de uno.
CUSTOM_CREDITS_MINIMUM: int = credits_for_spend(CUSTOM_SPEND_MINIMUM_USD)

#: Máximo de créditos, derivado del tope de gasto por el mismo motivo.
CUSTOM_CREDITS_MAXIMUM: int = credits_for_spend(CUSTOM_SPEND_MAXIMUM_USD)


#: Los dos modos de pago que el panel puede pedir.
#:
#: `CREDITS` es una recarga puntual: se paga una vez y se acreditan créditos. `SUBSCRIPTION`
#: es la cuota mensual que sube el plan del workspace a Pro.
#:
#: Son un `Literal` y no dos endpoints porque comparten casi todo: las URLs de retorno, la
#: validación anti-open-redirect, el límite de peticiones, la metadata con el
#: `organization_id` y el manejo de error de Stripe. Separarlos duplicaría unas ochenta
#: líneas para diferenciarse en un parámetro que Stripe ya recibe.
CheckoutMode = Literal["credits", "subscription"]


class CheckoutSessionRequest(BaseModel):
    """Solicitud de sesión de Stripe Checkout: recarga de créditos o suscripción Pro.

    Admite las cuatro cosas que el panel puede pedir:

    - un pack del catálogo (`25`, `100`, `250`), que es lo que ofrecen las tarjetas;
    - cualquier cantidad dentro de la escalera de volumen, que es lo que produce el slider;
    - la suscripción Pro, con `mode="subscription"` y sin `credits`.

    ## Por qué se acepta una cantidad que no está en el catálogo

    Restringir la compra a tres valores obligaría al cliente que quiere 40 créditos a
    comprar 25 y desperdiciar 5, o 100 y pagar 60 de más. El mínimo cubre el coste fijo de
    la sesión de cobro; por encima de eso, cualquier cantidad es una compra legítima.

    ## Por qué `credits` es opcional y no simplemente no-presentado

    Pydantic no distingue "ausente" de `None` en un `int` con valor por defecto, así que
    `credits: int | None = None` con un validador que exige `None` cuando el modo es
    suscripción. La alternativa —dejar `credits` obligatorio y mandar `0`— daría un error
    confuso alPanel si alguna vez se equivoca al construir la petición.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: CheckoutMode = Field(
        default="credits",
        description="`credits` recarga saldo; `subscription` contrata el plan Pro.",
    )
    credits: int | None = Field(
        default=None,
        description="Créditos a comprar. Obligatorio salvo en modo `subscription`.",
        ge=CUSTOM_CREDITS_MINIMUM,
        le=CUSTOM_CREDITS_MAXIMUM,
    )
    success_url: str = Field(max_length=2048)
    cancel_url: str = Field(max_length=2048)

    @model_validator(mode="after")
    def validate_pack_and_urls(self) -> CheckoutSessionRequest:
        if self.mode == "subscription":
            if self.credits is not None:
                # Mandar créditos en una suscripción es una contradicción: se pagaría el
                # importe de los créditos **y** la cuota, dos veces por lo mismo. Se rechaza
                # en vez de ignorarlo porque ignorarlo dejaría al cliente pagando $29 y
                # pensando que ha comprado créditos.
                raise ValueError(
                    "Una suscripción no lleva créditos: quite el campo `credits` "
                    "o use `mode=\"credits\"`"
                )
        elif self.credits is None:
            raise ValueError("Una recarga necesita la cantidad de créditos a comprar")
        for field_name, value in (
            ("success_url", self.success_url),
            ("cancel_url", self.cancel_url),
        ):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{field_name} debe ser una URL absoluta HTTP(S)")
        return self

    @property
    def amount_usd(self) -> Decimal:
        """El importe de esta compra, resuelto por el catálogo.

        En modo suscripción es la cuota mensual, no el precio de los créditos. La función
        que decide el importe es la misma en los dos casos porque en los dos casos la decide
        el catálogo, no el cliente.
        """

        if self.mode == "subscription":
            return subscription_price_usd()
        if self.credits is None:  # pragma: no cover - el validador lo impide
            raise ValueError("Una recarga necesita la cantidad de créditos a comprar")
        return price_for_credits(self.credits)

    @property
    def granted_credits(self) -> int:
        """Los créditos que esta compra acredita. `0` en una suscripción.

        Una suscripción no acredita créditos: cambia el plan. Mezclar las dos cosas en un
        solo campo habría hecho que el webhook tuviera que adivinar de dónde salió cada
        saldo, y un error ahí es saldo regalado o saldo perdido.
        """

        return self.credits or 0


class CheckoutSessionResponse(BaseModel):
    """Sesión de pago creada. Nunca incluye la clave secreta del backend."""

    session_id: str
    url: str
    mode: CheckoutMode = "credits"
    #: `0` en una suscripción, que no acredita saldo sino que cambia el plan.
    credits: int
    amount_usd: Decimal
    currency: str = "usd"


class WebhookAckResponse(BaseModel):
    """Acuse de recibo del webhook de Stripe.

    Stripe reintenta cualquier respuesta que no sea `2xx`, así que el código de
    estado es parte del contrato: un evento que no se pudo aplicar por una causa
    permanente debe devolver `2xx` con `status: "ignored"` para detener los
    reintentos, y solo un error transitorio devuelve `5xx`.
    """

    status: Literal["processed", "ignored"]
    duplicate: bool = False
    event_id: str
    event_type: str
    credits_granted: int = 0
    balance_after: Decimal | None = None
    #: Plan resultante del evento, si el evento lo cambió. `None` en una recarga de saldo,
    #: que no toca el plan. Viaja para que el panel pueda refrescar la cabecera sin tener
    #: que pedir `/me` aparte después de cada pago.
    plan_tier: PlanTierEnum | None = None


class SubscriptionOfferResponse(BaseModel):
    """La oferta de suscripción Pro, tal como la ve el panel.

    ## Por qué viaja aquí y no la calcula el frontend

    R1. El panel necesita saber el precio para pintar el botón, y si lo compuso por su
    cuenta habría un número en el código del cliente que nadie cambia cuando sube el precio
    —que es el peor sitio posible para que viva un precio—.

    `plan_tier` viaja para que el panel pueda decir «ya estás en Pro» sin comparar strings.
    `credits_included` no existe a propósito: la suscripción cambia el plan y no acredita
    saldo. Mezclar las dos cosas haría que el cliente esperara créditos por pagar la cuota.
    """

    plan_tier: PlanTierEnum
    monthly_usd: Decimal = Field(gt=0)
    #: Si el tenant ya está en este plan, el panel lo pinta como activo en vez de ofrecerlo.
    is_current_plan: bool


class VolumeTierResponse(BaseModel):
    """Un tramo de la escalera de descuento, con sus dos extremos.

    Los dos, no solo el superior, porque el panel necesita pintar una barra de progreso con
    tramos de ancho proporcional: con solo el superior, todos los tramos Intermediate
    saldrían del mismo tamaño.
    """

    minimum_credits: int = Field(ge=1)
    maximum_credits: int = Field(ge=1)
    #: Descuento aplicable, como fracción: `0.10` es el 10%.
    discount: Decimal = Field(ge=0, lt=1)
    #: Precio unitario en este tramo. Lo calcula el servidor para que el panel no divida
    #: en el cliente, donde un redondeo distinto por navegador daría dos cifras para el
    #: mismo producto.
    usd_per_credit: Decimal = Field(gt=0)


class VolumePricingResponse(BaseModel):
    """La escalera completa y los límites del control, para que el panel no los invente.

    R1: el frontend pinta lo que está aquí y no tiene ningún número propio. Si el catálogo
    cambiara, cambiaría esta respuesta y el panel, no el panel y el catálogo.
    """

    tiers: list[VolumeTierResponse]
    minimum_credits: int = Field(ge=1)
    maximum_credits: int = Field(ge=1)
    #: Paridad base sin descuento, para el cálculo del ahorro.
    list_usd_per_credit: Decimal = Field(gt=0)


class CreditLedgerEntryResponse(BaseModel):
    """Asiento del ledger expuesto al panel de facturación."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    amount_delta: Decimal
    balance_after: Decimal
    reason: str
    reference_id: str | None
    created_at: str


class CreditPackResponse(BaseModel):
    """Un paquete comprable del catálogo comercial.

    `usd_per_credit` lo calcula el servidor para que el panel pueda mostrar el precio
    unitario sin dividir en el cliente, donde un redondeo distinto por navegador daría dos
    cifras para el mismo producto.
    """

    credits: int = Field(gt=0)
    amount_usd: Decimal = Field(gt=0)
    usd_per_credit: Decimal = Field(gt=0)


class BillingSummaryResponse(BaseModel):
    """Resumen de facturacion del tenant, para las tarjetas KPI del panel.

    ## Por qué el importe en dólares es exacto y no una estimación

    El catálogo está a la paridad declarada en `core.config`: `credits_per_usd = 1.00`, y
    la escalera de descuento la parte de ahí hacia abajo, nunca hacia arriba. El precio de
    una cantidad de créditos lo decide `price_for_credits`, que devuelve **un** número por
    cantidad. Por eso el equivalente en dólares de un saldo es una cifra que el cliente
    puede comprobar en lugar de una estimación al mejor precio.

    El descuento por volumen se reintrodujo en esta fase, y reintroducirlo bien dependía de
    dos cosas que el diseño viejo no tenía:

    1. **Un precio por cantidad.** De nada sirve quitar «$0,038 en el pack pequeño» si al
       reintroducir el descuento «200 créditos» vuelve a no tener precio único. `price_for_credits`
       es weakly monótona, así que siempre hay un precio.
    2. **Que comprar más no salga más barato.** La escalera aplicada sola lo hace —251
       créditos cuestan menos que 250— y eso es un agujero. Ver la nota larga de
       `price_for_credits`.
    """

    credit_balance: Decimal
    credit_balance_usd: Decimal
    #: La paridad usada, para que el panel no la vuelva a derivar por su cuenta.
    credits_per_usd: Decimal = Field(gt=0)
    spent_this_month: Decimal = Field(ge=0)
    purchased_this_month: Decimal = Field(ge=0)
    spent_this_month_usd: Decimal = Field(ge=0)
    period_start: datetime
    packs: list[CreditPackResponse]
    #: Límites del pack a medida, en créditos.
    custom_minimum: int = Field(ge=1)
    custom_maximum: int = Field(ge=1)
    #: La escalera de descuento completa, para el slider.
    volume: VolumePricingResponse
    #: La suscripción Pro, para el botón de suscripción.
    subscription: SubscriptionOfferResponse
