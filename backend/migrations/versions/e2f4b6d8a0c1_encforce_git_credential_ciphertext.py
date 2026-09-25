"""enforce_git_credential_ciphertext

Revision ID: e2f4b6d8a0c1
Revises: e1f3a5c7e9b0
Create Date: 2026-09-25 15:15:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e2f4b6d8a0c1"
down_revision: Union[str, Sequence[str], None] = "e1f3a5c7e9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Impide que tokens Git en texto plano entren en la tabla de credenciales."""

    op.create_check_constraint(
        "ck_git_credentials_access_token_encrypted",
        "git_credentials",
        "encrypted_access_token LIKE 'v1.%'",
    )
    op.create_check_constraint(
        "ck_git_credentials_refresh_token_encrypted",
        "git_credentials",
        "encrypted_refresh_token IS NULL OR encrypted_refresh_token LIKE 'v1.%'",
    )


def downgrade() -> None:
    """Elimina únicamente las restricciones de formato cifrado."""

    op.drop_constraint(
        "ck_git_credentials_refresh_token_encrypted",
        "git_credentials",
        type_="check",
    )
    op.drop_constraint(
        "ck_git_credentials_access_token_encrypted",
        "git_credentials",
        type_="check",
    )
