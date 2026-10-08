"""Marca un modelo predeterminado por caso de uso sin alterar el historial."""

from alembic import op
import sqlalchemy as sa

revision: str = "b8c9d0e1f2a3"
down_revision: str = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_model_configs",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text(
            "UPDATE llm_model_configs SET is_default = true "
            "WHERE model_id = 'z-ai/glm-5.3' AND is_active = true"
        )
    )
    op.create_index(
        "uq_llm_model_configs_default_por_caso",
        "llm_model_configs",
        ["use_case"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_llm_model_configs_default_por_caso", table_name="llm_model_configs")
    op.drop_column("llm_model_configs", "is_default")
