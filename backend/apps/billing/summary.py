"""Resumen de facturación para el panel del cliente.

## Por qué los packs viajan desde el servidor

El catálogo comercial está en `billing.schemas.CREDIT_PACKS` y lo usa
`POST /api/v1/billing/checkout-session` para validar la compra. Si el panel llevara su
propia lista de packs, habría dos catálogos: uno que el servidor acepta y otro que el
panel ofrece, y el síntoma sería un botón «comprar» que devuelve `422` para un pack que el
propio panel enseñó. El panel pinta lo que le mandan.

## Por qué el equivalente en dólares usa el mejor precio unitario

El catálogo **descuento por volumen**:

    500 créditos  → $19,00  →  $0,0380 por crédito
    1500 créditos → $49,00  →  $0,0327 por crédito
    5000 créditos → $149,00 →  $0,0298 por crédito
    15000 créditos→ $399,00 →  $0,0266 por crédito

No hay una única paridad crédito-dólar, y la primera versión de este módulo fingía que sí la
había: dividía por el precio del pack pequeño y daba 1000 créditos = **$26.315,79**, una
cifra que no corresponde a nada que se pueda comprar. Convertir un saldo con una paridad
lineal sobre un catálogo con descuento no da un precio aproximado: da un precio **falso**,
y además acompaña a un saldo real, así que el cliente lo lee como lo que tendría que pagar
por lo que ya compró.

Lo que sí es cierto, y es lo que se devuelve, es lo que **costaría comprar** esa cantidad
de créditos hoy al precio unitario más barato del catálogo. Para 1000 créditos, $26,60: un
número que el cliente puede comprobar comparándolo con los packs de la misma pantalla. Es
una estimación y la interfaz la etiqueta como tal, no como el valor de su saldo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.schemas import CREDIT_PACKS, CreditPackResponse
from backend.apps.organizations.models import Organization

#: El precio unitario más barato del catálogo, y el pack que lo ofrece.
#:
#: Se calcula **a partir de `CREDIT_PACKS`** y no se escribe a mano. Fijarlo en una
#: constante obligaría a recordarlo cambiarlo en dos sitios cada vez que se toca un precio,
#: y el que se olvida es el que hace que la pantalla dé una cifra distinta de los packs que
#: tiene al lado.
BEST_UNIT_PRICE: tuple[Decimal, int] = min(
    (amount / Decimal(credits), credits) for credits, amount in CREDIT_PACKS.items()
)


def cheapest_buy_price(credits: Decimal) -> Decimal:
    """Lo que costaría comprar `credits` créditos al mejor precio unitario del catálogo.

    Redondeado a dos decimales porque es dinero que se muestra. Para una cantidad pequeña
    el redondeo puede dejar la cifra en $0,00 —250 créditos a $0,0266 son $6,65, pero 20
    créditos son $0,53—, y eso es preferible a mostrar cuatro decimales que el cliente
    interpretaría como un precio exacto cuando es una estimación.
    """

    unit_price, _pack = BEST_UNIT_PRICE
    return (Decimal(credits) * unit_price).quantize(Decimal("0.01"))


def best_unit_price() -> Decimal:
    """Precio unitario más barato del catálogo, redondeado a cuatro decimales.

    Viaja en la respuesta para que el panel pueda mostrar de dónde sale la estimación sin
    tener que deducirlo de los packs.
    """

    return BEST_UNIT_PRICE[0].quantize(Decimal("0.0001"))


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
        for credits, amount in sorted(CREDIT_PACKS.items())
    ]
