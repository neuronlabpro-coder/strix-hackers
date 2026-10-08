"""La telemetría real del proveedor y los topes de coste administrables.

## Qué añade

### En `llm_model_configs`

| Columna                | Qué es                                                                 |
| :--------------------- | :--------------------------------------------------------------------- |
| `cached_input_cost_m`  | Precio por 1M de los tokens de entrada que el proveedor sirvió de **su** caché |
| `provider`             | Quién publica el modelo                                                 |
| `context_limit_tokens` | Ventana de contexto que declara el modelo, si la declara               |
| `output_limit_tokens`  | Tokens de salida que declara el modelo, si los declara                 |

### En `llm_usage_events`

| Columna                         | Qué es                                                          |
| :------------------------------ | :-------------------------------------------------------------- |
| `cached_tokens`                 | Tokens de entrada que salieron de la caché del proveedor         |
| `cache_price_published`         | ¿El catálogo declara precio de caché para ese modelo?            |
| `provider`                      | Proveedor, copiado del catálogo al escribir el evento             |
| `provider_cost_usd`             | Lo que el proveedor **facturó** de verdad                        |
| `estimated_provider_cost_usd`   | Lo que el catálogo **calcula** con el precio de caché             |
| `duration_seconds`              | Cuánto duró la ejecución                                        |
| `budget_max_usd`                | El tope con el que se lanzó el run, ya resuelto                 |
| `budget_consumed_usd`           | Cuánto de ese tope se consumió                                   |

### La tabla `llm_cost_limits`

Los topes de gasto del proveedor en los cuatro niveles de resolución, con vigencia por fechas.

## Por qué `cached_input_cost_m` es la columna que faltaba

Porque el catálogo tenía **un solo** precio de entrada y el proveedor factura **dos**. Medido sobre
el run real de este proyecto, `mindguard-site_23ee`, con `z-ai/glm-5.3`:

| Magnitud                                          | Valor            |
| :------------------------------------------------ | :--------------- |
| `input_tokens`                                    | 16.819.076       |
| `input_tokens_details[0].cached_tokens`          | **16.443.264** (97,8 %) |
| `output_tokens`                                   | 46.417           |
| `run.json.llm_usage.cost`                         | **1,84830111 USD** |
| Catálogo con **un** precio de entrada (0,40 USD/M)| **6,80189760 USD** |
| Catálogo **con** precio de caché (0,10 USD/M)    | **1,86891840 USD** |

Con la caché al precio de la entrada, la estimación es **3,68 veces** lo que el proveedor cobró de
verdad. Con el precio de caché declarado, queda a un **1,1 %**.

El precio de caché de **0,10 USD/M** no es un número inventado aquí: es el precio efectivo que se
desprende del propio artefacto. Los tres proveedores que sirvieron el run cobran precios distintos,
así que el 0,10 es la **mezcla ponderada** de los tres. Lo que hace esta migración es **poder
declararlo**, no deducirlo: el precio de cada proveedor es un dato de la plataforma y lo escribe
quien la administra.

## Por qué `NULL` y no cero en `cached_input_cost_m`

Porque son dos afirmaciones distintas y el coste depende de la diferencia:

- `NULL`: el proveedor **no publica** precio de caché. La caché se valora al precio de la entrada
  normal y `cache_price_published` queda en `false`.
- `0`: el proveedor publica precio de caché y es cero. Es un dato, no una ausencia.

Y hay un tercer caso que `NULL` **sí** representa y que no es ninguno de los dos: un modelo al que
**nunca se le pidió caché**. Da el mismo importe que el primero, y por eso el estado
`cache_price_published` tiene que viajar al lado del importe.

## Por qué `estimated_provider_cost_usd` **no** duplica `base_cost_usd`

Porque no son el mismo número y por eso los dos hacen falta:

- `base_cost_usd` es la base sobre la que se aplica el recargo al **cliente**. Su fórmula **no se
  toca**: el importe está sellado en el `credit_ledger`, que es *append-only* (R4), y cambiarlo sería
  cambiar lo que se cobra al cliente, que es una decisión comercial y no un efecto de hacer visible
  una medición.
- `estimated_provider_cost_usd` es el mismo consumo valorado **con el precio de caché declarado**, y
  es el número comparable con `provider_cost_usd`.

Si fueran el mismo, se habría escrito una columna y no dos. Y si se hubiera cambiado `base_cost_usd`
para meterle la caché, el precio de todos los escaneos habría bajado solo, sin que nadie lo decidiera.

## Por qué la tabla de topes tiene vigencia por fechas y no se actualiza

Porque la pregunta «¿por qué este run tuvo un tope de 3,00 y el otro de 25?» solo tiene respuesta si
la política que estaba vigente cuando se lanzó **sigue existiendo**. Un `UPDATE` de `max_budget_usd`
reescribe la historia de todos los runs que lo usaron.

Por eso el cambio se inserta y el anterior se cierra con `valid_until`, y por eso la resolución
compara con el reloj **al leer**: un tope con `valid_from` en el futuro existe pero todavía no
manda, que es el caso normal de un aviso de renovación.

## Lo que **no** toca esta migración

- **`credit_ledger`.** Ni una columna, ni un índice, ni un trigger. Es *append-only* (R4) y esta
  migración no lo lee para escribir: escribe en `llm_usage_events` y `pentest_runs`, que es
  telemetría.
- **Los precios comerciales.** Ni `credits_per_usd`, ni `scan_credit_cost`, ni
  `pro_subscription_monthly_usd`, ni los packs, ni los tramos de volumen.
- **`llm_model_configs.base_cost_input_m` y `base_cost_output_m`.** Siguen siendo los que son: lo que
  se cobra al cliente no se recalcula.

## Reversión

`DROP TABLE llm_cost_limits` y `DROP COLUMN` de las doce. Sin efectos sobre `credit_ledger` ni sobre
`pentest_runs`: la reversión pierde la telemetría del proveedor y las políticas de topes, y ningún
asiento.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d1f4a8b2c6e7"
branch_labels: str | None = None
depends_on: str | None = None

COST_PRECISION = sa.Numeric(precision=18, scale=8)

NIVEL_LIMITE_COSTE_ENUM = postgresql.ENUM(
    "ORGANIZACION",
    "OPERACION",
    "PLAN",
    "GLOBAL",
    name="nivel_limite_coste_enum",
)
OPERACION_COSTE_ENUM = postgresql.ENUM(
    "PENTEST_QUICK",
    "PENTEST_DEEP",
    "PR_REVIEW",
    "CHAT",
    name="operacion_coste_enum",
)

COLUMNAS_DE_MODEL_CONFIG = (
    sa.Column(
        "cached_input_cost_m",
        COST_PRECISION,
        nullable=True,
        comment=(
            "Precio por 1M de la entrada que el proveedor sirvio de su cache. "
            "NULL = el proveedor no publica precio de cache"
        ),
    ),
    sa.Column("provider", sa.String(length=64), nullable=True, comment="Proveedor que publica el modelo"),
    sa.Column(
        "context_limit_tokens",
        sa.Integer(),
        nullable=True,
        comment="Tokens de contexto que declara el modelo. NULL si el proveedor no lo publica",
    ),
    sa.Column(
        "output_limit_tokens",
        sa.Integer(),
        nullable=True,
        comment="Tokens de salida que declara el modelo. NULL si el proveedor no lo publica",
    ),
)

CHECKS_DE_MODEL_CONFIG = (
    sa.CheckConstraint(
        "cached_input_cost_m IS NULL OR cached_input_cost_m >= 0",
        name="ck_llm_model_configs_cached_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "context_limit_tokens IS NULL OR context_limit_tokens > 0",
        name="ck_llm_model_configs_context_positive",
    ),
    sa.CheckConstraint(
        "output_limit_tokens IS NULL OR output_limit_tokens > 0",
        name="ck_llm_model_configs_output_limit_positive",
    ),
)

COLUMNAS_DE_USO = (
    sa.Column(
        "cached_tokens",
        sa.Integer(),
        nullable=True,
        comment="Entrada que el proveedor sirvio de su cache. NULL si el motor no lo publica",
    ),
    sa.Column(
        "cache_price_published",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
        comment="El catalogo declara un precio de cache para el modelo de este evento",
    ),
    sa.Column(
        "provider",
        sa.String(length=64),
        nullable=True,
        comment="Proveedor que publica el modelo, copiado del catalogo al escribir el evento",
    ),
    sa.Column(
        "provider_cost_usd",
        COST_PRECISION,
        nullable=True,
        comment="Coste que el proveedor cobro de verdad, de run.json.llm_usage.cost",
    ),
    sa.Column(
        "estimated_provider_cost_usd",
        COST_PRECISION,
        nullable=True,
        comment="Coste del proveedor estimado por el catalogo, con el precio de cache declarado",
    ),
    sa.Column("input_cost_usd", COST_PRECISION, nullable=True),
    sa.Column("cached_input_cost_usd", COST_PRECISION, nullable=True),
    sa.Column("output_cost_usd", COST_PRECISION, nullable=True),
    sa.Column(
        "duration_seconds",
        sa.Numeric(precision=12, scale=3),
        nullable=True,
        comment="Duracion de la ejecucion en segundos, segun el motor",
    ),
    sa.Column(
        "budget_max_usd",
        COST_PRECISION,
        nullable=True,
        comment="Tope de gasto del proveedor que se aplico al run, ya resuelto",
    ),
    sa.Column(
        "budget_consumed_usd",
        COST_PRECISION,
        nullable=True,
        comment=(
            "Consumo del proveedor contra el tope: el importe real, "
            "o la estimacion si no se publico"
        ),
    ),
)

CHECKS_DE_USO = (
    sa.CheckConstraint(
        "cached_tokens IS NULL OR cached_tokens >= 0", name="ck_llm_usage_cached_nonnegative"
    ),
    sa.CheckConstraint(
        "provider_cost_usd IS NULL OR provider_cost_usd >= 0",
        name="ck_llm_usage_provider_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "estimated_provider_cost_usd IS NULL OR estimated_provider_cost_usd >= 0",
        name="ck_llm_usage_estimated_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "input_cost_usd IS NULL OR input_cost_usd >= 0",
        name="ck_llm_usage_input_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "cached_input_cost_usd IS NULL OR cached_input_cost_usd >= 0",
        name="ck_llm_usage_cached_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "output_cost_usd IS NULL OR output_cost_usd >= 0",
        name="ck_llm_usage_output_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "budget_max_usd IS NULL OR budget_max_usd > 0", name="ck_llm_usage_budget_max_positive"
    ),
    sa.CheckConstraint(
        "budget_consumed_usd IS NULL OR budget_consumed_usd >= 0",
        name="ck_llm_usage_budget_consumed_nonnegative",
    ),
)


def upgrade() -> None:
    # Los dos enums se crean **antes** que la tabla que los usa. `plan_tier_enum` ya existe: lo crea
    # la migración de la tabla de organizaciones y esta reutiliza ese mismo tipo, porque un enum
    # copiado con los mismos valores se queda viejo el día que se añada un plan.
    NIVEL_LIMITE_COSTE_ENUM.create(op.get_bind(), checkfirst=True)
    OPERACION_COSTE_ENUM.create(op.get_bind(), checkfirst=True)

    for columna in COLUMNAS_DE_MODEL_CONFIG:
        op.add_column("llm_model_configs", columna)
    for check in CHECKS_DE_MODEL_CONFIG:
        op.create_check_constraint(str(check.name), "llm_model_configs", check.sqltext)

    for columna in COLUMNAS_DE_USO:
        op.add_column("llm_usage_events", columna)
    for check in CHECKS_DE_USO:
        op.create_check_constraint(str(check.name), "llm_usage_events", check.sqltext)
    op.create_index(
        "ix_llm_usage_org_created",
        "llm_usage_events",
        ["organization_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "llm_cost_limits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scope",
            # `create_type=False` porque los dos enums los crea el `upgrade` de forma explícita y
            # con `checkfirst=True`. Sin esta marca, `postgresql.ENUM` intenta crearlos otra vez al
            # crear la tabla y el despliegue falla con «el tipo ya existe».
            postgresql.ENUM(
                "ORGANIZACION",
                "OPERACION",
                "PLAN",
                "GLOBAL",
                name="nivel_limite_coste_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "operation",
            postgresql.ENUM(
                "PENTEST_QUICK",
                "PENTEST_DEEP",
                "PR_REVIEW",
                "CHAT",
                name="operacion_coste_enum",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            # `create_type=False` también aquí, y por el motivo opuesto: este enum **ya existe** y lo
            # crea la migración de la tabla de organizaciones. Reutilizarlo es lo que hace que un
            # plan nuevo valga para las dos columnas a la vez.
            "plan_tier",
            postgresql.ENUM(
                "FREE", "PRO", "ENTERPRISE", name="plan_tier_enum", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("max_budget_usd", COST_PRECISION, nullable=True),
        sa.Column("max_turns", sa.Integer(), nullable=True),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "max_budget_usd IS NOT NULL OR max_turns IS NOT NULL",
            name="ck_llm_cost_limits_al_menos_un_tope",
        ),
        sa.CheckConstraint(
            "max_budget_usd IS NULL OR max_budget_usd > 0",
            name="ck_llm_cost_limits_budget_positive",
        ),
        sa.CheckConstraint("max_turns IS NULL OR max_turns > 0", name="ck_llm_cost_limits_turns_positive"),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_llm_cost_limits_vigencia_coherente",
        ),
        sa.CheckConstraint(
            "(scope = 'ORGANIZACION') = (organization_id IS NOT NULL)",
            name="ck_llm_cost_limits_organizacion_coherente",
        ),
        sa.CheckConstraint(
            "(scope = 'OPERACION') = (operation IS NOT NULL)",
            name="ck_llm_cost_limits_operacion_coherente",
        ),
        sa.CheckConstraint(
            "(scope = 'PLAN') = (plan_tier IS NOT NULL)", name="ck_llm_cost_limits_plan_coherente"
        ),
    )
    op.create_index(
        "ix_llm_cost_limits_organizacion",
        "llm_cost_limits",
        ["organization_id", "scope"],
        unique=False,
    )
    op.create_index(
        "ix_llm_cost_limits_alcance",
        "llm_cost_limits",
        ["scope", "operation", "plan_tier"],
        unique=False,
    )
    op.create_index(
        "ix_llm_cost_limits_created_by", "llm_cost_limits", ["created_by"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_llm_cost_limits_created_by", table_name="llm_cost_limits")
    op.drop_index("ix_llm_cost_limits_alcance", table_name="llm_cost_limits")
    op.drop_index("ix_llm_cost_limits_organizacion", table_name="llm_cost_limits")
    op.drop_table("llm_cost_limits")
    OPERACION_COSTE_ENUM.drop(op.get_bind(), checkfirst=True)
    NIVEL_LIMITE_COSTE_ENUM.drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_llm_usage_org_created", table_name="llm_usage_events")
    for check in CHECKS_DE_USO:
        op.drop_constraint(str(check.name), "llm_usage_events", type_="check")
    for columna in COLUMNAS_DE_USO:
        op.drop_column("llm_usage_events", columna.name)

    for check in CHECKS_DE_MODEL_CONFIG:
        op.drop_constraint(str(check.name), "llm_model_configs", type_="check")
    for columna in COLUMNAS_DE_MODEL_CONFIG:
        op.drop_column("llm_model_configs", columna.name)
