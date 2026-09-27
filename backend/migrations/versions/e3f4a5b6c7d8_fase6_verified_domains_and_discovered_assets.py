"""fase6_verified_domains_and_discovered_assets

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-27

Añade la superficie de ataque: dominios verificados y activos descubiertos.

## Por qué el `downgrade` borra primero los activos

Porque `discovered_assets` referencia a `verified_domains` con `CASCADE`. PostgreSQL ejecutaría
el borrado en cascada por su cuenta, pero dejarlo implícito haría que el `downgrade` pareciera
correcto y dependiera de un comportamiento de la base. Declarar el orden lo hace explícito y
falla de forma ruidosa si mañana esa clave cambia a `RESTRICT`: el `downgrade` se negaría a
borrar un dominio con activos colgados, que es mejor que dejar filas huérfanas.

## Por qué hay cuatro índices y no nueve

Porque los índices simples sobre `organization_id`, `is_verified` y `asset_type` serían
prefijos izquierdos de los compuestos que ya existen, y PostgreSQL no los usaría nunca: para
filtrar por `organization_id` prefiere el compuesto, que además trae `is_verified` o
`asset_type` en la segunda columna. Cada índice simple no ahorra ni una lectura y sí añade
una escritura en cada alta, cada verificación y cada activo nuevo.

Se conservan:

- `ix_verified_domains_org_verified`: el listado de dominios del panel.
- `ix_discovered_assets_domain_id`: **no** es prefijo de ningún compuesto. Lo necesita el
  `CASCADE` de PostgreSQL, que al borrar un dominio tiene que localizar sus activos, y el
  `count` por dominio de la respuesta de encolado.
- `ix_discovered_assets_org_scanned`: el inventario ordenado por última revisión.
- `ix_discovered_assets_org_type`: el filtro por tipo de activo.

"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Crea las dos tablas de la superficie de ataque."""

    op.create_table(
        "verified_domains",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("domain_name", sa.String(length=255), nullable=False),
        sa.Column("verification_token", sa.String(length=64), nullable=False),
        sa.Column(
            "verification_method",
            sa.Enum(
                "DNS_TXT", "HTTP_FILE", name="domain_verification_method_enum"
            ),
            server_default="DNS_TXT",
            nullable=False,
        ),
        sa.Column(
            "is_verified", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Un dominio no puede aparecer dos veces en el mismo workspace, con o sin
        # verificar. Sin esto, "verificar" no tendría una respuesta clara.
        sa.UniqueConstraint(
            "organization_id", "domain_name", name="uq_verified_domains_org_name"
        ),
    )
    op.create_index(
        "ix_verified_domains_org_verified",
        "verified_domains",
        ["organization_id", "is_verified"],
        unique=False,
    )

    op.create_table(
        "discovered_assets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("domain_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "asset_type",
            sa.Enum(
                "SUBDOMAIN", "IP_ADDRESS", "API_ENDPOINT", name="asset_type_enum"
            ),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("service_name", sa.String(length=100), nullable=True),
        sa.Column(
            "technologies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # `CASCADE` a propósito, y es el caso opuesto al de `organization_id`: un activo
        # pertenece a su dominio, y borrar un dominio no verificado tiene que llevarse sus
        # resultados de descubrimiento o el inventario se llenaría de basura de dominios
        # que ya no existen.
        sa.ForeignKeyConstraint(
            ["domain_id"], ["verified_domains.id"], ondelete="CASCADE"
        ),
        # `RESTRICT`: el inventario es la evidencia de qué se escaneó para cada cliente.
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Único **por dominio y tipo**, no global: el mismo host puede pertenecer a dos
        # dominios de la misma organización, y son dos clientes afectados, no uno.
        sa.UniqueConstraint(
            "domain_id", "asset_type", "value", name="uq_discovered_assets_unique"
        ),
    )
    op.create_index(
        "ix_discovered_assets_domain_id",
        "discovered_assets",
        ["domain_id"],
        unique=False,
    )
    op.create_index(
        "ix_discovered_assets_org_scanned",
        "discovered_assets",
        ["organization_id", "last_scanned_at"],
        unique=False,
    )
    op.create_index(
        "ix_discovered_assets_org_type",
        "discovered_assets",
        ["organization_id", "asset_type"],
        unique=False,
    )


def downgrade() -> None:
    """Borra la superficie de ataque en orden inverso al de las dependencias."""

    op.drop_index(
        "ix_discovered_assets_org_type", table_name="discovered_assets"
    )
    op.drop_index(
        "ix_discovered_assets_org_scanned", table_name="discovered_assets"
    )
    op.drop_index("ix_discovered_assets_domain_id", table_name="discovered_assets")
    op.drop_table("discovered_assets")

    op.drop_index(
        "ix_verified_domains_org_verified", table_name="verified_domains"
    )
    op.drop_table("verified_domains")

    op.execute("DROP TYPE IF EXISTS asset_type_enum")
    op.execute("DROP TYPE IF EXISTS domain_verification_method_enum")
