"""protect_git_tenant_identity

Revision ID: e5c7d9e1f3a5
Revises: e4b6c8d0f2a3
Create Date: 2026-09-25 16:00:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e5c7d9e1f3a5"
down_revision: Union[str, Sequence[str], None] = "e4b6c8d0f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Impide mover repositorios o credenciales entre tenants/proveedores."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_git_tenant_identity()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_TABLE_NAME = 'repositories' THEN
                IF OLD.organization_id IS DISTINCT FROM NEW.organization_id
                   OR OLD.provider IS DISTINCT FROM NEW.provider
                   OR OLD.remote_repo_id IS DISTINCT FROM NEW.remote_repo_id THEN
                    RAISE EXCEPTION 'La identidad tenant del repositorio no puede cambiar';
                END IF;
            ELSIF OLD.organization_id IS DISTINCT FROM NEW.organization_id
               OR OLD.provider IS DISTINCT FROM NEW.provider THEN
                RAISE EXCEPTION 'La identidad tenant de la credencial no puede cambiar';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_repository_tenant_identity
        BEFORE UPDATE ON repositories
        FOR EACH ROW EXECUTE FUNCTION protect_git_tenant_identity();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_git_credential_tenant_identity
        BEFORE UPDATE ON git_credentials
        FOR EACH ROW EXECUTE FUNCTION protect_git_tenant_identity();
        """
    )


def downgrade() -> None:
    """Rechaza el rollback para no debilitar el aislamiento R3."""

    raise RuntimeError(
        "e5c7d9e1f3a5 es forward-only: el rollback elimina protecciones de tenant."
    )
