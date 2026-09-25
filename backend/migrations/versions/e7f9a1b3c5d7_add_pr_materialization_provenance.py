"""add_pr_materialization_provenance

Revision ID: e7f9a1b3c5d7
Revises: e6d8f0a2b4c6
Create Date: 2026-09-25 18:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7f9a1b3c5d7"
down_revision: Union[str, Sequence[str], None] = "e6d8f0a2b4c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persiste el origen y la base inmutables usados para materializar el PR."""

    op.add_column(
        "pull_request_reviews",
        sa.Column("head_clone_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "pull_request_reviews",
        sa.Column("base_sha", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_pr_reviews_base_sha",
        "pull_request_reviews",
        "base_sha IS NULL OR base_sha ~ '^[0-9a-fA-F]{40,64}$'",
    )
    op.create_check_constraint(
        "ck_pr_reviews_head_clone_url",
        "pull_request_reviews",
        "head_clone_url IS NULL OR head_clone_url ~ '^https://[^/]+/.+'",
    )


def downgrade() -> None:
    """Rechaza el rollback para no perder provenance de materialización."""

    raise RuntimeError(
        "e7f9a1b3c5d7 es forward-only: el rollback elimina provenance del PR."
    )
