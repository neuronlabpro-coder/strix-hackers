"""strict_git_ciphertext_checks

Revision ID: e4b6c8d0f2a3
Revises: e3a5b7c9d1e2
Create Date: 2026-09-25 15:45:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e4b6c8d0f2a3"
down_revision: Union[str, Sequence[str], None] = "e3a5b7c9d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Exige el formato completo v1.nonce.ciphertext de AES-GCM."""

    op.drop_constraint(
        "ck_git_credentials_access_token_encrypted",
        "git_credentials",
        type_="check",
    )
    op.drop_constraint(
        "ck_git_credentials_refresh_token_encrypted",
        "git_credentials",
        type_="check",
    )
    op.create_check_constraint(
        "ck_git_credentials_access_token_encrypted",
        "git_credentials",
        r"encrypted_access_token ~ '^v1\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{22,}$'",
    )
    op.create_check_constraint(
        "ck_git_credentials_refresh_token_encrypted",
        "git_credentials",
        r"encrypted_refresh_token IS NULL OR encrypted_refresh_token ~ '^v1\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{22,}$'",
    )


def downgrade() -> None:
    """Rechaza el rollback para no permitir Stored Git en texto plano."""

    raise RuntimeError(
        "e4b6c8d0f2a3 es forward-only: el rollback debilita la protección de credenciales."
    )
