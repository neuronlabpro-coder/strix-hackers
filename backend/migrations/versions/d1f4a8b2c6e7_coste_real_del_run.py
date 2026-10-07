"""El coste real del proveedor y el coste del catálogo, uno al lado del otro.

## Qué añade

Tres columnas nullable en `pentest_runs`:

| Columna              | Qué es                                                          |
| :------------------- | :-------------------------------------------------------------- |
| `provider_cost_usd`  | Lo que el proveedor **cobró de verdad**, de `run.json`.        |
| `provider_tokens`    | Los tokens totales que el motor declara.                        |
| `catalogue_cost_usd` | Lo que el catálogo de la plataforma **cree** que cuesta.        |

## Por qué hace falta, y por qué no se arregla tarificando el real

Porque son **dos números distintos** y la plataforma solo conoce uno de ellos. El catálogo dice
qué precio por millón paga la plataforma; el proveedor dice qué facturó. No son lo mismo, y la
diferencia no es ruido.

Medido sobre el run real de este proyecto, `mindguard-site_23ee`, contra el catálogo de
`z-ai/glm-5.3` que está en la base:

| Magnitud                        | Valor    |
| :------------------------------ | :------- |
| Tokens de entrada declarados    | 16.819.076 |
| Tokens de salida declarados     | 46.417   |
| `run.json.llm_usage.cost`       | **1,84830111 USD** |
| Coste base según el catálogo    | 6,72763040 + 0,07426720 = **6,80189760 USD** |
| Precio al cliente (recargo 200 %)| **20,41 USD** |

El catálogo estima casi **cuatro veces** lo que el proveedor cobró. Con el recargo aplicado, el
tenant paga por tokens que en su mayoría **no se cobraron**: de esos 16,8 millones de entrada,
**16.443.264 los sirvió la caché del proveedor** (`input_tokens_details.cached_tokens`), que se
facturan a otro precio y que `LLMModelConfig` no puede representar porque solo tiene un
`base_cost_input_m`.

## Por qué el arreglo **no** es cambiar lo que se cobra

Porque el importe cobrado está **sellado** en el `credit_ledger`, que es *append-only* (R4). Y
porque cambiar la política de precios es una decisión comercial: cuándo se ajusta, en cuánto y a
quién se le avisa, es del humano. Este módulo **solo hace visible la divergencia**. Un número que
nadie ve no se puede decidir cambiar; un número con su contraponido al lado, sí.

Y hay un riesgo real en hacerlo al revés sin avisar: si el catálogo se ajustara a la baja ahora,
un tenant que ya pagó a precio alto por 20 USD de consumo real de 1,85 USD no recuperaría nada
—su asiento está sellado— y el siguiente pagaría menos por lo mismo. Ese es un problema de
facturación que se decide con los dos números delante, no con uno.

## Por qué `NULL` y no cero

Porque `NULL` es «no lo sé» y `0` es «no costó nada», y la diferencia es la misma que separa un
escaneo limpio de uno que no se pudo hacer. Un run anterior a esta migración, o un modo de
ejecución que no publica `llm_usage.cost`, no tiene dato: no tiene **cero**.

## Reversión

`DROP COLUMN` de las tres y nada más. No se toca `credit_ledger` ni `llm_usage_events`, así que la
reversión pierde estos tres números de los runs ya ingeridos y ningún asiento. Quien la ejecute
tiene que saber que está borrando histórico de coste, no referencias.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1f4a8b2c6e7"
down_revision: str | None = "b3a1f9c7d2e4"
branch_labels: str | None = None
depends_on: str | None = None

TABLA = "pentest_runs"

COLUMNAS = (
    sa.Column(
        "provider_cost_usd",
        sa.Numeric(precision=18, scale=8),
        nullable=True,
        comment="Coste que el proveedor cobro de verdad, de run.json.llm_usage.cost",
    ),
    sa.Column(
        "provider_tokens",
        sa.Integer(),
        nullable=True,
        comment="Tokens totales que declara el motor en run.json.llm_usage.total_tokens",
    ),
    sa.Column(
        "catalogue_cost_usd",
        sa.Numeric(precision=18, scale=8),
        nullable=True,
        comment="Coste base que el catalogo de llm_model_configs estima para ese consumo",
    ),
)


def upgrade() -> None:
    for columna in COLUMNAS:
        op.add_column(TABLA, columna)


def downgrade() -> None:
    for columna in COLUMNAS:
        op.drop_column(TABLA, columna.name)
