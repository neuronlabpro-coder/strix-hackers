"""El coste del proveedor se conserva separado del cargo al cliente."""

from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.workers.runner.telemetry import LlmUsageTelemetry


@pytest.mark.asyncio
async def test_el_evento_conserva_cache_coste_real_y_estimado() -> None:
    model = LLMModelConfig(
        id=uuid4(),
        model_id="openrouter/z-ai/glm-5.3",
        display_name="GLM",
        use_case=LLMUseCaseEnum.QUICK_SCAN,
        base_cost_input_m=Decimal("0.4"),
        base_cost_output_m=Decimal("1.6"),
        cached_input_cost_m=Decimal("0.1"),
        provider="OpenRouter",
        markup_pct=Decimal("200"),
        priority_order=1,
        is_active=True,
    )
    session = AsyncMock()
    session.add = Mock()
    with patch("backend.workers.runner.telemetry.apply_credit_delta", new_callable=AsyncMock):
        await LlmUsageTelemetry(
            model=model,
            use_case=LLMUseCaseEnum.QUICK_SCAN,
            prompt_tokens=16_819_076,
            completion_tokens=46_417,
            cached_tokens=16_443_264,
            provider_cost_usd=Decimal("1.84830111"),
            duration_seconds=Decimal("1348.415"),
        ).charge(
            session=session,
            organization_id=uuid4(),
            run_id=uuid4(),
            credits_per_usd=Decimal("1"),
        )

    evento = session.add.call_args.args[0]
    assert evento.cached_tokens == 16_443_264
    assert evento.provider_cost_usd == Decimal("1.84830111")
    assert evento.estimated_provider_cost_usd is not None
    assert evento.estimated_provider_cost_usd < evento.base_cost_usd
    assert evento.provider == "OpenRouter"
    assert evento.duration_seconds == Decimal("1348.415")
    assert (
        evento.input_cost_usd + evento.cached_input_cost_usd + evento.output_cost_usd
        == evento.estimated_provider_cost_usd
    )


@pytest.mark.asyncio
async def test_coste_llm_interno_no_ajusta_creditos_del_pentest() -> None:
    model = LLMModelConfig(
        id=uuid4(),
        model_id="openrouter/z-ai/glm-5.3",
        display_name="GLM",
        use_case=LLMUseCaseEnum.QUICK_SCAN,
        base_cost_input_m=Decimal("0.4"),
        base_cost_output_m=Decimal("1.6"),
        provider=None,
        markup_pct=Decimal("200"),
        priority_order=1,
        is_active=True,
    )
    session = AsyncMock()
    session.add = Mock()
    with patch(
        "backend.workers.runner.telemetry.apply_credit_delta", new_callable=AsyncMock
    ) as mover_creditos:
        await LlmUsageTelemetry(
            model=model,
            use_case=LLMUseCaseEnum.QUICK_SCAN,
            prompt_tokens=1_000_000,
            completion_tokens=0,
            provider_cost_usd=Decimal("1.85"),
        ).charge(
            session=session,
            organization_id=uuid4(),
            run_id=uuid4(),
            credits_per_usd=Decimal("2"),
            reserved_credits=Decimal("10"),
            adjust_credits=False,
        )

    mover_creditos.assert_not_awaited()
    evento = session.add.call_args.args[0]
    assert evento.provider == "openrouter"
    assert evento.net_profit_usd == Decimal("3.15")
    assert evento.provider_cost_usd == Decimal("1.85")
