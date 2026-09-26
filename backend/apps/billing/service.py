"""Servicio de saldo de créditos con operaciones atómicas y serializables."""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.organizations.models import Organization

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


class InsufficientCreditsError(RuntimeError):
    """El saldo no cubre el movimiento solicitado.

    Se propaga como excepción de dominio, no como `HTTPException`, para que la
    capa HTTP decida el código (402 en pentests, 409 en otros contextos) sin que
    el servicio de saldo dependa de FastAPI.
    """

    def __init__(self, required: Decimal, available: Decimal) -> None:
        self.required = required
        self.available = available
        super().__init__(
            f"Saldo insuficiente: se requieren {required} créditos y hay {available}"
        )


async def credit_balance_of(
    session: AsyncSession, organization_id: UUID
) -> Decimal:
    """Lee el saldo actual sin tomar el bloqueo de escritura."""

    result = await session.execute(
        select(func.coalesce(func.sum(CreditLedger.amount_delta), 0)).where(
            CreditLedger.organization_id == organization_id
        )
    )
    return Decimal(str(result.scalar_one()))


async def apply_credit_delta(
    *,
    session: AsyncSession,
    organization_id: UUID,
    amount: Decimal,
    reason: LedgerReasonEnum,
    reference_id: str | None = None,
    actor_user_id: UUID | None = None,
) -> CreditLedger:
    """Aplica un movimiento al ledger y actualiza el saldo, o no hace nada.

    La atomicidad se apoya en `SELECT ... FOR UPDATE` sobre la organización: dos
    pentests concurrentes del mismo tenant se serializan en la fila de la
    organización, de modo que ninguno puede leer un saldo ya gastado por el otro.
    Sin ese bloqueo, dos deducciones que leen 10 y Gastan 8 cada una dejarían el
    saldo en -6.

    Si el movimiento dejaría el saldo negativo, no se escribe nada: se lanza
    `InsufficientCreditsError` y la transacción del llamante sigue intacta. Un
    rechazo de pago no debe dejar un asiento a medias.
    """

    if amount == ZERO:
        raise ValueError("Un movimiento de cero créditos no es un movimiento")

    organization_result = await session.execute(
        select(Organization)
        .where(Organization.id == organization_id)
        .with_for_update()
    )
    organization = organization_result.scalar_one_or_none()
    if organization is None:
        raise LookupError(f"La organización {organization_id} no existe")

    current = Decimal(str(organization.credit_balance))
    resulting = current + amount
    if resulting < ZERO:
        raise InsufficientCreditsError(required=abs(amount), available=current)

    organization.credit_balance = resulting
    entry = CreditLedger(
        organization_id=organization_id,
        amount_delta=amount,
        balance_after=resulting,
        reason=reason,
        reference_id=reference_id,
    )
    session.add(entry)
    await session.flush()
    logger.info(
        "Movimiento de ledger organization=%s delta=%s saldo=%s motivo=%s referencia=%s",
        organization_id,
        amount,
        resulting,
        reason.value,
        reference_id,
    )
    del actor_user_id  # El actor vive en `audit_log`; aquí solo importa el saldo.
    return entry


async def ensure_sufficient_credits(
    *,
    session: AsyncSession,
    organization_id: UUID,
    required: Decimal,
) -> Decimal:
    """Comprueba saldo suficiente sin escribir. Devuelve el saldo disponible."""

    result = await session.execute(
        select(Organization.credit_balance).where(Organization.id == organization_id)
    )
    available = Decimal(str(result.scalar_one_or_none() or 0))
    if available < required:
        raise InsufficientCreditsError(required=required, available=available)
    return available
