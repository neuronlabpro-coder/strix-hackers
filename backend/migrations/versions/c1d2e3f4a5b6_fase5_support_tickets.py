"""Fase 5 · Sistema nativo de tickets de soporte

## Qué crea

Dos tablas y una secuencia. La secuencia es la parte que parece accesoria y no lo es:
`ticket_number` es único, y numerarlo con `MAX(id) + 1` haría que dos altas simultáneas
chocaran. Una secuencia de PostgreSQL es atómica y no necesita bloqueo.

Empieza en `1001` con `START WITH`. El `1000` de margen no es estética: evita que un
número de ticket se confunda con un identificador de otra tabla, y deja hueco para los
mil primeros tickets antes de que los números empiecen a mezclarse visualmente con un id.

## Por qué `organization_id` es `RESTRICT` y no `CASCADE`

Con `CASCADE`, dar de baja un workspace borraría sus tickets —y con ellos la única prueba
de qué se le dijo a ese cliente—. Un ticket resuelto es evidencia en un dispute de
facturación o de responsabilidad sobre un hallazgo. `RESTRICT` hace que el borrado **falle**
y obligue a usar el borrado lógico, que es la vía correcta y conserva los tickets.

El mismo razonamiento aplica a `created_by_user_id` y a `sender_user_id`: `RESTRICT`. Si
alguien intenta borrar un usuario que ha escrito en un ticket, la base lo impide en vez de
dejar una conversación con el emisor borrado.

`assigned_to_user_id` es la excepción, y a propósito: `SET NULL`. Un agente que deja la
empresa no puede tener tickets asignados, y su baja no debe fallar por eso.

## Por qué no hay backfill

Las tablas son nuevas. No hay datos que migrar.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_number", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column(
            "category",
            sa.Enum("TECHNICAL", "BILLING", "VULNERABILITY_REVIEW", "FEATURE_REQUEST",
                    name="support_category_enum"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Enum("LOW", "NORMAL", "URGENT", name="ticket_priority_enum"),
            server_default="NORMAL",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED", name="ticket_status_enum"),
            server_default="OPEN",
            nullable=False,
        ),
        sa.Column("assigned_to_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            ["organization_id"],
            ["organizations.id"],
            # RESTRICT: dar de baja un workspace no puede borrar su historial de soporte.
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_number"),
    )
    op.create_index("ix_support_tickets_organization_id", "support_tickets", ["organization_id"])
    op.create_index("ix_support_tickets_created_by_user_id", "support_tickets", ["created_by_user_id"])
    op.create_index("ix_support_tickets_assigned_to_user_id", "support_tickets", ["assigned_to_user_id"])
    op.create_index("ix_support_tickets_created_at", "support_tickets", ["created_at"])
    op.create_index("ix_support_tickets_category", "support_tickets", ["category"])
    op.create_index("ix_support_tickets_priority", "support_tickets", ["priority"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
    op.create_index(
        "ix_support_tickets_tenant_created",
        "support_tickets",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_support_tickets_queue",
        "support_tickets",
        ["status", "priority", "created_at"],
    )

    op.create_table(
        "ticket_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_admin_reply", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # CASCADE: un ticket sin mensajes no es una conversación, y su rastro de haber
        # existido vive en audit_log, que por R4 no se borra.
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ticket_messages_ticket_created",
        "ticket_messages",
        ["ticket_id", "created_at"],
    )

    # La secuencia numera los tickets visibles. Empieza en 1001: los mil primeros números
    # no se confunden con un identificador de otra tabla.
    op.execute(
        sa.text(
            "CREATE SEQUENCE support_ticket_number_seq START WITH 1001 INCREMENT BY 1"
        )
    )
    # Los paréntesis y los casts (`::text`, `::regclass`) son la **forma canónica** con la
    # que PostgreSQL guarda un `DEFAULT`: envuelve la expresión entera y le pone el tipo a
    # cada operando. Declararla así desde el principio hace que el texto almacenado sea
    # idéntico al que declara el modelo, y `alembic check` no vea drift. Sin ellos la base
    # normaliza igual y el resultado es el mismo, pero el modelo declararía una cadena
    # distinta a la guardada y el verificador compararía dos textos que no coinciden.
    # Ver la nota del mismo campo en `backend/apps/support/models.py`.
    op.execute(
        sa.text(
            "ALTER TABLE support_tickets ALTER COLUMN ticket_number SET DEFAULT "
            "('TK-'::text || nextval('support_ticket_number_seq'::regclass))"
        )
    )


def downgrade() -> None:
    # El orden importa por la clave ajena: los mensajes primero.
    op.drop_index("ix_ticket_messages_ticket_created", table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index("ix_support_tickets_queue", table_name="support_tickets")
    op.drop_index("ix_support_tickets_tenant_created", table_name="support_tickets")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_priority", table_name="support_tickets")
    op.drop_index("ix_support_tickets_category", table_name="support_tickets")
    op.drop_index("ix_support_tickets_created_at", table_name="support_tickets")
    op.drop_index("ix_support_tickets_assigned_to_user_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_created_by_user_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_organization_id", table_name="support_tickets")
    op.drop_table("support_tickets")
    op.execute(sa.text("DROP SEQUENCE IF EXISTS support_ticket_number_seq"))
    op.execute(sa.text("DROP TYPE IF EXISTS support_category_enum"))
    op.execute(sa.text("DROP TYPE IF EXISTS ticket_priority_enum"))
    op.execute(sa.text("DROP TYPE IF EXISTS ticket_status_enum"))
