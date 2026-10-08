"""Productos de pentest y acuerdos individuales, separados del ledger y del coste LLM."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

_USD = Numeric(18, 2)
_CREDITS = Numeric(18, 4)


class PentestProduct(Base):
    """Oferta comercial configurable. No mueve créditos por sí misma."""

    __tablename__ = "pentest_products"
    __table_args__ = (
        CheckConstraint(
            "price_min_usd IS NULL OR price_min_usd >= 0", name="ck_pentest_products_min_price"
        ),
        CheckConstraint(
            "price_max_usd IS NULL OR price_max_usd >= price_min_usd",
            name="ck_pentest_products_max_price",
        ),
        CheckConstraint(
            "credits_required IS NULL OR credits_required > 0", name="ck_pentest_products_credits"
        ),
        CheckConstraint(
            "max_budget_usd IS NULL OR max_budget_usd > 0", name="ck_pentest_products_budget"
        ),
        CheckConstraint("max_turns IS NULL OR max_turns > 0", name="ck_pentest_products_turns"),
        CheckConstraint(
            "scan_mode IS NULL OR scan_mode IN ('QUICK', 'STANDARD', 'DEEP')",
            name="ck_pentest_products_scan_mode",
        ),
        Index("ix_pentest_products_scan_mode", "scan_mode", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scan_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name_es: Mapped[str] = mapped_column(String(128), nullable=False)
    name_en: Mapped[str] = mapped_column(String(128), nullable=False)
    description_es: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    description_en: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    price_label_es: Mapped[str] = mapped_column(String(128), nullable=False)
    price_label_en: Mapped[str] = mapped_column(String(128), nullable=False)
    price_min_usd: Mapped[Decimal | None] = mapped_column(_USD, nullable=True)
    price_max_usd: Mapped[Decimal | None] = mapped_column(_USD, nullable=True)
    credits_required: Mapped[Decimal | None] = mapped_column(_CREDITS, nullable=True)
    max_budget_usd: Mapped[Decimal | None] = mapped_column(_USD, nullable=True)
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    features: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    limits: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EnterpriseAgreement(Base):
    """Contrato individual por organización; los flags son overrides explícitos."""

    __tablename__ = "enterprise_agreements"
    __table_args__ = (
        CheckConstraint(
            "price_monthly_usd IS NULL OR price_monthly_usd >= 0",
            name="ck_enterprise_agreements_price",
        ),
        CheckConstraint("seats IS NULL OR seats > 0", name="ck_enterprise_agreements_seats"),
        CheckConstraint("included_credits >= 0", name="ck_enterprise_agreements_credits"),
        CheckConstraint(
            "discount_pct >= 0 AND discount_pct <= 100", name="ck_enterprise_agreements_discount"
        ),
        CheckConstraint(
            "max_budget_usd IS NULL OR max_budget_usd > 0", name="ck_enterprise_agreements_budget"
        ),
        CheckConstraint(
            "max_turns IS NULL OR max_turns > 0", name="ck_enterprise_agreements_turns"
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="ck_enterprise_agreements_dates"
        ),
        Index("ix_enterprise_agreements_org_dates", "organization_id", "valid_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    price_monthly_usd: Mapped[Decimal | None] = mapped_column(_USD, nullable=True)
    seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    included_credits: Mapped[Decimal] = mapped_column(
        _CREDITS, nullable=False, default=Decimal(0), server_default="0"
    )
    discount_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    max_budget_usd: Mapped[Decimal | None] = mapped_column(_USD, nullable=True)
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    features: Mapped[dict[str, bool]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    limits: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    special_operations: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlanFeatureEntitlement(Base):
    """Entitlement de plan; ausencia de fila significa default deshabilitado."""

    __tablename__ = "plan_feature_entitlements"
    __table_args__ = (
        Index("ix_plan_feature_entitlements_tier_feature", "plan_tier", "feature_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
