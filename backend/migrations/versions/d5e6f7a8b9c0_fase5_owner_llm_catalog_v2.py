"""fase5_owner_llm_catalog_v2

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-26 18:40:00

Sustituye el catálogo de modelos por el listado oficial exacto seleccionado por el
Owner, con `z-ai/glm-5.3` como primario de prioridad 1.

Es una migración nueva y no una edición de `b3c4d5e6f7a8` porque esa ya está aplicada
en la base de datos por Tailscale. Reescribir el seed de una migración aplicada deja
el repositorio y la base discrepando, y `alembic check` no lo detecta: compara el
esquema, no las filas. Los datos que no se pueden reescribir se corrigen hacia
adelante, que es la única dirección que la historia de la base permite deshacer.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Catálogo oficial del Owner.
# (model_id, display_name, coste entrada, coste salida, markup, prioridad, caso de uso)
#
# Los costes son por millón de tokens y el `markup_pct` es el recargo comercial sobre
# ese coste, según la semántica fijada en el Bloque 5.1: un 200 % significa que el
# cliente paga tres veces el coste.
#
# `z-ai/glm-5.3` se declara `ALL` y no `DEEP_PENTEST`. El Owner lo pidió como primario
# de `ALL / DEEP_PENTEST`, pero `model_id` es único y una fila solo admite un caso de
# uso, así que duplicar la fila es imposible. `ALL` es la única forma de expresar
# "primario de todas sin duplicar": `resolve_model_chain` incluye los modelos `ALL` en
# cada cadena, así que el modelo encabeza también la de DEEP_PENTEST. El efecto pedido
# se cumple exactamente y la restricción de unicidad se mantiene.
CATALOG = [
    (
        "z-ai/glm-5.3",
        "GLM 5.3",
        "0.40000000",
        "1.60000000",
        "200.0000",
        1,
        "ALL",
    ),
    (
        "openai/gpt-6-astra",
        "GPT-6 Astra",
        "4.00000000",
        "18.00000000",
        "150.0000",
        2,
        "DEEP_PENTEST",
    ),
    (
        "anthropic/claude-opus-5.5",
        "Claude Opus 5.5",
        "5.00000000",
        "25.00000000",
        "150.0000",
        3,
        "DEEP_PENTEST",
    ),
    (
        "deepseek/deepseek-v4-pro-0813",
        "DeepSeek V4 Pro 0813",
        "1.20000000",
        "4.80000000",
        "200.0000",
        4,
        "DEEP_PENTEST",
    ),
    (
        "anthropic/claude-fable-5.1",
        "Claude Fable 5.1",
        "2.00000000",
        "8.00000000",
        "150.0000",
        5,
        "AUTOFIX",
    ),
    (
        "openai/gpt-6-sol",
        "GPT-6 Sol",
        "1.50000000",
        "6.00000000",
        "200.0000",
        6,
        "ALL",
    ),
    (
        "moonshotai/kimi-k3",
        "Kimi K3",
        "0.80000000",
        "3.20000000",
        "250.0000",
        7,
        "ALL",
    ),
    (
        "deepseek/deepseek-v4.1-flash",
        "DeepSeek V4.1 Flash",
        "0.15000000",
        "0.60000000",
        "300.0000",
        8,
        "QUICK_SCAN",
    ),
]

# Modelos del catálogo anterior que el Owner retiró del catálogo activo.
RETIRED_MODEL_IDS = (
    "anthropic/claude-3.7-sonnet",
    "deepseek/deepseek-r1",
    "openai/o3-mini",
    "openai/gpt-4o",
    "deepseek/deepseek-chat",
    "openrouter/auto",
    "anthropic/claude-3.5-sonnet",
)


def upgrade() -> None:
    connection = op.get_bind()

    # Los retirados salen de la cadena ANTES de insertar el catálogo nuevo, para que
    # `priority_order` nunca tenga dos activos con el mismo número. Sin este orden, un
    # modelo retirado con prioridad 1 y `glm-5.3` con prioridad 1 coexistirían y el
    # desempate de `resolve_model_chain` pasa a depender del alfabeto del slug.
    for retired in RETIRED_MODEL_IDS:
        connection.execute(
            sa.text("UPDATE llm_model_configs SET is_active = false WHERE model_id = :model_id"),
            {"model_id": retired},
        )

    for (
        model_id,
        display_name,
        cost_input,
        cost_output,
        markup,
        priority,
        use_case,
    ) in CATALOG:
        connection.execute(
            sa.text(
                """
                INSERT INTO llm_model_configs (
                    id, model_id, display_name, base_cost_input_m, base_cost_output_m,
                    markup_pct, priority_order, is_active, use_case
                ) VALUES (
                    gen_random_uuid(), :model_id, :display_name, :cost_input, :cost_output,
                    :markup, :priority, true, :use_case
                )
                ON CONFLICT (model_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    base_cost_input_m = EXCLUDED.base_cost_input_m,
                    base_cost_output_m = EXCLUDED.base_cost_output_m,
                    markup_pct = EXCLUDED.markup_pct,
                    priority_order = EXCLUDED.priority_order,
                    is_active = true,
                    use_case = EXCLUDED.use_case
                """
            ),
            {
                "model_id": model_id,
                "display_name": display_name,
                "cost_input": cost_input,
                "cost_output": cost_output,
                "markup": markup,
                "priority": priority,
                "use_case": use_case,
            },
        )


def downgrade() -> None:
    """Reactiva el catálogo anterior y retira el nuevo.

    Es una reversión deactivation, no una restauración exacta: los costes del catálogo
    anterior se conservan en la migración `b3c4d5e6f7a8`, y reactivarlos sin restaurar
    sus valores dejaría la tabla con precios que no son ni de un catálogo ni del otro.
    Por eso la reversión solo reactiva y avisa por log en vez de fingir precisión.
    """

    connection = op.get_bind()
    for retired in RETIRED_MODEL_IDS:
        connection.execute(
            sa.text("UPDATE llm_model_configs SET is_active = true WHERE model_id = :model_id"),
            {"model_id": retired},
        )
    for (
        model_id,
        _display_name,
        _cost_input,
        _cost_output,
        _markup,
        _priority,
        _use_case,
    ) in CATALOG:
        connection.execute(
            sa.text("UPDATE llm_model_configs SET is_active = false WHERE model_id = :model_id"),
            {"model_id": model_id},
        )
