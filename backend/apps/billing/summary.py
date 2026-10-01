"""Resumen de facturación para el panel del cliente.

## Por qué los packs viajan desde el servidor

El catálogo comercial está en `billing.schemas.CREDIT_PACKS` y lo usa
`POST /api/v1/billing/checkout-session` para validar la compra. Si el panel llevara su
propia lista de packs, habría dos catálogos: uno que el servidor acepta y otro que el
panel ofrece, y el síntoma sería un botón «comprar» que devuelve `422` para un pack que el
propio panel enseñó. El panel pinta lo que le mandan.

## Por qué el equivalente en dólares ya no necesita el "mejor precio"

La primera versión de este módulo dividía el saldo por una paridad única sacada del pack
más barato, y daba 1000 créditos = **$26.315,79**: un número que no correspondía a nada
comprable. La causa era que el catálogo tenía descuento por volumen —$0,038 por crédito en
el pack pequeño y $0,0266 en el grande—, y en medio de un descuento **no existe** un
precio por crédito: "100 créditos" no tenía un precio, tenía un rango.

El catálogo ahora está a la paridad declarada en `core.config` (`credits_per_usd = 1.00`),
que es lo que hace el resto de la plataforma coherente, y sin descuento el precio de
cualquier cantidad es su cantidad. Por eso este módulo ya no busca el "mejor precio
unitario": usa `settings.credits_per_usd`, que es la **única** declaración de la paridad
en todo el backend.

## Qué cambió al reintroducir el descuento por volumen

Se reintrodujo en la fase 5, y reintroducirlo bien dependía de dos cosas que el diseño
anterior no tenía. Las dos están resueltas en `billing.schemas`:

1. **Vuelve a haber un precio por cantidad.** `price_for_credits` devuelve **un** número
   para cualquier cantidad, siempre. Sin eso, «1.000 créditos» no tendría precio y este
   módulo volvería a necesitar el «mejor precio unitario» y sus estimaciones.
2. **Comprar más nunca sale más barato.** La escalera está definida sobre el **gasto**, no
   sobre los créditos, y con cota superior por tramo. Es la única forma de tener un
   descuento por volumen sin que la frontera del umbral sea un regalo.

Consecuencia para este módulo: el saldo en dólares sigue siendo un número exacto y
comprobable —1.000 créditos son 1.000 dólares—, porque el descuento aplica a **compras**,
no al saldo que ya se tiene. Un cliente con 1.000 créditos ya comprados no los "revende"
con descuento; los siguientes créditos que compre sí. Esa distinción es la que hace que el
saldo sea un hecho y el descuento una condición de la siguiente compra.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.catalogo import catalogo_vigente
from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.pricing import credits_per_usd
from backend.apps.billing.schemas import (
    CUSTOM_CREDITS_MAXIMUM,
    CUSTOM_CREDITS_MINIMUM,
    PRO_SUBSCRIPTION_PLAN,
    CreditPackResponse,
    SubscriptionOfferResponse,
    VolumePricingResponse,
    VolumeTierResponse,
    credits_for_spend,
    price_for_credits,
    subscription_price_usd,
)
from backend.apps.organizations.models import Organization


def credits_to_usd(credits: Decimal | int) -> Decimal:
    """Equivalente en dólares de una cantidad de créditos.

    Se redondea **aquí** y no en el cliente. Un saldo de 250 créditos son $250,00, y
    redondear en el navegador con `toFixed` depende de la coma decimal del sistema y da
    resultados distintos según el navegador del cliente.
    """

    unitario = credits_per_usd()
    return (Decimal(credits) * unitario).quantize(Decimal("0.01"))


async def credit_activity(
    session: AsyncSession, organization_id: uuid.UUID, now: datetime
) -> tuple[Decimal, Decimal, Decimal]:
    """Saldo, consumido este mes y créditos comprados este mes.

    ## Por qué el saldo se lee de la columna y no sumando el ledger

    El ledger es la fuente de verdad histórica, pero `credit_balance` es la caché que
    `apply_credit_delta` mantiene **en la misma transacción** que escribe el asiento. Leer
    la caché no puede estar desincronizada del asiento, y recorrer el ledger entero para
    sumar es trabajo que crece con cada compra del cliente.

    Los dos agregados del mes sí se calculan con `SUM`, recortados en la consulta. Recortarlos
    en el cliente obligaría a traer los asientos de todo el histórico para descartar los de
    meses anteriores, que es justo lo que se quiere evitar.
    """

    desde_mes = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    saldos = (
        await session.execute(
            select(Organization.credit_balance).where(Organization.id == organization_id)
        )
    ).scalar_one()

    consumidos = await session.execute(
        select(func.coalesce(func.sum(-CreditLedger.amount_delta), 0)).where(
            CreditLedger.organization_id == organization_id,
            CreditLedger.reason == LedgerReasonEnum.SCAN_CONSUMPTION,
            CreditLedger.created_at >= desde_mes,
        )
    )
    comprados = await session.execute(
        select(func.coalesce(func.sum(CreditLedger.amount_delta), 0)).where(
            CreditLedger.organization_id == organization_id,
            CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE,
            CreditLedger.created_at >= desde_mes,
        )
    )
    return (
        Decimal(saldos),
        Decimal(str(consumidos.scalar_one())),
        Decimal(str(comprados.scalar_one())),
    )


def available_packs() -> list[CreditPackResponse]:
    """El catálogo comercial, listo para pintar.

    Se ordena de menor a mayor porque es como se lee una lista de precios: primero el
    compromiso que cuesta poco y después el que conviene para un volumen alto. El orden del
    diccionario de Python es el de inserción, y ese orden no está garantizado por la
    especificación, así que se ordena explícitamente en vez de confiar en él.
    """

    return [
        CreditPackResponse(
            credits=credits,
            amount_usd=amount,
            usd_per_credit=(amount / Decimal(credits)).quantize(Decimal("0.0001")),
        )
        for credits, amount in catalogo_vigente().packs
    ]


def volume_pricing() -> VolumePricingResponse:
    """La escalera de descuento completa, con los dos extremos de cada tramo.

    El panel la pinta como una barra de progreso. Los dos extremos son necesarios: con solo
    el superior, todos los tramos intermedios saldrían del mismo ancho y la barra no
    reflejaría la escala real.

    El `maximum_credits` del último tramo es el máximo del catálogo, no un número redondo.
    Va derivado, no escrito, porque si el tope de gasto subiera y este `13.333` se quedara
    atrás, el tramo final se mostraría más corto de lo que es y el último crédito del
    catálogo quedaría fuera de la barra sin explicación.
    """

    tramos: list[VolumeTierResponse] = []
    catalogo = catalogo_vigente()
    for indice, (minimo, descuento) in enumerate(catalogo.tramos):
        siguiente = (
            catalogo.tramos[indice + 1][0]
            if indice + 1 < len(catalogo.tramos)
            else None
        )
        # El máximo del tramo, en créditos, es lo que da el mínimo del siguiente spending
        # convertido con el tipo de cambio de **este** tramo. La conversión inversa usa
        # `credits_for_spend` con un céntimo menos, para que el máximo sea el último crédito
        # que realmente cae dentro del tramo y no el primero del siguiente.
        maximo = (
            credits_for_spend(siguiente - Decimal("0.01")) if siguiente else CUSTOM_CREDITS_MAXIMUM
        )
        tramos.append(
            VolumeTierResponse(
                minimum_credits=credits_for_spend(minimo),
                maximum_credits=maximo,
                discount=descuento,
                usd_per_credit=catalogo.usd_por_credito_con_descuento(descuento),
            )
        )
    return VolumePricingResponse(
        tiers=tramos,
        minimum_credits=CUSTOM_CREDITS_MINIMUM,
        maximum_credits=CUSTOM_CREDITS_MAXIMUM,
        list_usd_per_credit=catalogo.usd_por_credito(),
    )


def subscription_offer(organization: Organization) -> SubscriptionOfferResponse:
    """La oferta de suscripción Pro para este workspace.

    `is_current_plan` se resuelve contra el plan del tenant, no contra un `plan_tier` que
    mande el panel: es el servidor quien sabe en qué plan está cada workspace, y que el
    botón aparezca activo o no es una consecuencia de ese dato.
    """

    return SubscriptionOfferResponse(
        plan_tier=PRO_SUBSCRIPTION_PLAN,
        monthly_usd=subscription_price_usd(),
        is_current_plan=organization.plan_tier == PRO_SUBSCRIPTION_PLAN,
    )


def is_commercializable(credits: int) -> bool:
    """Si una cantidad es vendible: dentro de los límites del catálogo.

    Lo consulta el panel para habilitar o desactivar el botón de compra sin que el usuario
    descubra la regla al pulsar y reciba un `422`.

    La cota superior es `CUSTOM_CREDITS_MAXIMUM` y no un número inventado. Con el slider
    acotado a $10.000, 13.333 créditos es lo máximo comprable, y admitir un millón sería
    abrir una puerta que el catálogo no tiene: el endpoint aceptaría la compra y no habría
    tramo que la explique.
    """

    return CUSTOM_CREDITS_MINIMUM <= credits <= CUSTOM_CREDITS_MAXIMUM


__all__ = [
    "available_packs",
    "credit_activity",
    "credits_to_usd",
    "is_commercializable",
    "price_for_credits",
    "subscription_offer",
    "volume_pricing",
]
