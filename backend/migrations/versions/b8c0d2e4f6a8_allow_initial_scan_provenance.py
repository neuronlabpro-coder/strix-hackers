"""allow_initial_scan_provenance

Revision ID: b8c0d2e4f6a8
Revises: a7b9c1d3e5f7
Create Date: 2026-09-25 12:45:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "b8c0d2e4f6a8"
down_revision: Union[str, Sequence[str], None] = "a7b9c1d3e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Permite asignar provenance una sola vez, pero impide cambiarla después."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_pentest_run_tenant()
        RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
               OR (OLD.source_scan_id IS NOT NULL
                   AND OLD.source_scan_id IS DISTINCT FROM NEW.source_scan_id) THEN
                RAISE EXCEPTION 'La identidad tenant o provenance de un pentest es inmutable.';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    """Rechaza el rollback para no debilitar la protección de provenance."""

    raise RuntimeError(
        "b8c0d2e4f6a8 es irreversible en producción: la provenance del scan debe conservarse."
    )
