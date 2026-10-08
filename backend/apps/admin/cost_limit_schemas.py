"""Contrato de políticas de coste administradas desde SuperAdmin."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.apps.llm_router.models import NivelLimiteCosteEnum, OperacionCosteEnum
from backend.apps.organizations.models import PlanTierEnum


class CostLimitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: NivelLimiteCosteEnum
    organization_id: UUID | None = None
    operation: OperacionCosteEnum | None = None
    plan_tier: PlanTierEnum | None = None
    max_budget_usd: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    max_turns: int | None = Field(default=None, gt=0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def alcance_coherente(self) -> CostLimitCreate:
        if self.max_budget_usd is None and self.max_turns is None:
            raise ValueError("Declara presupuesto o turnos")
        valores = {
            NivelLimiteCosteEnum.ORGANIZACION: self.organization_id,
            NivelLimiteCosteEnum.OPERACION: self.operation,
            NivelLimiteCosteEnum.PLAN: self.plan_tier,
        }
        if any((valor is not None) != (nivel is self.scope) for nivel, valor in valores.items()):
            raise ValueError("El alcance no coincide con la clave de la política")
        if self.valid_from is not None and self.valid_from.tzinfo is None:
            raise ValueError("valid_from debe tener zona horaria")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None:
                raise ValueError("valid_until debe tener zona horaria")
            if self.valid_from is not None and self.valid_until <= self.valid_from:
                raise ValueError("valid_until debe ser posterior a valid_from")
        return self


class CostLimitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: NivelLimiteCosteEnum
    organization_id: UUID | None
    operation: OperacionCosteEnum | None
    plan_tier: PlanTierEnum | None
    max_budget_usd: Decimal | None
    max_turns: int | None
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime
