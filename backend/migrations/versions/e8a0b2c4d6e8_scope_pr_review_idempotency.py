"""scope_pr_review_idempotency

Revision ID: e8a0b2c4d6e8
Revises: e7f9a1b3c5d7
Create Date: 2026-09-25 18:30:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e8a0b2c4d6e8"
down_revision: Union[str, Sequence[str], None] = "e7f9a1b3c5d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Incluye el commit base en la clave de idempotencia del PR."""

    op.drop_constraint(
        "uq_pr_reviews_repository_pr_commit",
        "pull_request_reviews",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_pr_reviews_repository_pr_commit",
        "pull_request_reviews",
        ["repository_id", "pr_number", "commit_sha", "base_sha"],
    )


def downgrade() -> None:
    """Rechaza el rollback para no debilitar la idempotencia del diff."""

    raise RuntimeError(
        "e8a0b2c4d6e8 es forward-only: el rollback pierde la identidad del diff base."
    )
