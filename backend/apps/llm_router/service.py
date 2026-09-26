"""Servicio de la consola de modelos de lenguaje."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.llm_router.models import LLMModelConfig, LLMUsageEvent
from backend.apps.llm_router.schemas import LLMUsageMetrics


class DuplicateLLMModelError(RuntimeError):
    """El `model_id` ya existe en el catálogo."""


async def usage_metrics(
    session: AsyncSession, model_ids: list[UUID]
) -> dict[UUID, LLMUsageMetrics]:
    """Agrega el consumo por modelo. Un modelo sin uso devuelve ceros, no falta."""

    if not model_ids:
        return {}
    result = await session.execute(
        select(
            LLMUsageEvent.model_config_id,
            func.count(LLMUsageEvent.id),
            func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.base_cost_usd), 0),
            func.coalesce(func.sum(LLMUsageEvent.net_profit_usd), 0),
        )
        .where(LLMUsageEvent.model_config_id.in_(model_ids))
        .group_by(LLMUsageEvent.model_config_id)
    )
    return {
        model_id: LLMUsageMetrics(
            runs=int(runs),
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            base_cost_usd=Decimal(str(base_cost)),
            net_profit_usd=Decimal(str(profit)),
        )
        for model_id, runs, prompt_tokens, completion_tokens, base_cost, profit in result.all()
    }


def empty_usage() -> LLMUsageMetrics:
    return LLMUsageMetrics(
        runs=0,
        prompt_tokens=0,
        completion_tokens=0,
        base_cost_usd=Decimal("0"),
        net_profit_usd=Decimal("0"),
    )


async def create_model(
    session: AsyncSession,
    *,
    model_id: str,
    display_name: str,
    base_cost_input_m: Decimal,
    base_cost_output_m: Decimal,
    markup_pct: Decimal,
    priority_order: int,
    is_active: bool,
    use_case: object,
) -> LLMModelConfig:
    """Da de alta un modelo, rechazando duplicados con un error de dominio."""

    existing = await session.execute(
        select(LLMModelConfig.id).where(LLMModelConfig.model_id == model_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise DuplicateLLMModelError(f"El modelo {model_id} ya está en el catálogo")

    model = LLMModelConfig(
        model_id=model_id,
        display_name=display_name,
        base_cost_input_m=base_cost_input_m,
        base_cost_output_m=base_cost_output_m,
        markup_pct=markup_pct,
        priority_order=priority_order,
        is_active=is_active,
        use_case=use_case,  # pyright: ignore[reportArgumentType]
    )
    session.add(model)
    await session.flush()
    return model
