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

Consecuencia útil: el saldo en dólares de un tenant es ahora una cifra que el cliente
puede comprobar —1000 créditos, 1000 dólares— y no una estimación. La interfaz sigue
etiquetándola como equivalente, pero ya no necesita un `≈`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.schemas import (
    CREDIT_PACKS,
    CUSTOM_CREDITS_MINIMUM,
    CreditPackResponse,
    price_for_credits,
)
from backend.apps.organizations.models import Organization
from backend.core.config import settings


def credits_to_usd(credits: Decimal | int) -> Decimal:
    """Equivalente en dólares de una cantidad de créditos.

    Se redondea **aquí** y no en el cliente. Un saldo de 250 créditos son $250,00, y
    redondear en el navegador con `toFixed` depende de la coma decimal del sistema y da
    resultados distintos según el navegador del cliente.
    """

    unitario = settings.credits_per_usd
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
        for credits, amount in sorted(CREDIT_PACKS.items())
    ]


def custom_purchase_bounds() -> dict[str, int]:
    """Límites del pack a medida, para que el panel configure su campo.

    Viajan como enteros porque son **cantidades de créditos**, no importes. El panel pinta
    el mínimo como créditos para que el número que el usuario teclea sea el mismo que se
    cobra, y no una conversión que tendría que hacer el cliente mentalmente.
    """

    return {"minimum": CUSTOM_CREDITS_MINIMUM, "maximum": 1_000_000}


def is_commercializable(credits: int) -> bool:
    """Si una cantidad es vendible: pack del catálogo o cantidad a medida válida.

    Lo consulta el panel para habilitar o desactivar el botón de compra sin que el usuario
    descubra la regla al pulsar y reciba un `422`.
    """

    return credits in CREDIT_PACKS or credits >= CUSTOM_CREDITS_MINIMUM


__all__ = [
    "available_packs",
    "credit_activity",
    "credits_to_usd",
    "custom_purchase_bounds",
    "is_commercializable",
    "price_for_credits",
]
