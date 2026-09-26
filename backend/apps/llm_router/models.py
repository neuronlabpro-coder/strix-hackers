"""Modelos del catálogo de LLMs y del registro de consumo."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

# Numérico exacto para dinero. `Float` acumula error de coma flotante y un
# ledger de créditos con derivas de centavo es un ledger que no cuadra.
_COST_PRECISION = Numeric(18, 8)
_PCT_PRECISION = Numeric(9, 4)


class LLMUseCaseEnum(StrEnum):
    """Casos de uso para los que se puede enrutar un modelo concreto."""

    ALL = "ALL"
    QUICK_SCAN = "QUICK_SCAN"
    DEEP_PENTEST = "DEEP_PENTEST"
    AUTOFIX = "AUTOFIX"


class LLMModelConfig(Base):
    """Modelo de OpenRouter con su coste base y su recargo comercial.

    `markup_pct` es un **recargo sobre el coste base**, no un margen sobre el
    precio de venta: 150 significa que el cliente paga 2,5 veces el coste. Con la
    definición habitual de margen, un 150 % sería imposible porque el precio
    tendría que ser negativo. El nombre evita la ambigüedad.

    `priority_order` construye la cadena de resolución: 1 es el primario del caso
    de uso y los siguientes son fallbacks. El motor solo enruta a modelos activos,
    de modo que apagar un modelo en caliente lo retira de la cadena sin borrar su
    histórico de consumo.
    """

    __tablename__ = "llm_model_configs"
    __table_args__ = (
        UniqueConstraint("model_id", name="uq_llm_model_configs_model_id"),
        CheckConstraint("priority_order >= 1", name="ck_llm_model_configs_priority_positive"),
        CheckConstraint(
            "base_cost_input_m >= 0 AND base_cost_output_m >= 0",
            name="ck_llm_model_configs_costs_nonnegative",
        ),
        CheckConstraint("markup_pct >= 0", name="ck_llm_model_configs_markup_nonnegative"),
        CheckConstraint("display_name <> ''", name="ck_llm_model_configs_display_name"),
        Index("ix_llm_model_configs_routing", "use_case", "is_active", "priority_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_cost_input_m: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    base_cost_output_m: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    markup_pct: Mapped[Decimal] = mapped_column(_PCT_PRECISION, nullable=False)
    priority_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    use_case: Mapped[LLMUseCaseEnum] = mapped_column(
        SQLEnum(LLMUseCaseEnum, name="llm_use_case_enum"),
        nullable=False,
        default=LLMUseCaseEnum.ALL,
        server_default=LLMUseCaseEnum.ALL.name,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class LLMUsageEvent(Base):
    """Consumo real de un modelo, necesario para auditar el margen declarado.

    Sin esta tabla, el margen del catálogo sería una intención comercial sin
    respaldo: nadie podría responder cuánto cuesta de verdad operar la
    plataforma ni qué modelo está quemando presupuesto.
    """

    __tablename__ = "llm_usage_events"
    __table_args__ = (
        CheckConstraint("prompt_tokens >= 0", name="ck_llm_usage_prompt_tokens_nonnegative"),
        CheckConstraint(
            "completion_tokens >= 0", name="ck_llm_usage_completion_tokens_nonnegative"
        ),
        CheckConstraint("base_cost_usd >= 0", name="ck_llm_usage_base_cost_nonnegative"),
        Index("ix_llm_usage_model_created", "model_config_id", "created_at"),
        Index("ix_llm_usage_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pentest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    use_case: Mapped[LLMUseCaseEnum] = mapped_column(
        SQLEnum(LLMUseCaseEnum, name="llm_use_case_enum"), nullable=False
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    base_cost_usd: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    net_profit_usd: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
