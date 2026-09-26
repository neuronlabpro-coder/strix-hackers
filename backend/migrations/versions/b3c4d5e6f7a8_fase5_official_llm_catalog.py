"""fase5_official_llm_catalog_and_markup

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-26 12:30:00

Renombra `profit_margin_pct` a `markup_pct` (el campo es un recargo sobre coste,
no un margen sobre precio), instala el catálogo oficial de OpenRouter optimizado
para DevSecOps y estrecha `organizations.credit_balance` a `Numeric(12, 4)`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Catálogo oficial: (model_id, display, coste in, coste out, markup, prioridad, caso de uso).
# Los costes son los de referencia publicados por OpenRouter y el markup es la
# política comercial de la plataforma. Ambos se versionan aquí para que el margen
# sea auditable con `git blame` junto al resto de migraciones.
CATALOG = [
    (
        "anthropic/claude-3.7-sonnet",
        "Claude 3.7 Sonnet",
        "3.00000000",
        "15.00000000",
        "150.0000",
        1,
        "DEEP_PENTEST",
    ),
    (
        "deepseek/deepseek-r1",
        "DeepSeek R1",
        "0.55000000",
        "2.19000000",
        "200.0000",
        2,
        "DEEP_PENTEST",
    ),
    (
        "openai/o3-mini",
        "o3-mini",
        "1.10000000",
        "4.40000000",
        "150.0000",
        3,
        "DEEP_PENTEST",
    ),
    (
        "openai/gpt-4o",
        "GPT-4o",
        "2.50000000",
        "10.00000000",
        "150.0000",
        4,
        "ALL",
    ),
    (
        "deepseek/deepseek-chat",
        "DeepSeek Chat",
        "0.14000000",
        "0.28000000",
        "300.0000",
        5,
        "QUICK_SCAN",
    ),
    (
        "openrouter/auto",
        "OpenRouter Auto",
        "2.00000000",
        "8.00000000",
        "150.0000",
        6,
        "ALL",
    ),
]

# Modelos del catálogo anterior que quedan fuera: el 3.5 se sustituye por el 3.7.
RETIRED_MODEL_IDS = ("anthropic/claude-3.5-sonnet",)


def upgrade() -> None:
    op.alter_column(
        "llm_model_configs",
        "profit_margin_pct",
        new_column_name="markup_pct",
        existing_type=sa.Numeric(9, 4),
        existing_nullable=False,
    )
    op.execute(
        "ALTER TABLE llm_model_configs DROP CONSTRAINT IF EXISTS "
        "ck_llm_model_configs_margin_nonnegative"
    )
    op.execute(
        "ALTER TABLE llm_model_configs ADD CONSTRAINT ck_llm_model_configs_markup_nonnegative "
        "CHECK (markup_pct >= 0)"
    )

    # El saldo se estrecha a 12,4: con 1 crédito = 1 USD, 99.999.999,99 créditos
    # cubren cualquier saldo de una plataforma de por vida. Un saldo no negativo
    # solo necesita un dígito entero, así que la precisión adicional era Airport.
    op.alter_column(
        "organizations",
        "credit_balance",
        type_=sa.Numeric(12, 4),
        existing_type=sa.Numeric(18, 4),
        existing_nullable=False,
    )

    # Los modelos retirados salen de la cadena: dejarlos activos significaría que
    # un fallback pudiera enrutar a un modelo que producto ya no quiere ofrecer.
    # Un modelo con historial de consumo no se borra, se desactiva, porque sus
    # eventos de uso lo referencian.
    connection = op.get_bind()
    for retired in RETIRED_MODEL_IDS:
        connection.execute(
            sa.text(
                "DELETE FROM llm_model_configs "
                "WHERE model_id = :model_id AND NOT EXISTS ("
                "  SELECT 1 FROM llm_usage_events "
                "  WHERE llm_usage_events.model_config_id = llm_model_configs.id"
                ")"
            ),
            {"model_id": retired},
        )
        connection.execute(
            sa.text(
                "UPDATE llm_model_configs SET is_active = false WHERE model_id = :model_id"
            ),
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
    op.alter_column(
        "organizations",
        "credit_balance",
        type_=sa.Numeric(18, 4),
        existing_type=sa.Numeric(12, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "llm_model_configs",
        "markup_pct",
        new_column_name="profit_margin_pct",
        existing_type=sa.Numeric(9, 4),
        existing_nullable=False,
    )
    op.execute(
        "ALTER TABLE llm_model_configs DROP CONSTRAINT IF EXISTS "
        "ck_llm_model_configs_markup_nonnegative"
    )
    op.execute(
        "ALTER TABLE llm_model_configs ADD CONSTRAINT "
        "ck_llm_model_configs_margin_nonnegative CHECK (profit_margin_pct >= 0)"
    )
