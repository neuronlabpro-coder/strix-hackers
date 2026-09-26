"""Telemetría de consumo de LLM y resolución del modelo para el runner.

El contenedor de Strix habla con OpenRouter por su cuenta, así que el worker no ve
los códigos HTTP del proveedor. El fallback, por tanto, no puede reaccionar a un
429 en el momento: reacciona a un fallo de la ejecución y reencola con el
siguiente modelo de la cadena. Es menos granular que un reintento dentro del
contenedor, pero es lo que la arquitectura permite sin exponer la credencial.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.llm_router.models import LLMModelConfig, LLMUsageEvent, LLMUseCaseEnum
from backend.apps.llm_router.pricing import ChargeBreakdown, compute_charge

logger = logging.getLogger(__name__)

_USAGE_KEYS = ("usage", "token_usage", "tokens")
_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "prompt")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens", "completion")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens consumidos por una ejecución, tal como los reporta el proveedor."""

    prompt_tokens: int
    completion_tokens: int


def select_runtime_models(models: Sequence[LLMModelConfig]) -> list[str]:
    """Devuelve la cadena de slugs a probar, en orden de prioridad.

    Se filtran los inactivos y se eliminan duplicados conservando el primero: un
    mismo slug en dos filas con prioridades distintas es un error de catálogo, y
    reintentarlo dos veces solo gastaría tiempo.
    """

    chain: list[str] = []
    for model in sorted(models, key=lambda item: (item.priority_order, item.model_id)):
        if not model.is_active or model.model_id in chain:
            continue
        chain.append(model.model_id)
    return chain


def _coerce_tokens(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _first_token_value(block: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """Primer valor de token utilizable entre los alias conocidos."""

    for key in keys:
        if key not in block:
            continue
        coerced = _coerce_tokens(block[key])
        if coerced is not None:
            return coerced
    return None


def _has_corrupt_token_value(block: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    """El bloque declara un token pero su valor no es utilizable.

    Un `prompt_tokens` negativo no es un dato válido: es un reporte corrupto. Aceptar
    la mitad buena del bloque significaría tarificar la mitad del consumo real, así
    que el bloque entero se descarta y el escaneo se queda sin cobrar de más.
    """

    return any(key in block and _coerce_tokens(block[key]) is None for key in keys)


def _parse_usage_block(block: object) -> TokenUsage | None:
    if not isinstance(block, Mapping):
        return None
    mapping = cast(Mapping[str, Any], block)
    if _has_corrupt_token_value(mapping, _PROMPT_KEYS) or _has_corrupt_token_value(
        mapping, _COMPLETION_KEYS
    ):
        logger.warning("Bloque de consumo de tokens corrupto; se descarta: %r", mapping)
        return None
    prompt = _first_token_value(mapping, _PROMPT_KEYS)
    completion = _first_token_value(mapping, _COMPLETION_KEYS)
    if prompt is None and completion is None:
        return None
    return TokenUsage(prompt_tokens=prompt or 0, completion_tokens=completion or 0)


def extract_token_usage(output_json: str) -> TokenUsage | None:
    """Extrae el consumo de tokens del reporte de Strix, si lo publica.

    Devuelve `None` —no cero— cuando el reporte no trae el dato. La distinción es
    deliberada: cero significa "el motor consumió tokens gratis" y `None` significa
    "no lo sabemos". Cobrar en función de `None` sería tarificar al aire.
    """

    try:
        report: object = json.loads(output_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    payload = cast(dict[str, Any], report)

    for key in _USAGE_KEYS:
        if key not in payload:
            continue
        block = payload[key]
        if isinstance(block, list):
            totals = TokenUsage(0, 0)
            found = False
            for item in cast(list[object], block):
                parsed = _parse_usage_block(item)
                if parsed is not None:
                    totals = TokenUsage(
                        totals.prompt_tokens + parsed.prompt_tokens,
                        totals.completion_tokens + parsed.completion_tokens,
                    )
                    found = True
            if found:
                return totals
            continue
        parsed = _parse_usage_block(block)
        if parsed is not None:
            return parsed
    return None


@dataclass(frozen=True, slots=True)
class LlmUsageTelemetry:
    """Consumo de un modelo en una ejecución, listo para tarificarse."""

    model: LLMModelConfig
    use_case: LLMUseCaseEnum
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def has_usage(self) -> bool:
        return self.prompt_tokens is not None or self.completion_tokens is not None

    def breakdown(self, *, credits_per_usd: Decimal) -> ChargeBreakdown | None:
        """Tarificación del consumo, o `None` si no hay datos de tokens."""

        if not self.has_usage:
            return None
        return compute_charge(
            base_cost_input_m=self.model.base_cost_input_m,
            base_cost_output_m=self.model.base_cost_output_m,
            markup_pct=self.model.markup_pct,
            prompt_tokens=self.prompt_tokens or 0,
            completion_tokens=self.completion_tokens or 0,
            credits_per_usd=credits_per_usd,
        )

    async def charge(
        self,
        *,
        session: AsyncSession,
        organization_id: UUID,
        run_id: UUID | None,
        credits_per_usd: Decimal,
        reserved_credits: Decimal | None = None,
        reference_suffix: str = "usage",
    ) -> ChargeBreakdown | None:
        """Cobra el consumo real y registra el evento de uso.

        `reserved_credits` es lo que el tenant ya pagó por adelantado al lanzar el
        escaneo. El movimiento es la **diferencia**: si lo real supera lo
        reservado se cobra el exceso, y si es menor se devuelve. Cargar el consumo
        completo dejaría pagando dos veces al cliente; no cobrar nada, regalar el
        margen.
        """

        breakdown = self.breakdown(credits_per_usd=credits_per_usd)
        if breakdown is None:
            logger.info(
                "El run %s no publicó consumo de tokens; no se cobra ni se registra",
                run_id,
            )
            return None
        delta = breakdown.client_cost_credits
        if reserved_credits is not None:
            delta = delta - reserved_credits
        reference = f"{run_id}:{reference_suffix}" if run_id is not None else None
        if delta != 0:
            # `apply_credit_delta` suma el importe al saldo, así que un cargo entra
            # con signo negativo. Invertir el signo aquí evita que cada llamador
            # tenga que acordarse, que es exactamente el tipo de detalle que acaba
            # regalando margen sin que nadie lo note.
            await apply_credit_delta(
                session=session,
                organization_id=organization_id,
                amount=-delta,
                reason=LedgerReasonEnum.ADMIN_ADJUSTMENT,
                reference_id=reference,
            )
        session.add(
            LLMUsageEvent(
                model_config_id=self.model.id,
                organization_id=organization_id,
                run_id=run_id,
                use_case=self.use_case,
                prompt_tokens=breakdown.prompt_tokens,
                completion_tokens=breakdown.completion_tokens,
                base_cost_usd=breakdown.base_cost_usd,
                net_profit_usd=breakdown.net_profit_usd,
            )
        )
        await session.commit()
        logger.info(
            "Consumo del run %s en %s: %s tokens de entrada, %s de salida, "
            "coste %s USD, precio %s créditos, ajuste %s",
            run_id,
            self.model.model_id,
            breakdown.prompt_tokens,
            breakdown.completion_tokens,
            breakdown.base_cost_usd,
            breakdown.client_cost_credits,
            delta,
        )
        return breakdown
