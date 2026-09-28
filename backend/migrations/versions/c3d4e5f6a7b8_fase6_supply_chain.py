"""Fase 6 · Supply Chain: inventario de dependencias

## Qué hace

Crea la tabla `supply_chain_packages` y el enum `ecosystem_enum`.

## Por qué la columna `has_vulnerabilities` nace **nullable**

Porque son tres estados y una columna booleana solo admite dos:

- `TRUE`: comprobado, tiene vulnerabilidades conocidas.
- `FALSE`: comprobado, limpio.
- `NULL`: **no comprobado**, porque no hay fuente de vulnerabilidades por paquete que lo
  compruebe.

El proyecto tiene `cve_records`, pero esa tabla guarda `cve_id`, severidad, CVSS, EPSS y una
descripción: **no guarda qué paquetes afecta cada CVE**. Cruzar una dependencia con ella exige
buscar el nombre del paquete dentro del texto libre de una descripción, y eso produce falsos
positivos. En una herramienta de seguridad, un "este paquete tiene un CVE" falso cuesta más que
un "no lo sabemos": el usuario deja de confiar en la herramienta o descarta una alerta real.

Si la columna naciera `NOT NULL DEFAULT false`, todos los paquetes recién indexados se
mostrarían como "limpios" sin que nadie los hubiera comprobado, y el panel estaría afirmando
algo que nadie sabe. El `NULL` es lo que permite que el tercero estado exista.

## Por qué `ecosystem_enum` se crea con los siete valores de una vez

Porque `ALTER TYPE ... ADD VALUE` es la operación de esquema más restringida que existe en
PostgreSQL —el valor nuevo no se puede usar en la misma transacción que lo añade— y porque los
siete valores se conocen. Añadirlos de uno en uno obligaría a siete migraciones para lo que es
una decisión de una vez.

Se crea **con** la tabla y no en una migración aparte, a diferencia de los valores de
`audit_action_enum` y `ledger_reason_enum`, que se añadieron cuando las tablas ya existían y por
eso viven en migraciones propias. Aquí nace y muere aquí.

La diferencia de fondo es el orden: los `ADD VALUE` de los otros enums no se podían meter
en la misma transacción que la tabla que los usa, y este sí puede porque el primer `INSERT` lo
hace la aplicación, no esta migración.

## Por qué el índice de vulnerabilidades incluye `organization_id`

Porque ese filtro es **obligatorio** (R3) y va en toda consulta. Un índice solo sobre
`has_vulnerabilities` obligaría a PostgreSQL a recorrer las dependencias de todos los tenants y
descartar en memoria las de los demás: lento, y además hace trabajo con datos de otros
clientes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Los siete valores del enum, en el mismo orden que `EcosystemEnum`. Se repiten aqui y no se
#: importan del modelo a proposito: una migracion que importa el modelo de la aplicacion se
#: rompe el dia que el modelo cambie, y una migracion tiene que describir **el** estado de la
#: base, no el de la version de codigo que se aplico.
ECOSYSTEMS = ("NPM", "PYPI", "GO", "CARGO", "MAVEN", "COMPOSER", "OTHER")


def upgrade() -> None:
    # El parentesis no es decorativo: `CREATE TYPE x AS ENUM 'A', 'B'` es un error de sintaxis
    # y PostgreSQL lo rechaza con un mensaje que no nombra el parentesis. Se construye la lista
    # completa en una linea y se pasa por `op.execute` en vez de dejar que lo haga `sa.Enum`,
    # porque el tipo se crea **antes** que la tabla y el `create_type` de SQLAlchemy 2 ya no
    # existe.
    valores = ", ".join(f"'{valor}'" for valor in ECOSYSTEMS)
    op.execute(f"CREATE TYPE ecosystem_enum AS ENUM ({valores})")

    op.create_table(
        "supply_chain_packages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column(
            "ecosystem",
            # `postgresql.ENUM` y no `sa.Enum`: el `create_type` de `sa.Enum` se elimino en
            # SQLAlchemy 2, y sin el nombre explicito la columna no se ata al tipo creado
            # arriba. Declararlo con `create_type=False` es lo que hace que la migracion no
            # intente crear el tipo una segunda vez.
            postgresql.ENUM(*ECOSYSTEMS, name="ecosystem_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("license", sa.String(length=100), nullable=True),
        # Nullable y **sin** default. Ver el encabezado: `NOT NULL DEFAULT false` haria que
        # todo paquete recien indexado apareciese como comprobado y limpio.
        sa.Column("has_vulnerabilities", sa.Boolean(), nullable=True, server_default=None),
        sa.Column(
            "cve_ids",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("manifest_path", sa.String(length=255), nullable=True),
        sa.Column(
            "is_dev_dependency", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "repository_id",
            "name",
            "ecosystem",
            name="uq_supply_chain_repo_name_ecosystem",
        ),
    )

    op.create_index(
        "ix_supply_chain_repo_name_version",
        "supply_chain_packages",
        ["repository_id", "name", "version"],
    )
    op.create_index(
        "ix_supply_chain_org_vulnerable",
        "supply_chain_packages",
        ["organization_id", "has_vulnerabilities"],
    )
    op.create_index(
        "ix_supply_chain_org_ecosystem",
        "supply_chain_packages",
        ["organization_id", "ecosystem"],
    )


def downgrade() -> None:
    """Deshace todo, incluido el enum.

    A diferencia de `audit_action_enum`, aqui `DROP TYPE` **si** es posible: no hay filas que
    dependan del enum mas alla de la tabla que se borra justo antes, y PostgreSQL 16 no impide
    borrar un tipo sin uso. Un `downgrade` que deja la tabla pero no el enum, o al reves, deja
    la base en un estado que no corresponde a ninguna version del codigo.
    """

    op.drop_index("ix_supply_chain_org_ecosystem", table_name="supply_chain_packages")
    op.drop_index("ix_supply_chain_org_vulnerable", table_name="supply_chain_packages")
    op.drop_index("ix_supply_chain_repo_name_version", table_name="supply_chain_packages")
    op.drop_table("supply_chain_packages")
    op.execute("DROP TYPE ecosystem_enum")
