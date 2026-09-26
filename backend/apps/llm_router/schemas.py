"""Esquemas de la consola de modelos de lenguaje."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.apps.llm_router.models import LLMUseCaseEnum


class LLMModelCreate(BaseModel):
    """Alta de un modelo de OpenRouter en el catálogo."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model_id: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._:-]*$",
        description="Identificador canónico de OpenRouter, con el prefijo de proveedor.",
    )
    display_name: str = Field(min_length=1, max_length=128)
    base_cost_input_m: Decimal = Field(ge=0, max_digits=18, decimal_places=8)
    base_cost_output_m: Decimal = Field(ge=0, max_digits=18, decimal_places=8)
    markup_pct: Decimal = Field(ge=0, max_digits=9, decimal_places=4)
    priority_order: int = Field(ge=1, le=100)
    is_active: bool = True
    use_case: LLMUseCaseEnum = LLMUseCaseEnum.ALL


class LLMModelUpdate(BaseModel):
    """Margen, prioridad y estado de un modelo. El `model_id` no es mutable."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    base_cost_input_m: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=8
    )
    base_cost_output_m: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=8
    )
    markup_pct: Decimal | None = Field(
        default=None, ge=0, max_digits=9, decimal_places=4
    )
    priority_order: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None
    use_case: LLMUseCaseEnum | None = None

    @field_validator("is_active", "priority_order", "use_case", "display_name")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        """Un `null` explícito sobre un campo opcional es un error de cliente."""

        if value is None:
            raise ValueError("Envía solo los campos que quieres modificar")
        return value


class LLMUsageMetrics(BaseModel):
    """Consumo agregado de un modelo desde que existe el catálogo."""

    runs: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    base_cost_usd: Decimal = Field(ge=0)
    net_profit_usd: Decimal


class LLMModelResponse(BaseModel):
    """Fila del catálogo con sus métricas de consumo."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    model_id: str
    display_name: str
    base_cost_input_m: Decimal
    base_cost_output_m: Decimal
    markup_pct: Decimal
    priority_order: int
    is_active: bool
    use_case: LLMUseCaseEnum
    created_at: datetime
    updated_at: datetime
    usage: LLMUsageMetrics


class LLMModelPage(BaseModel):
    """Página del catálogo de modelos."""

    items: list[LLMModelResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
