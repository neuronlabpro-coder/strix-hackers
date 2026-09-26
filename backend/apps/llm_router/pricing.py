"""Cálculo de tarificación de tokens con margen comercial.

Función pura y sin dependencias de base de datos: el precio que paga el cliente
y el beneficio neto deben poder auditarse sin levantar el resto del sistema.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Final

from pydantic import BaseModel, Field, computed_field

# Escala de OpenRouter: los precios del catálogo son por millón de tokens.
TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
ONE_HUNDRED: Final[Decimal] = Decimal(100)

_USD_QUANTUM: Final[Decimal] = Decimal("0.000001")
_PCT_QUANTUM: Final[Decimal] = Decimal("0.0001")
_CREDIT_QUANTUM: Final[Decimal] = Decimal("0.0001")

DEFAULT_CREDITS_PER_USD: Final[Decimal] = Decimal("1")


class ChargeBreakdown(BaseModel):
    """Desglose auditable de una llamada a modelo de lenguaje.

    `markup_pct` es un **recargo sobre coste base**, no un margen sobre
    precio de venta. Con la definición habitual de margen, un 150 % sería
    imposible (el precio tendría que ser negativo); con la definición de recargo,
    150 % significa que el cliente paga 2.5 veces el coste. El proyecto usa
    recargo, y el nombre del campo lo documenta para que nadie lo lea al revés.
    """

    model_config = {"frozen": True}

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    markup_pct: Decimal = Field(ge=0)
    base_cost_usd: Decimal
    client_price_usd: Decimal
    net_profit_usd: Decimal
    net_profit_pct: Decimal
    client_cost_credits: Decimal
    credits_per_usd: Decimal = Field(ge=Decimal("0.0001"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def markup_multiplier(self) -> Decimal:
        """Factor multiplicador total aplicado al coste base (2.5x para 150 %)."""

        return (ONE_HUNDRED + self.markup_pct) / ONE_HUNDRED


def compute_charge(
    *,
    base_cost_input_m: Decimal,
    base_cost_output_m: Decimal,
    markup_pct: Decimal,
    prompt_tokens: int,
    completion_tokens: int,
    credits_per_usd: Decimal = DEFAULT_CREDITS_PER_USD,
) -> ChargeBreakdown:
    """Calcula coste base, precio al cliente, beneficio y créditos consumidos.

    Los importes se redondean a seis decimales (microdólar) antes de convertir a
    créditos, para que el redondeo no se acumule entre precio y beneficio. El
    beneficio se calcula sobre valores ya redondeados, de modo que
    `client_price_usd - base_cost_usd == net_profit_usd` siempre.
    """

    if base_cost_input_m < 0 or base_cost_output_m < 0:
        raise ValueError("Los costes base por millón de tokens no pueden ser negativos")
    if markup_pct < 0:
        raise ValueError("El margen comercial no puede ser negativo")
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("El número de tokens no puede ser negativo")
    if credits_per_usd <= 0:
        raise ValueError("La conversión de dólares a créditos debe ser positiva")

    # `localcontext` evita que la precisión global de `decimal` altere el
    # resultado en el resto del proceso.
    with localcontext() as context:
        context.prec = 40
        base_cost = (
            base_cost_input_m * prompt_tokens
            + base_cost_output_m * completion_tokens
        ) / TOKENS_PER_MILLION
        client_price = base_cost * (ONE_HUNDRED + markup_pct) / ONE_HUNDRED

    base_usd = base_cost.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    price_usd = client_price.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    profit_usd = (price_usd - base_usd).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    profit_pct = (
        (profit_usd / base_usd * ONE_HUNDRED) if base_usd > 0 else Decimal(0)
    ).quantize(_PCT_QUANTUM, rounding=ROUND_HALF_UP)
    credits = (price_usd * credits_per_usd).quantize(
        _CREDIT_QUANTUM, rounding=ROUND_HALF_UP
    )

    return ChargeBreakdown(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        markup_pct=markup_pct,
        base_cost_usd=base_usd,
        client_price_usd=price_usd,
        net_profit_usd=profit_usd,
        net_profit_pct=profit_pct,
        client_cost_credits=credits,
        credits_per_usd=credits_per_usd,
    )
