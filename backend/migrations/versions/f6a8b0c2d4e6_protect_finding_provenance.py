"""protect_finding_provenance

Revision ID: f6a8b0c2d4e6
Revises: e5f7a9b1c3d4
Create Date: 2026-09-25 12:15:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "f6a8b0c2d4e6"
down_revision: Union[str, Sequence[str], None] = "e5f7a9b1c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Incluye la provenance externa en la protección de evidencias."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_vulnerability_evidence()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                RAISE EXCEPTION
                    'Las evidencias técnicas de una vulnerabilidad son inmutables y no pueden truncarse.';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'Las evidencias técnicas de una vulnerabilidad son inmutables y no pueden eliminarse.';
            END IF;
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
               OR OLD.run_id IS DISTINCT FROM NEW.run_id
               OR OLD.source_finding_id IS DISTINCT FROM NEW.source_finding_id
               OR OLD.title IS DISTINCT FROM NEW.title
               OR OLD.description IS DISTINCT FROM NEW.description
               OR OLD.severity IS DISTINCT FROM NEW.severity
               OR OLD.cvss_score IS DISTINCT FROM NEW.cvss_score
               OR OLD.cve_id IS DISTINCT FROM NEW.cve_id
               OR OLD.affected_target IS DISTINCT FROM NEW.affected_target
               OR OLD.affected_line IS DISTINCT FROM NEW.affected_line
               OR OLD.poc_reproduction_raw IS DISTINCT FROM NEW.poc_reproduction_raw
               OR OLD.autofix_patch_diff IS DISTINCT FROM NEW.autofix_patch_diff
               OR OLD.discovered_at IS DISTINCT FROM NEW.discovered_at THEN
                RAISE EXCEPTION
                    'Las evidencias técnicas y puntuaciones CVSS de una vulnerabilidad son inmutables.';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    """Rechaza el rollback para no permitir modificar la provenance."""

    raise RuntimeError(
        "f6a8b0c2d4e6 es irreversible en producción: la provenance de findings debe permanecer inmutable."
    )
