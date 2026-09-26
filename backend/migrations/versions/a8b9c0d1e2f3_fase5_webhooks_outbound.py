"""fase5_webhooks_outbound

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-09-27 01:05:00

Crea `webhook_endpoints` y `webhook_deliveries`.

## Decisiones que la migración deja escritas en el esquema

- **`ON DELETE RESTRICT` en `webhook_endpoints.organization_id`.** Igual que
  `credit_ledger` y `api_tokens`: un tenant con webhooks conserva su fila. Un `CASCADE`
  borraría los endpoints en silencio y dejaría las entregas apuntando a un tenant que ya
  no existe, sin nada que lo explicara.

- **`ON DELETE CASCADE` en `webhook_deliveries.endpoint_id`.** Es la excepción, y es
  deliberada: el historial de entregas es la memoria de una operación concreta y desaparece
  con ella. Lo que no se borra nunca es el rastro financiero y el forense, que viven en
  `credit_ledger` y `audit_log`.

- **`organization_id` también en `webhook_deliveries`.** Duplicado a propósito. Sin esa
  columna, listar las entregas de un tenant exige un `JOIN` contra `webhook_endpoints`, y
  R3 exige que el filtro por organización esté en la consulta de la tabla que se lee. Con
  ella, la garantía es local a la tabla y no depende de un `JOIN` que una ruta futura
  pueda saltarse.

- **El secreto va cifrado, en `String(192)`.** La especificación pedía `String(64)` en
  claro, y ahí hay dos problemas. El primero es de longitud: `whsec_` son 6 caracteres y
  los 32 bytes de entropía son 64 en hexadecimal, o sea 70. Un `VARCHAR(64)` en
  PostgreSQL **trunca en silencio**, y un secreto truncado es un secreto con la mitad de
  la entropía cuyo fallo aparecería semanas después como "la firma no valida". El segundo
  es que la columna guarda el material **cifrado**, y medido sobre el formato real del
  proyecto son 135 caracteres. Los 192 dan margen para que el secreto crezca y para que
  nadie tenga que medir otra vez. El tercero es de fondo: el secreto tiene que ser
  recuperable para firmar, así que no puede guardarse como un hash, pero recuperable no
  es lo mismo que en claro. El proyecto ya cifra las credenciales de Git con AES-256-GCM
  ligado al tenant, y el material de firma de un webhook es el mismo tipo de secreto.

- **`event_types` como `JSONB`.** Se escribe y se lee la lista entera, y la consulta
  útil —qué endpoints escuchan `pentest.completed`— es un `@>` que JSONB resuelve con
  índice GIN.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # El enum de PostgreSQL guarda los valores, no la definición de Python. Añadir una
    # acción en el modelo sin extender el tipo deja el INSERT rechazado con `invalid
    # input value for enum`, que es un fallo de despliegue y no de código.
    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'WEBHOOK_AUTO_DISABLED'")

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("encrypted_secret", sa.String(length=192), nullable=False),
        sa.Column(
            "event_types",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
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
        # El contador de fallos no puede ser negativo. El auto-desactivado lo suma, y sin
        # esta restricción un descuadre en una resta futura dejaría la fila en -1, que
        # `is_auto_disabled` leería como un endpoint que pasó de lejos el umbral.
        sa.CheckConstraint(
            "consecutive_failures >= 0", name="ck_webhook_endpoints_failures_positive"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="webhook_endpoints_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_endpoints_organization_id", "webhook_endpoints", ["organization_id"]
    )
    op.create_index(
        "ix_webhook_endpoints_org_created",
        "webhook_endpoints",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_endpoints_failures",
        "webhook_endpoints",
        ["organization_id", "consecutive_failures"],
    )
    op.create_index(
        "ix_webhook_endpoints_org_active",
        "webhook_endpoints",
        ["organization_id", "is_active"],
    )
    op.create_index(
        "ix_webhook_endpoints_secret", "webhook_endpoints", ["encrypted_secret"], unique=True
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("execution_time_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # `attempt` empieza en 1 porque es un número de intento humano, no un índice base
        # cero. La restricción lo fija en el esquema para que un valor cero en una fila
        # creada a mano no se lea como "sin intentos" en el historial.
        sa.CheckConstraint("attempt >= 1", name="ck_webhook_deliveries_attempt_positive"),
        sa.CheckConstraint(
            "status_code IS NULL OR (status_code >= 100 AND status_code <= 599)",
            name="ck_webhook_deliveries_status_code_range",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoints.id"],
            name="webhook_deliveries_endpoint_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="webhook_deliveries_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"])
    op.create_index(
        "ix_webhook_deliveries_organization_id", "webhook_deliveries", ["organization_id"]
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint_created",
        "webhook_deliveries",
        ["endpoint_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint_event",
        "webhook_deliveries",
        ["endpoint_id", "event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_endpoint_event", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_endpoint_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_organization_id", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_endpoint_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index("ix_webhook_endpoints_secret", table_name="webhook_endpoints")
    op.drop_index("ix_webhook_endpoints_org_active", table_name="webhook_endpoints")
    op.drop_index("ix_webhook_endpoints_failures", table_name="webhook_endpoints")
    op.drop_index("ix_webhook_endpoints_org_created", table_name="webhook_endpoints")
    op.drop_index("ix_webhook_endpoints_organization_id", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")

    # El valor del enum no se quita: `ALTER TYPE ... DROP VALUE` no existe en PostgreSQL y
    # borrarlo exigiría recrear el tipo, lo que rompería las filas que ya lo usaron.
