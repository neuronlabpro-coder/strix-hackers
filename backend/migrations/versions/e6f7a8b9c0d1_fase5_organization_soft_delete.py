"""fase5_organization_soft_delete

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-26 21:15:00

Introduce el borrado lógico de organizaciones y convierte las claves forenses y
financieras en `ON DELETE RESTRICT`.

## Por qué RESTRICT y no CASCADE

Con `CASCADE`, borrar un tenant dispara un `DELETE` sobre `credit_ledger` y `audit_log`
que sus propios triggers `BEFORE DELETE` bloquean. El resultado es un error cuyo
mensaje habla de un trigger de auditoría, cuando el problema real es que la política
de borrado no está declarada donde corresponde.

Con `RESTRICT` la intención queda escrita en el esquema: un tenant con historial
financiero o forense no admite borrado físico. El fallo llega antes de tocar nada y
el mensaje es el correcto.

## Por qué `RESTRICT` y no `NO ACTION`

`NO ACTION` permite posponer la comprobación hasta el final de la transacción, de modo
que un borrado y una inserción posterior dentro de la misma transacción pueden
terminar en un estado imposible. `RESTRICT` comprueba en el acto. Aquí la diferencia
no se nota hoy, pero es la que evita una clase de fallo silencioso mañana.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tablas cuyo rastro no puede desaparecer con su organización. Se listan de forma
# explicita: descubrir esta regla recorriendo `information_schema` en una migración
# convierte una decisión de política en una consecuencia del esquema actual.
RESTRICTED_TABLES = ("credit_ledger", "audit_log")


def upgrade() -> None:
    # El enum de PostgreSQL guarda los valores, no la definición de Python. Añadir un
    # valor a `AuditActionEnum` sin extender el tipo deja el INSERT rechazado con
    # `invalid input value for enum`, que es un fallo de despliegue y no de código.
    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'ORGANIZATION_DELETED'")

    op.add_column(
        "organizations",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_organizations_stripe_customer_id", "organizations", ["stripe_customer_id"]
    )
    op.create_index("ix_organizations_deleted_at", "organizations", ["deleted_at"])

    # Las organizaciones existentes están vivas: `deleted_at` NULL e `is_active` true,
    # que es exactamente el estado por defecto de las columnas. No hace falta un UPDATE.
    for table in RESTRICTED_TABLES:
        op.drop_constraint(
            f"{table}_organization_id_fkey", table, type_="foreignkey"
        )
        op.create_foreign_key(
            f"{table}_organization_id_fkey",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for table in RESTRICTED_TABLES:
        op.drop_constraint(
            f"{table}_organization_id_fkey", table, type_="foreignkey"
        )
        op.create_foreign_key(
            f"{table}_organization_id_fkey",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.drop_index("ix_organizations_deleted_at", table_name="organizations")
    op.drop_constraint("uq_organizations_stripe_customer_id", "organizations")
    op.drop_column("organizations", "stripe_customer_id")
    op.drop_column("organizations", "is_active")
    op.drop_column("organizations", "deleted_at")
