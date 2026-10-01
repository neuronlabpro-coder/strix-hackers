"""Añade a la fila única de precios los tres importes comerciales que faltaban.

## Qué hace

La migración anterior (`e3a4b5c6d7e8`) creó `platform_pricing` con los cuatro precios
**escalares** de la plataforma, pero dejó el catálogo comercial a medias: los packs y la
escalera de descuento sí tienen su tabla, y el mínimo de gasto, el tope de gasto y el precio
del plan Pro no tenían dónde vivir. Aquí les da sitio, en la **misma fila**, no en una nueva.

## Por qué en la misma fila y no en otra tabla

Porque no son una lista: son tres números que siempre se leen y se escriben juntos, y que
juntos forman «lo que cuesta una compra» igual que los otros cuatro forman «lo que cuesta
un escaneo». Meterlos en una tabla propia obligaría a un segundo `SELECT` y a un segundo
`UPDATE` para una operación que el operador hace de una vez —«voy a subir el mínimo del pack a
medida»—, y la fila se quedaría a medio cambiar si el segundo fallaba.

Y porque un `UPDATE` de una fila no tiene a quién interpretar: no hay ambigüedad de cuál de las
dos filas es la vigente, que es el problema que obligaría a una columna `is_active` y a una
condición de carrera en una tabla clave-valor.

## Por qué `NOT NULL` sin valor por defecto

Porque el único camino legítimo para crear la fila es la migración, y ese camino está aquí. Un
`server_default` sería un segundo camino silencioso: un `INSERT` que se saltase esta migración
crearía una fila con precios que nadie eligió. Con `NOT NULL` y sin default, ese `INSERT` falla.

## Por qué el `CHECK` de que el máximo sea mayor que el mínimo

Porque son dos números que solo tienen sentido juntos. Un catálogo con un tope por debajo del
mínimo es un rango vacío: `credits_for_spend` devolvería `0` para todas las cantidades y el
panel no ofrecería ninguna compra, sin error en ninguna parte —el esquema respondería bien,
el checkout también, y simplemente no se podría comprar nada.

Y es un `CHECK` de base de datos y no una validación de la API a propósito: la fila puede
escribirse desde la API, desde un script de soporte o desde una corrección a mano, y la
restricción tiene que ser la misma en los tres caminos.

## Por qué la traza de cambios se siembra también con estos tres

Porque una traza que empieza el día de la primera edición tiene un hueco: lo que se cambió
antes de que existiera no queda registrado. La historia del catálogo empieza en su origen.

Con `valor_anterior` a `NULL` otra vez, y por el mismo motivo que en la migración anterior: no
había un valor anterior en la base, lo había en el código, y el asiento dice la verdad con el
`NULL` en vez de mentir con un número que nadie editó en el panel.

## Por qué no se toca nada de lo que ya funciona

Los packs y la escalera ya tienen su tabla y su sembrado, y las constantes del código siguen
leyéndose como valor de arranque. Esta migración **solo añade columnas**. El paso de que la
aplicación lea estas tres columnas es un despliegue posterior, y mientras tanto el valor de
arranque del código manda: es el patrón expand-migrate-contract del proyecto.

## Por qué las columnas se añaden **anulables** y se endurecen después

Porque `platform_pricing` **ya tiene una fila**, sembrada por la migración anterior. Y
Postgres no permite `ADD COLUMN ... NOT NULL` sin valor por defecto sobre una tabla con filas:
lo rechaza con `NotNullViolationError` antes de mover un solo dato. La secuencia correcta es
la de tres pasos que hace esta migración, y es la que impone el patrón
expand-migrate-contract:

1. **Añadir la columna anulable.** La tabla no se reescribe y no hay ventana en la que falte.
2. **Rellenar** las tres columnas con los valores de arranque.
3. **Endurecer a `NOT NULL`.** En PostgreSQL 11+ esto solo consulta la tabla, no la reescribe,
   porque la ausencia de nulos ya está garantizada por el paso anterior.

La primera versión de esta migración añadía las tres columnas directamente como `NOT NULL`. La
base la rechazó —que es exactamente para lo que está una migración de prueba contra datos
reales— y el error fue el queimar de tres pasos.

Y el paso intermedio se nota en la traza: si el relleno se hiciera en el mismo paso 1, una fila
nueva creada entre medias quedaría con nulos y el endurecimiento del paso 3 fallaría. Por eso
van separados.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "f4b5c6d7e8a9"
down_revision: str = "e3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Los tres valores de arranque, escritos a mano y no leídos de la configuración.
#:
#: ## Por qué literales y no `settings`
#:
#: Porque una migración se ejecuta contra **cualquier** base y tiene que ser **reproducible**:
#: el mismo fichero, aplicado a dos bases, tiene que sembrar lo mismo. Si leyera el `.env` del
#: proceso que corre Alembic, aplicar el mismo fichero con dos entornos distintos sembraría
#: precios distintos, y eso no es una migración, es un script.
#:
#: Y porque estos tres números no están en la configuración: viven en
#: `billing/schemas.py` como catálogo versionable, que es donde el proyecto ha decidido que
#: vive el catálogo. Escribirlos aquí es una repetición, y por eso la prueba
#: `test_el_catalogo_de_arranque_coincide_con_el_codigo` vigila que las dos copias no se
#: separen: si alguien cambia la constante del código y no esta migración, la prueba avisa
#: diciendo exactamente qué número se ha quedado viejo.
MOTIVO = "sembrado por la migracion desde el catalogo del codigo"

ARRANQUE_CATALOGO = {
    "custom_spend_minimum_usd": Decimal("10.00"),
    "custom_spend_maximum_usd": Decimal("10000.00"),
    "pro_subscription_monthly_usd": Decimal("29.00"),
}


def upgrade() -> None:
    # ---- paso 1: las columnas, anulables.
    # `nullable=True` porque la tabla tiene una fila y Postgres rechaza `NOT NULL` sin
    # defecto sobre una tabla poblada. Ver el docstring: por qué van en tres pasos.
    op.add_column(
        "platform_pricing",
        sa.Column("custom_spend_minimum_usd", sa.Numeric(18, 2), nullable=True),
    )
    op.add_column(
        "platform_pricing",
        sa.Column("custom_spend_maximum_usd", sa.Numeric(18, 2), nullable=True),
    )
    op.add_column(
        "platform_pricing",
        sa.Column("pro_subscription_monthly_usd", sa.Numeric(18, 2), nullable=True),
    )

    # ---- paso 2: el relleno, con parámetros vinculados y sin interpolar valores, por el mismo
    # motivo que en la migración anterior: con parámetros el valor viaja al servidor como
    # parámetro y no hay concatenación que un lint pueda señalar ni que nadie pueda inyectar.
    vinculo = op.get_bind()
    vinculo.execute(
        sa.text(
            "UPDATE platform_pricing "
            "SET custom_spend_minimum_usd = :minimo, "
            "    custom_spend_maximum_usd = :maximo, "
            "    pro_subscription_monthly_usd = :pro"
        ),
        {
            "minimo": ARRANQUE_CATALOGO["custom_spend_minimum_usd"],
            "maximo": ARRANQUE_CATALOGO["custom_spend_maximum_usd"],
            "pro": ARRANQUE_CATALOGO["pro_subscription_monthly_usd"],
        },
    )

    # ---- paso 3: se endurecen. En PostgreSQL 11+ esto solo consulta la tabla, no la
    # reescribe, porque la ausencia de nulos ya está garantizada por el paso 2.
    for columna in ARRANQUE_CATALOGO:
        op.alter_column("platform_pricing", columna, nullable=False)

    # ---- las restricciones, al final: aplicadas sobre valores ya válidos, de modo que un
    # `CHECK` que rechazara los valores de arranque abortaría la migración aquí y no en
    # silencio con una fila que no cumple.
    op.create_check_constraint(
        "ck_platform_pricing_custom_min_positive",
        "platform_pricing",
        "custom_spend_minimum_usd > 0",
    )
    op.create_check_constraint(
        "ck_platform_pricing_custom_max_positive",
        "platform_pricing",
        "custom_spend_maximum_usd > 0",
    )
    op.create_check_constraint(
        "ck_platform_pricing_custom_range",
        "platform_pricing",
        "custom_spend_maximum_usd >= custom_spend_minimum_usd",
    )
    op.create_check_constraint(
        "ck_platform_pricing_pro_price_positive",
        "platform_pricing",
        "pro_subscription_monthly_usd > 0",
    )

    # Un asiento por cada precio copiado, con la traza append-only ya protegida por el
    # disparador que creó la migración anterior.
    vinculo.execute(
        sa.text(
            "INSERT INTO platform_price_changes "
            "(id, clave, valor_anterior, valor_nuevo, motivo) "
            "VALUES (:id, :clave, NULL, :valor, :motivo)"
        ),
        [
            {"id": uuid.uuid4(), "clave": clave, "valor": valor, "motivo": MOTIVO}
            for clave, valor in ARRANQUE_CATALOGO.items()
        ],
    )


def downgrade() -> None:
    # Al revés, y las restricciones antes que las columnas: una restricción que sobrevive a su
    # columna se queda en la base apuntando a algo que ya no existe, y `DROP COLUMN` no avisa.
    op.drop_constraint("ck_platform_pricing_pro_price_positive", "platform_pricing")
    op.drop_constraint("ck_platform_pricing_custom_range", "platform_pricing")
    op.drop_constraint("ck_platform_pricing_custom_max_positive", "platform_pricing")
    op.drop_constraint("ck_platform_pricing_custom_min_positive", "platform_pricing")
    op.drop_column("platform_pricing", "pro_subscription_monthly_usd")
    op.drop_column("platform_pricing", "custom_spend_maximum_usd")
    op.drop_column("platform_pricing", "custom_spend_minimum_usd")
