"""fase5_llm_router_credit_ledger

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-26 10:15:00

Crea el catálogo de LLMs con su registro de consumo, el ledger de créditos
append-only, la idempotencia de eventos de Stripe y migera el saldo de la
organización de `float` a `numeric`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Precios de referencia de OpenRouter en USD por millón de tokens. Son datos de
# catálogo, no secretos, y se versionan aquí para que el margen sea auditable
# con `git blame` junto al resto de migraciones.
_SEED_MODELS = [
    (
        "anthropic/claude-3.5-sonnet",
        "Claude 3.5 Sonnet",
        "3.00000000",
        "15.00000000",
        "120.0000",
        1,
        True,
        "ALL",
    ),
    (
        "openai/gpt-4o",
        "GPT-4o",
        "2.50000000",
        "10.00000000",
        "150.0000",
        2,
        True,
        "ALL",
    ),
    (
        "deepseek/deepseek-chat",
        "DeepSeek Chat",
        "0.27000000",
        "1.10000000",
        "200.0000",
        3,
        True,
        "ALL",
    ),
]


def upgrade() -> None:
    op.execute("CREATE TYPE llm_use_case_enum AS ENUM ('ALL', 'QUICK_SCAN', 'DEEP_PENTEST', 'AUTOFIX')")
    op.execute("CREATE TYPE ledger_reason_enum AS ENUM ('SCAN_CONSUMPTION', 'STRIPE_PURCHASE', 'ADMIN_ADJUSTMENT', 'SIGNUP_BONUS')")

    op.execute(
        """
        CREATE TABLE llm_model_configs (
            id UUID PRIMARY KEY,
            model_id VARCHAR(255) NOT NULL,
            display_name VARCHAR(128) NOT NULL,
            base_cost_input_m NUMERIC(18, 8) NOT NULL,
            base_cost_output_m NUMERIC(18, 8) NOT NULL,
            profit_margin_pct NUMERIC(9, 4) NOT NULL,
            priority_order INTEGER NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            use_case llm_use_case_enum NOT NULL DEFAULT 'ALL',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_llm_model_configs_model_id UNIQUE (model_id),
            CONSTRAINT ck_llm_model_configs_priority_positive CHECK (priority_order >= 1),
            CONSTRAINT ck_llm_model_configs_costs_nonnegative CHECK (base_cost_input_m >= 0 AND base_cost_output_m >= 0),
            CONSTRAINT ck_llm_model_configs_margin_nonnegative CHECK (profit_margin_pct >= 0),
            CONSTRAINT ck_llm_model_configs_display_name CHECK (display_name <> '')
        )
        """
    )
    op.execute("CREATE INDEX ix_llm_model_configs_model_id ON llm_model_configs (model_id)")
    op.execute("CREATE INDEX ix_llm_model_configs_routing ON llm_model_configs (use_case, is_active, priority_order)")

    op.execute(
        """
        CREATE TABLE llm_usage_events (
            id UUID PRIMARY KEY,
            model_config_id UUID NOT NULL REFERENCES llm_model_configs(id) ON DELETE CASCADE,
            organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
            run_id UUID REFERENCES pentest_runs(id) ON DELETE SET NULL,
            use_case llm_use_case_enum NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            base_cost_usd NUMERIC(18, 8) NOT NULL,
            net_profit_usd NUMERIC(18, 8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_llm_usage_prompt_tokens_nonnegative CHECK (prompt_tokens >= 0),
            CONSTRAINT ck_llm_usage_completion_tokens_nonnegative CHECK (completion_tokens >= 0),
            CONSTRAINT ck_llm_usage_base_cost_nonnegative CHECK (base_cost_usd >= 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_llm_usage_events_model_config_id ON llm_usage_events (model_config_id)")
    op.execute("CREATE INDEX ix_llm_usage_events_organization_id ON llm_usage_events (organization_id)")
    op.execute("CREATE INDEX ix_llm_usage_model_created ON llm_usage_events (model_config_id, created_at)")
    op.execute("CREATE INDEX ix_llm_usage_run ON llm_usage_events (run_id)")

    # El saldo pasa de `float` a `numeric`. Un ledger de créditos con coma
    # flotante deriva de centavo y acaba sin cuadrar sin explicación posible.
    op.alter_column(
        "organizations",
        "credit_balance",
        type_=sa.Numeric(18, 4),
        existing_type=sa.Float(),
        existing_nullable=False,
        postgresql_using="credit_balance::numeric",
    )

    op.execute(
        """
        CREATE TABLE credit_ledger (
            id UUID PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            amount_delta NUMERIC(18, 4) NOT NULL,
            balance_after NUMERIC(18, 4) NOT NULL,
            reason ledger_reason_enum NOT NULL,
            reference_id VARCHAR(128),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_credit_ledger_delta_nonzero CHECK (amount_delta <> 0),
            CONSTRAINT ck_credit_ledger_balance_nonnegative CHECK (balance_after >= 0),
            CONSTRAINT ck_credit_ledger_reference_not_empty CHECK (reference_id IS NULL OR reference_id <> '')
        )
        """
    )
    op.execute("CREATE INDEX ix_credit_ledger_organization_id ON credit_ledger (organization_id)")
    op.execute("CREATE INDEX ix_credit_ledger_reason ON credit_ledger (reason)")
    op.execute("CREATE INDEX ix_credit_ledger_org_created ON credit_ledger (organization_id, created_at)")
    op.execute("CREATE INDEX ix_credit_ledger_reference ON credit_ledger (organization_id, reference_id)")

    # R4: el libro mayor es append-only. Sin esto, un operador con escritura en la
    # base podría borrar el asiento de una compra y el saldo dejaría de cuadrar
    # con el historial que se presenta al cliente.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_credit_ledger_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'El ledger de creditos es append-only y no admite UPDATE, DELETE ni TRUNCATE.';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_credit_ledger_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON credit_ledger
        FOR EACH STATEMENT EXECUTE FUNCTION protect_credit_ledger_append_only()
        """
    )

    op.execute(
        """
        CREATE TABLE stripe_events (
            id UUID PRIMARY KEY,
            event_id VARCHAR(128) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
            session_id VARCHAR(128),
            credits_granted NUMERIC(18, 4),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_stripe_events_event_id UNIQUE (event_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_stripe_events_organization_id ON stripe_events (organization_id)")
    op.execute("CREATE INDEX ix_stripe_events_created ON stripe_events (created_at)")

    connection = op.get_bind()
    for (
        model_id,
        display_name,
        cost_input,
        cost_output,
        margin,
        priority,
        is_active,
        use_case,
    ) in _SEED_MODELS:
        connection.execute(
            sa.text(
                """
                INSERT INTO llm_model_configs (
                    id, model_id, display_name, base_cost_input_m, base_cost_output_m,
                    profit_margin_pct, priority_order, is_active, use_case
                ) VALUES (
                    gen_random_uuid(), :model_id, :display_name, :cost_input, :cost_output,
                    :margin, :priority, :is_active, :use_case
                )
                """
            ),
            {
                "model_id": model_id,
                "display_name": display_name,
                "cost_input": cost_input,
                "cost_output": cost_output,
                "margin": margin,
                "priority": priority,
                "is_active": is_active,
                "use_case": use_case,
            },
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stripe_events")
    op.execute("DROP TRIGGER IF EXISTS trg_protect_credit_ledger_append_only ON credit_ledger")
    op.execute("DROP FUNCTION IF EXISTS protect_credit_ledger_append_only()")
    op.execute("DROP TABLE IF EXISTS credit_ledger")
    op.alter_column(
        "organizations",
        "credit_balance",
        type_=sa.Float(),
        existing_type=sa.Numeric(18, 4),
        existing_nullable=False,
    )
    op.execute("DROP TABLE IF EXISTS llm_usage_events")
    op.execute("DROP TABLE IF EXISTS llm_model_configs")
    op.execute("DROP TYPE IF EXISTS ledger_reason_enum")
    op.execute("DROP TYPE IF EXISTS llm_use_case_enum")
