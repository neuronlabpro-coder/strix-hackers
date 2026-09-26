"""Ledger de créditos append-only e idempotencia de eventos de Stripe."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

# El dinero no se guarda en coma flotante. Un ledger con derivas de centavo
# acaba sin cuadrar y sin ninguna forma de explicar la diferencia.
_CREDIT_PRECISION = Numeric(18, 4)


class LedgerReasonEnum(StrEnum):
    """Causas de cada movimiento del ledger. Solo se añaden, nunca se reescriben."""

    SCAN_CONSUMPTION = "SCAN_CONSUMPTION"
    STRIPE_PURCHASE = "STRIPE_PURCHASE"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"
    SIGNUP_BONUS = "SIGNUP_BONUS"


class CreditLedger(Base):
    """Asiento inmutable del balance de créditos de una organización.

    R4: la tabla es *append-only*. `balance_after` es una fotografía del saldo
    en el momento del asiento, no un saldo derivado: cada fila es verificable de
    forma independiente y la suma de `amount_delta` reconstruye el histórico sin
    recalcular nada. El saldo denormalizado de `organizations.credit_balance` es
    una caché que este servicio mantiene bajo bloqueo pesimista.
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (
        CheckConstraint("amount_delta <> 0", name="ck_credit_ledger_delta_nonzero"),
        CheckConstraint("balance_after >= 0", name="ck_credit_ledger_balance_nonnegative"),
        CheckConstraint(
            "reference_id IS NULL OR reference_id <> ''",
            name="ck_credit_ledger_reference_not_empty",
        ),
        Index("ix_credit_ledger_org_created", "organization_id", "created_at"),
        Index("ix_credit_ledger_reference", "organization_id", "reference_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT y no CASCADE: el rastro financiero sobrevive a su organización. Con
        # CASCADE, borrar un tenant dispara aquí un DELETE que el propio trigger
        # append-only bloquea, y el error resultante habla de un trigger de auditoría
        # en lugar de de la política de borrado. RESTRICT declara la intención en el
        # esquema y falla antes de tocar nada. El borrado de un tenant es lógico:
        # `organizations.deleted_at`.
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount_delta: Mapped[Decimal] = mapped_column(_CREDIT_PRECISION, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(_CREDIT_PRECISION, nullable=False)
    reason: Mapped[LedgerReasonEnum] = mapped_column(
        SQLEnum(LedgerReasonEnum, name="ledger_reason_enum"), nullable=False, index=True
    )
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StripeEvent(Base):
    """Evento de Stripe ya procesado, para idempotencia de entregas.

    Stripe reintenta un webhook hasta recibir un `2xx`, y puede entregar el mismo
    evento más de una vez aunque la primera entrega funcionara. La restricción de
    unicidad sobre `event_id` es la que hace la recarga idempotente: el segundo
    intento choca contra ella y se descarta.
    """

    __tablename__ = "stripe_events"
    __table_args__ = (
        Index("ix_stripe_events_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credits_granted: Mapped[Decimal | None] = mapped_column(_CREDIT_PRECISION, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
