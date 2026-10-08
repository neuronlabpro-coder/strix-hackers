"""Asocia opcionalmente un producto comercial a un modo de pentest.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

from alembic import op
import sqlalchemy as sa

revision: str = "a7b8c9d0e1f2"
down_revision: str = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pentest_products", sa.Column("scan_mode", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_pentest_products_scan_mode",
        "pentest_products",
        "scan_mode IS NULL OR scan_mode IN ('QUICK', 'STANDARD', 'DEEP')",
    )
    op.create_index(
        "ix_pentest_products_scan_mode", "pentest_products", ["scan_mode"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_pentest_products_scan_mode", table_name="pentest_products")
    op.drop_constraint("ck_pentest_products_scan_mode", "pentest_products", type_="check")
    op.drop_column("pentest_products", "scan_mode")
