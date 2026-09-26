"""fase5_api_tokens

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-26 22:40:00

Crea `api_tokens`, la tabla de credenciales de la API pública.

## Decisiones que la tabla deja escritas en el esquema

- **`ON DELETE RESTRICT` en `organization_id`.** Misma razón que `credit_ledger` y
  `audit_log`: un tenant no se borra físicamente. Con `CASCADE`, borrar un tenant
  borraría sus credenciales en silencio y el cliente se quedaría sin acceso sin ningún
  asiento que lo explicara.

- **`token_hash` único e indexado.** Es a la vez la clave de búsqueda —cada petición
  autenticada busca por ahí— y la garantía de unicidad del secreto. Una colisión de
  SHA-256 tiene que ser un error de inserción, no dos filas donde revocar una
  credencial affecte a la otra.

- **`scopes` como `JSONB` y no `JSON`.** Es binario, no deduplica claves y admite un
  índice GIN el día que exista una consulta del tipo "qué tokens tienen el scope X".

- **`last_used_at` sin valor por defecto.** Un `now()` de servidor lo llenaría al
  insertar y el panel mostraría todo token recién creado como "usado hace 0 s", que es
  una mentira que hace imposible detectar un token olvidado durante meses. Lo escribe la
  autenticación, y solo cuando el token se usa de verdad.

- **`created_at` con `server_default` y sin `updated_at`.** La única marca que puede
  cambiar después de crear la fila es `revoked_at`; un `updated_at` que se moviera al
  revocar sugeriría estado editable, que no existe.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # La longitud de `token_hash` se declara en el esquema para que una versión que
        # guardara otra cosa quede rechazada por la base, y no pase desapercibida hasta el
        # momento de buscar un token que no aparece porque su resumen tiene otra longitud.
        sa.CheckConstraint("char_length(token_hash) = 64", name="ck_api_tokens_hash_length"),
        # Un prefijo visible tiene que ser el prefijo del secreto y nada más corto, para
        # que la columna nunca se use como campo de búsqueda parcial por error.
        sa.CheckConstraint(
            "char_length(token_prefix) >= 9", name="ck_api_tokens_prefix_present"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="api_tokens_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_api_tokens_org_created", "api_tokens", ["organization_id", "created_at"]
    )
    op.create_index(
        "ix_api_tokens_org_revoked", "api_tokens", ["organization_id", "revoked_at"]
    )
    # Índice simple sobre `organization_id`: el modelo lo declara con `index=True` y sin
    # él Alembic vería drift. Se crea porque R3 exige que toda consulta a una tabla
    # privada filtre por organización, y ese filtro tiene que tener un índice detrás.
    op.create_index("ix_api_tokens_organization_id", "api_tokens", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_organization_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_org_revoked", table_name="api_tokens")
    op.drop_index("ix_api_tokens_org_created", table_name="api_tokens")
    op.drop_index("uq_api_tokens_token_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
