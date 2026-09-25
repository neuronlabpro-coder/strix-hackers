"""harden_git_tenant_fks

Revision ID: e3a5b7c9d1e2
Revises: e2f4b6d8a0c1
Create Date: 2026-09-25 15:30:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "e3a5b7c9d1e2"
down_revision: Union[str, Sequence[str], None] = "e2f4b6d8a0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Vincula revisiones al tenant del repositorio y valida el run asociado."""

    op.create_unique_constraint(
        "uq_repositories_id_organization",
        "repositories",
        ["id", "organization_id"],
    )
    op.drop_constraint(
        "pull_request_reviews_repository_id_fkey",
        "pull_request_reviews",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_pr_reviews_repository_organization",
        "pull_request_reviews",
        "repositories",
        ["repository_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_pull_request_review_tenant()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                OLD.organization_id IS DISTINCT FROM NEW.organization_id
                OR OLD.repository_id IS DISTINCT FROM NEW.repository_id
            ) THEN
                RAISE EXCEPTION 'La identidad tenant de la revisión no puede cambiar';
            END IF;
            IF NEW.run_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pentest_runs
                WHERE id = NEW.run_id AND organization_id = NEW.organization_id
            ) THEN
                RAISE EXCEPTION 'El run de la revisión pertenece a otra organización';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_pull_request_review_tenant
        BEFORE INSERT OR UPDATE ON pull_request_reviews
        FOR EACH ROW EXECUTE FUNCTION protect_pull_request_review_tenant();
        """
    )


def downgrade() -> None:
    """Rechaza el rollback para no debilitar el aislamiento R3."""

    raise RuntimeError(
        "e3a5b7c9d1e2 es forward-only: el rollback eliminaría las invariantes tenant."
    )
