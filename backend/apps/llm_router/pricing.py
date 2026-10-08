"""Cálculo de tarificación de tokens con margen comercial.

Función pura y sin dependencias de base de datos: el precio que paga el cliente
y el beneficio neto deben poder auditarse sin levantar el resto del sistema.

## Las dos cuentas, y por qué están separadas

Este módulo tiene **dos** funciones de coste y ninguna es la versión corregida de la otra:

| Función                | Pregunta que responde        | Effecto colateral               |
| :--------------------- | :--------------------------- | :------------------------------ |
| `compute_charge`       | ¿Cuánto paga el cliente?     | Escribe en `credit_ledger` (R4) |
| `compute_provider_cost`| ¿Cuánto le cuesta a la plataforma? | Ninguno: es telemetría  |

La segunda existe porque la primera **no puede** representar el precio real del proveedor: un solo
`base_cost_input_m` no distingue el token que entra de primero del que el proveedor sirvió de su
propia caché, y ese segundo tiene otro precio. Un QUICK real de este proyecto,
`mindguard-site_23ee`, consumió 16.819.076 tokens de entrada de los que **16.443.264** salieron de
caché: el catálogo lo valoraba en 6,80 USD y el proveedor facturó 1,85 USD.

Añadir la caché **aquí dentro** habría cambiado el precio del cliente. Por eso está en su propia
función, con su propia salida y sin ningún camino que la conecte al cobro.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Final

from pydantic import BaseModel, Field, computed_field

# Escala de OpenRouter: los precios del catálogo son por millón de tokens.
TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
ONE_HUNDRED: Final[Decimal] = Decimal(100)

_USD_QUANTUM: Final[Decimal] = Decimal("0.000001")
_PCT_QUANTUM: Final[Decimal] = Decimal("0.0001")
_CREDIT_QUANTUM: Final[Decimal] = Decimal("0.0001")


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
    credits_per_usd: Decimal,
) -> ChargeBreakdown:
    """Calcula coste base, precio al cliente, beneficio y créditos consumidos.

    Los importes se redondean a seis decimales (microdólar) antes de convertir a
    créditos, para que el redondeo no se acumule entre precio y beneficio. El
    beneficio se calcula sobre valores ya redondeados, de modo que
    `client_price_usd - base_cost_usd == net_profit_usd` siempre.

    ## Por qué `credits_per_usd` no tiene valor por defecto

    Porque tenía uno: `DEFAULT_CREDITS_PER_USD = Decimal("1")`. Con todos los llamadores
    pasando el valor no se notaba, pero era una segunda declaración de la paridad escrita a
    mano, igual que la que tenía el chat. Su daño era diferido y silencioso: el día que
    alguien añadiera un camino de cobro nuevo y **olvidara** el argumento, la llamada no
    fallaba —el valor por defecto la salvaba— y ese camino cobraba a 1:1 mientras el resto
    cobraba al precio vigente. Un cobro que se calcula con el precio equivocado y sin error es
    justo el fallo que este proyecto no puede permitirse en la facturación.

    ## Por qué un módulo sin base de datos no puede tener el precio

    Porque este módulo es puro a propósito: el desglose de un cobro tiene que poder
    auditarse sin levantar el sistema, y un `Decimal` a la firma lo permite. Lo que no
    permite es que el *precio* viva aquí, porque entonces volvería a estar escrito en el
    código. La solución no es meter una consulta —eso rompe la pureza— sino obligar a que
    quien llama diga cuál es el precio vigente.
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


# --------------------------------------------------------------------------- #
# El coste que el proveedor factura, que no es el precio que se cobra
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderCost:
    """Lo que el proveedor cobra por un consumo, con el precio de caché declarado o no.

    ## Por qué esto **no** es `ChargeBreakdown`

    Porque son dos números distintos con dos preguntas distintas:

    - `ChargeBreakdown` responde «cuánto paga el cliente», y su número está **sellado** en el
      `credit_ledger` (*append-only*, R4). Cambiar su fórmula es cambiar lo que se cobra, que es
      una decisión comercial del humano y no un efecto de hacer visible una medición.
    - `ProviderCost` responde «cuánto le cuesta a la plataforma», y es **telemetría**: no entra en
      el ledger, no mueve ningún saldo y no se puede arreglar un cobro ya emitido.

    ## Por qué `cache_price_published` es un campo y no se deduce de `cost_cached_input_m`

    Porque son dos afirmaciones distintas sobre lo que el proveedor **dice**, y confundirlas es
    exactamente el defecto que este módulo viene a arreglar:

    - `cached_input_cost_m IS NULL` significa «el proveedor no publica precio de caché para este
      modelo», así que la caché se valora al precio de la entrada normal.
    - `cached_input_cost_m = 0` significa «el proveedor publica precio de caché y es cero».

    Con la primera, la caché se trata como entrada normal **y se deja constancia**; con la segunda
    se trata como gratis, que es lo que dice el catálogo. Un modelo al que nunca se le pidió caché
    da el mismo número que uno sin caché publicada, y esa es justo la razón por la que el estado
    tiene que viajar al lado del importe.
    """

    prompt_tokens: int
    cached_tokens: int
    #: Los tokens de entrada que **no** salieron de la caché del proveedor.
    billable_input_tokens: int
    completion_tokens: int
    #: El precio por millón aplicado a la entrada no cacheada.
    cost_input_m: Decimal
    #: El precio por millón aplicado a la entrada cacheada. Con `cache_price_published` en `False`
    #: es **igual** a `cost_input_m`, y no un valor inventado.
    cost_cached_input_m: Decimal
    cost_output_m: Decimal
    #: ¿El catálogo declara un precio de caché para este modelo?
    cache_price_published: bool
    #: El coste estimado del proveedor, en dólares.
    cost_usd: Decimal
    input_cost_usd: Decimal
    cached_input_cost_usd: Decimal
    output_cost_usd: Decimal

    @property
    def cache_share(self) -> Decimal:
        """La fracción de la entrada que salió de la caché del proveedor.

        Se devuelve como fracción de 0 a 1 y no como porcentaje porque quien lo consume necesita
        multiplicarlo, no rotularlo. Con `None` de entrada la fracción es cero: no se pide caché, y
        eso no es lo mismo que pedirla y que el proveedor no la sirva.
        """

        if self.prompt_tokens <= 0:
            return Decimal(0)
        return Decimal(self.cached_tokens) / Decimal(self.prompt_tokens)


def compute_provider_cost(
    *,
    cost_input_m: Decimal,
    cost_cached_input_m: Decimal | None,
    cost_output_m: Decimal,
    prompt_tokens: int,
    cached_tokens: int | None,
    completion_tokens: int,
) -> ProviderCost:
    """El coste que el proveedor factura, distinguendo la entrada cacheada de la que no.

    La fórmula es la que el proveedor publica, y solo esta:

    ```
    coste = entrada_no_cacheada * coste_entrada_m
          + entrada_cacheada    * coste_cache_m
          + salida              * coste_salida_m
    ```

    con los tres precios **por millón de tokens** y el resultado en dólares.

    ## Por qué `cached_input_cost_m = None` usa `cost_input_m` y no lanza

    Porque `NULL` significa «el proveedor no publica precio de caché», que es una situación real y
    frecuente, no un dato corrupto. Un modelo sin caché publicada se valora al precio de la entrada
    normal y el resultado lo dice con `cache_price_published = False`, de modo que quien lo lee
    puede distinguirlo de un modelo al que nunca se le pidió caché —que daría el mismo importe—.

    La otra lectura posible —«sin precio de caché no se puede calcular»— convertiría una
    ausencia de dato en una ausencia de cifra, que es peor: se dejaría de medir el coste de un
    modelo que sí se está usando.

    ## Por qué la caché se recorta a la entrada total

    Porque un `cached_tokens` mayor que `prompt_tokens` es un reporte inconsistente, y aplicarlo tal
    cual daría una entrada facturable **negativa**: el total se iría hacia abajo y el coste con él,
    que es un número sin significado. Se recorta y el resultado expone el `cached_tokens` que se
    usó de verdad, para que el recorte se vea en lugar de quedar escondido.

    ## Por qué es una **función pura**

    Porque el importe con el que se decide la política de precios tiene que poder auditarse sin
    levantar la aplicación, sin sesión y sin red. Lo que la hace impura es el precio, y el precio
    no está aquí: llega en la firma.
    """

    if cost_input_m < 0 or cost_output_m < 0:
        raise ValueError("Los costes por millón de tokens no pueden ser negativos")
    if cost_cached_input_m is not None and cost_cached_input_m < 0:
        raise ValueError("El coste de la entrada cacheada no puede ser negativo")
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("El número de tokens no puede ser negativo")
    if cached_tokens is not None and cached_tokens < 0:
        raise ValueError("El número de tokens cacheados no puede ser negativo")

    publicado = cost_cached_input_m is not None
    precio_cache = cost_input_m if cost_cached_input_m is None else cost_cached_input_m
    cacheados = min(cached_tokens or 0, prompt_tokens)
    entrada_facturable = prompt_tokens - cacheados

    with localcontext() as context:
        context.prec = 40
        coste_entrada = (entrada_facturable * cost_input_m / TOKENS_PER_MILLION).quantize(
            _USD_QUANTUM, rounding=ROUND_HALF_UP
        )
        coste_cache = (cacheados * precio_cache / TOKENS_PER_MILLION).quantize(
            _USD_QUANTUM, rounding=ROUND_HALF_UP
        )
        coste_salida = (completion_tokens * cost_output_m / TOKENS_PER_MILLION).quantize(
            _USD_QUANTUM, rounding=ROUND_HALF_UP
        )

    return ProviderCost(
        prompt_tokens=prompt_tokens,
        cached_tokens=cacheados,
        billable_input_tokens=entrada_facturable,
        completion_tokens=completion_tokens,
        cost_input_m=cost_input_m,
        cost_cached_input_m=precio_cache,
        cost_output_m=cost_output_m,
        cache_price_published=publicado,
        cost_usd=coste_entrada + coste_cache + coste_salida,
        input_cost_usd=coste_entrada,
        cached_input_cost_usd=coste_cache,
        output_cost_usd=coste_salida,
    )


__all__ = (
    "TOKENS_PER_MILLION",
    "ChargeBreakdown",
    "ProviderCost",
    "compute_charge",
    "compute_provider_cost",
)
