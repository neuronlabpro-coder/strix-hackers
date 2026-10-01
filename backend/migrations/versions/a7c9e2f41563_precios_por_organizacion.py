"""Precios pactados con cada organización, editables desde su ficha.

## Qué hace

Crea `organization_price_overrides`: el sitio donde vive un precio negociado para una organización
concreta y una operación concreta. No siembra nada, porque un precio pactado no se puede deducir:
se negocia, y una fila puesta por la migración sería un precio que nadie pactó con nadie.

## Por qué es una tabla aparte y no una columna en `platform_pricing`

Porque `platform_pricing` es la fila única de lo que cobra **la plataforma**, y esto es lo que
cobra **un cliente concreto**. Meter los dos en la misma tabla obligaría a decidir qué fila gana,
que es exactamente la pregunta que un precio por cliente convierte en un problema de todos los
días: hoy gana el override, ¿mañana qué?

## Por qué append-only

Por R4, y porque un precio pactado es un compromiso. Si mañana se sube el precio a un cliente, lo
que se escribe es una fila nueva con `valido_desde` en el futuro, y la anterior se queda. Dentro
de seis meses, «¿con qué precio se le cobró este escaneo?» tiene respuesta.

Una tabla editable de precios pactados es una tabla donde un cliente puede reclamar que se le
cambió el precio sin avisar, porque el rastro lo ha escrito la misma plataforma que cobra. Por
eso el disparador va aquí, en la misma migración que la tabla, para que no exista un estado
intermedio en el que la tabla exista y no esté protegida.

## Por qué `valido_hasta` y no borrar al renovar

Porque un precio que caduca solo no se puede dejar puesto por descuido. Con `valido_hasta`, el día
de la renovación el override deja de aplicar sin que nadie tenga que acordarse de retirarlo, y el
cliente vuelve al precio de plataforma. Ese momento es justo donde un olvido dejaría a un cliente
pagando el precio viejo, o a la plataforma cobrando el nuevo a quien ya se había renovado.

## Por qué el único es **parcial**

Porque protege una sola cosa: que no haya **dos** overrides sin fecha de fin para la misma
operación de la misma organización. Un `UNIQUE (organization_id, operacion, alcance)` normal
impediría también tener dos overrides *cerrados* de la misma operación, que es justamente el
histórico de las subidas de precio de ese cliente. El `WHERE valido_hasta IS NULL` deja pasar
todos los cerrados y prohibe solo los abiertos.

## Por qué el `CHECK` del alcance

Porque `CREDIT_PACK_AMOUNT` sin `alcance` no dice qué pack es. Y un override que no se puede
aplicar es un override que no existe: se pactaría un precio que no se cobra, y nadie se enteraría
porque no hay ningún error —el cobro saldría bien, con el precio de plataforma—.

## Por qué `RESTRICT` en la organización

Porque borrar una organización con precios pactados abiertos dejaría sus escaneos cobrándose al
precio de plataforma sin que nadie lo decidiera. Con `RESTRICT`, la base dice que no, y quien
quiere borrar tiene que cerrar primero los override.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c9e2f41563"
down_revision: str = "f4b5c6d7e8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Los valores del enum, en un solo sitio.
#:
#: ## Por qué el enum se declara una vez aquí y no dos
#:
#: ## Por qué `create_type=False` en la columna
#:
#: Porque `sa.Enum` dentro de `op.create_table` emite su propio `CREATE TYPE`. Si además se
#: crea el tipo aquí, la migración falla en el `CREATE TABLE` con «type already exists»,
#: después de haber creado el tipo: es decir, deja la base **a medias**, con el enum puesto y
#: la tabla sin crear. Con `create_type=False` en la columna, el tipo se crea una vez aquí y la
#: tabla lo reutiliza.
VALORES_OPERACION = (
    "SCAN_CREDIT_COST",
    "QUICK_SCAN_MULTIPLIER",
    "CREDITS_PER_USD",
    "PR_REVIEW_CREDITS",
    "LLM_MARKUP_PCT",
    "PRO_MONTHLY_USD",
    "CREDIT_PACK_AMOUNT",
)


def upgrade() -> None:
    postgresql.ENUM(*VALORES_OPERACION, name="price_operation_enum").create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "organization_price_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "operacion",
            postgresql.ENUM(
                name="price_operation_enum",
                create_type=False,
                values=list(VALORES_OPERACION),
            ),
            nullable=False,
        ),
        sa.Column("alcance", sa.String(length=64), nullable=True),
        sa.Column("valor", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("motivo", sa.String(length=255), nullable=False),
        sa.Column(
            "valido_desde",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("valido_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # `RESTRICT` y no `CASCADE`: borrar una organización con precios pactados abiertos
        # dejaría sus escaneos cobrándose al precio de plataforma sin que nadie lo decidiera.
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "valido_hasta IS NULL OR valido_hasta > valido_desde",
            name="ck_org_price_override_window",
        ),
        sa.CheckConstraint(
            "operacion <> 'CREDIT_PACK_AMOUNT' OR alcance IS NOT NULL",
            name="ck_org_price_override_scope",
        ),
        sa.CheckConstraint("valor IS NOT NULL", name="ck_org_price_override_valor"),
    )

    # El indice de lectura: cuanto menor, mas antiguo, para resolver «el vigente».
    op.create_index(
        "ix_org_price_override_vigentes",
        "organization_price_overrides",
        ["organization_id", "operacion", "valido_desde"],
    )
    # `created_by` con indice, por `ON DELETE SET NULL`: borrar un usuario dispara un UPDATE por
    # cada fila suya, y esta tabla solo crece.
    op.create_index(
        "ix_organization_price_overrides_created_by",
        "organization_price_overrides",
        ["created_by"],
    )
    # El indice unico PARCIAL: solo los override sin fecha de fin, que son los vigentes.
    op.create_index(
        "uq_org_price_override_vigente",
        "organization_price_overrides",
        ["organization_id", "operacion", "alcance"],
        unique=True,
        postgresql_where=sa.text("valido_hasta IS NULL"),
    )

    # El disparador de inmutabilidad, en la misma migracion que la tabla: no existe un estado
    # intermedio en el que la tabla exista y no este protegida.
    #
    # `FOR EACH STATEMENT` y no por fila, porque es lo que hace el resto del proyecto
    # (`credit_ledger`, `audit_log`, `platform_price_changes`): cubre `TRUNCATE` —que no dispara
    # ningun trigger de fila— y cuesta lo mismo pase lo que pase el tamano de la tabla.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_org_price_overrides_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'organization_price_overrides es append-only; no admite % ni TRUNCATE', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_org_price_overrides_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON organization_price_overrides
        FOR EACH STATEMENT EXECUTE FUNCTION protect_org_price_overrides_append_only();
        """
    )


def downgrade() -> None:
    # El orden inverso al de `upgrade`, y el disparador cae antes que su funcion: al reves, la
    # caida de la tabla ya se lleva el disparador, pero dejarlo puesto sobre una funcion que ya
    # no existe deja la base con una dependencia rota que `DROP TABLE` no avisa.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_protect_org_price_overrides_append_only "
        "ON organization_price_overrides"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_org_price_overrides_append_only()")
    op.drop_index("uq_org_price_override_vigente", table_name="organization_price_overrides")
    op.drop_index(
        "ix_organization_price_overrides_created_by",
        table_name="organization_price_overrides",
    )
    op.drop_index("ix_org_price_override_vigentes", table_name="organization_price_overrides")
    op.drop_table("organization_price_overrides")
    postgresql.ENUM(name="price_operation_enum").drop(op.get_bind(), checkfirst=True)
