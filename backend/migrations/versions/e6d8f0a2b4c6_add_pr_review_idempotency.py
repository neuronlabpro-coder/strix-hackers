"""add_pr_review_idempotency

Revision ID: e6d8f0a2b4c6
Revises: e5c7d9e1f3a5
Create Date: 2026-09-25 17:00:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e6d8f0a2b4c6"
down_revision: Union[str, Sequence[str], None] = "e5c7d9e1f3a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Hace idempotente la revisión de un commit concreto del PR."""

    op.create_unique_constraint(
        "uq_pr_reviews_repository_pr_commit",
        "pull_request_reviews",
        ["repository_id", "pr_number", "commit_sha"],
    )


def downgrade() -> None:
    """Rechaza el rollback para no perder la garantía de idempotencia."""

    raise RuntimeError(
        "e6d8f0a2b4c6 es forward-only: el rollback permitiría escaneos duplicados."
    )
