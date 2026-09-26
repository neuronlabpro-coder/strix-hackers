"""Resolución de la cadena de modelos y clasificación de fallos transitorios."""

from __future__ import annotations

import logging
from typing import Final
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum

logger = logging.getLogger(__name__)

# Códigos de OpenRouter que representan un fallo transitorio: reintentar con el
# siguiente modelo de la cadena tiene sentido. Un 401 o un 400 no son
# transitorios: reintentar solo gastaría tiempo y tokens sin cambiar el
# resultado, así que abortan la cadena.
TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMAllModelsInactiveError(RuntimeError):
    """No hay ningún modelo activo para el caso de uso solicitado.

    Falla cerrado de forma explícita en lugar de recurrir a un modelo por
    defecto en código: un modelo implícito sería coste no auditado ni tarifado.
    """


class LLMNonTransientError(RuntimeError):
    """El proveedor rechazó la petición por un motivo que no mejora con reintento."""


def is_transient_status(status_code: int) -> bool:
    """Clasifica un código HTTP del proveedor como transitorio o no."""

    return status_code in TRANSIENT_STATUS_CODES or 500 <= status_code < 600


def classify_provider_error(error: Exception) -> tuple[bool, str]:
    """Decide si un error del proveedor admite fallback y con qué severidad.

    Devuelve `(transitorio, razón)`. La razón entra al log estructurado pero
    nunca al mensaje que ve el cliente: los errores del proveedor pueden
    incluir URLs con claves o identificadores de cuenta.
    """

    if isinstance(error, httpx.TimeoutException):
        return True, "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if is_transient_status(code):
            return True, f"http_{code}"
        return False, f"http_{code}"
    if isinstance(error, (httpx.TransportError, ConnectionError)):
        return True, "transport"
    return False, type(error).__name__


async def resolve_model_chain(
    session: AsyncSession,
    use_case: LLMUseCaseEnum,
) -> list[LLMModelConfig]:
    """Devuelve la cadena de modelos activos para un caso de uso, por prioridad.

    Se incluyen los modelos del caso de uso específico y los declarados como
    `ALL`, que son transversales. Un modelo de `ALL` con prioridad más alta que
    uno específico actúa como primario: el orden lo marca `priority_order`, no la
    specificity del caso de uso.
    """

    result = await session.execute(
        select(LLMModelConfig)
        .where(
            LLMModelConfig.is_active.is_(True),
            LLMModelConfig.use_case.in_([use_case, LLMUseCaseEnum.ALL]),
        )
        .order_by(LLMModelConfig.priority_order, LLMModelConfig.model_id)
    )
    chain = list(result.scalars().all())
    if not chain:
        raise LLMAllModelsInactiveError(
            f"No hay modelos de lenguaje activos para el caso de uso {use_case.value}"
        )
    return chain


async def record_usage(
    session: AsyncSession,
    *,
    model: LLMModelConfig,
    use_case: LLMUseCaseEnum,
    prompt_tokens: int,
    completion_tokens: int,
    base_cost_usd: object,
    net_profit_usd: object,
    organization_id: UUID | None = None,
    run_id: UUID | None = None,
) -> None:
    """Persiste el consumo de una llamada para que el margen sea auditable."""

    from decimal import Decimal

    from backend.apps.llm_router.models import LLMUsageEvent

    session.add(
        LLMUsageEvent(
            model_config_id=model.id,
            organization_id=organization_id,
            run_id=run_id,
            use_case=use_case,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            base_cost_usd=Decimal(str(base_cost_usd)),
            net_profit_usd=Decimal(str(net_profit_usd)),
        )
    )


def next_in_chain(
    chain: list[LLMModelConfig],
    current: LLMModelConfig,
) -> LLMModelConfig | None:
    """Devuelve el siguiente eslabón tras un fallo transitorio, o `None`.

    La identidad se compara por `model_id` y no por `id`: `model_id` es la
    identidad canónica de OpenRouter, existe antes del `flush` y es lo que
    aparece en los logs de consumo. Comparar por `id` haría que dos modelos aún
    sin persistir (`id` a `None`) se confundieran entre sí.
    """

    for index, model in enumerate(chain):
        if model.model_id == current.model_id:
            return chain[index + 1] if index + 1 < len(chain) else None
    return None
