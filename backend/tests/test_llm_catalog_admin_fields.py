"""Campos configurables de la consola LLM."""

from decimal import Decimal

from backend.apps.llm_router.schemas import LLMModelCreate, LLMModelUpdate


def test_superadmin_puede_declarar_proveedor_cache_y_limites() -> None:
    modelo = LLMModelCreate.model_validate(
        {
            "model_id": "z-ai/glm-5.3",
            "display_name": "GLM",
            "base_cost_input_m": "0.4",
            "base_cost_output_m": "1.6",
            "cached_input_cost_m": "0.1",
            "provider": "OpenRouter",
            "context_limit_tokens": 128000,
            "output_limit_tokens": 8192,
            "markup_pct": "200",
            "priority_order": 1,
            "is_default": True,
            "use_case": "QUICK_SCAN",
        }
    )
    assert modelo.cached_input_cost_m == Decimal("0.1")
    assert modelo.provider == "OpenRouter"
    assert modelo.context_limit_tokens == 128000
    assert modelo.output_limit_tokens == 8192
    assert modelo.is_default is True

    cambio = LLMModelUpdate.model_validate(
        {"cached_input_cost_m": "0", "provider": "Otro", "is_default": False}
    )
    assert cambio.cached_input_cost_m == 0
    assert cambio.is_default is False
