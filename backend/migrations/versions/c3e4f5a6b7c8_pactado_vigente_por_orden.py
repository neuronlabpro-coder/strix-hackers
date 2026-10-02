"""Que el pactado vigente lo decida el orden, y no un cierre que el append-only prohíbe.

## Qué estaba roto

Dos reglas de `a7c9e2f41563` se contradecían, y la contradicción solo se ve al intentar usarlas:

1. `organization_price_overrides` tiene un disparador `BEFORE UPDATE OR DELETE OR TRUNCATE`, así
   que **`valido_hasta` no se puede escribir después del `INSERT`**. Nunca. Un override abierto se
   queda abierto para siempre.
2. `uq_org_price_override_vigente` era un índice **único** parcial sobre
   `(organization_id, operacion, alcance) WHERE valido_hasta IS NULL`, o sea que solo admitía un
   abierto por operación y organización.

Juntas dan: **el primer pactado de un cliente es inmodificable para siempre**. El comercial pacta
3 créditos, y todo lo que la plataforma puede cobrarle a ese cliente mientras exista esa fila son
3 créditos, sin ruta de salida. La única forma de cambiarlo sería desactivar el disparador, que
es justo lo que R4 prohíbe.

## Qué estaba roto dos veces

El índice único **no protegía nada**, ni siquiera eso. `alcance` es nullable y en PostgreSQL los
`NULL` se consideran distintos entre sí dentro de un índice único, así que dos pactados abiertos de
`SCAN_CREDIT_COST` —que es la operación más usada y la única cuyo `alcance` es `NULL`— convivían sin
que el índice dijera nada. El que ganaba era el último en escribirse, según el orden en que
`cargar_overrides` los recorría. O sea: la regla «un vigente por operación» estaba garantizada por
la suerte, no por el esquema.

## Por qué la corrección es quitar la restricción y no aflojarla

Se podría haber permitido un `UPDATE` que solo toque `valido_hasta`, pero el disparador es
`FOR EACH STATEMENT`: no ve qué columnas cambian, así que habría que pasarlo a `FOR EACH ROW` y
escribir una comparación explícita en el `WHEN`. Eso abre la puerta a que el mismo disparador acabe
permitiendo otra cosa el día que alguien lo toque. Y, sobre todo, hace falta para nada: con
append-only, **sustituir un precio no es cerrar el anterior, es escribir uno más nuevo**.

## Por qué el vigente es el de `valido_desde` más reciente

Porque es el orden natural de un compromiso: el último pactado manda, y el anterior sigue ahí
como histórico. No necesita ninguna escritura adicional para quedar «cerrado»: simplemente hay uno
más nuevo que empieza a regir. Y como el filtro compara con el reloj **al leer**, un pactado con
`valido_desde` en el futuro no aplica hasta su fecha, que es exactamente el caso de la renovación
con subida pactada por adelantado.

## Por qué se conserva un índice, y por qué parcial

Porque hay dos lecturas distintas y las dos las quieren las mismas columnas:

- La **carga de la caché** recorre la tabla entera y quiere solo las filas sin `valido_hasta`. Un
  índice parcial sobre ese predicado las localiza sin leer las cerradas, que son las que más crecen.
- La **resolución** de un pactado concreto filtra por organización y operación y quiere el más
  reciente primero, así que `valido_desde DESC` devuelve el ganador sin ordenación.

Un único índice parcial cubre las dos. Y al no ser único, ya no importa que `alcance` sea nullable:
deja pasar los dos casos, que es lo que queremos.

## Qué NO hace esta migración

No toca ninguna fila de `organization_price_overrides`. No desactiva el disparador. No elimina
precios. Cambia un índice por otro, y la resolución del vigente pasa a apoyarse en el orden, que
es la parte que estaba sin cubrir.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e4f5a6b7c8"
down_revision: str | None = "a7c9e2f41563"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Se suelta el único antes de crear el nuevo, porque los dos indices cubren las mismas
    # columnas y no pueden coexistir con nombres distintos.
    op.drop_index("uq_org_price_override_vigente", table_name="organization_price_overrides")

    op.create_index(
        "ix_org_price_override_abiertos",
        "organization_price_overrides",
        ["organization_id", "operacion", sa.text("valido_desde DESC")],
        unique=False,
        postgresql_where=sa.text("valido_hasta IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_org_price_override_abiertos", table_name="organization_price_overrides")
    op.create_index(
        "uq_org_price_override_vigente",
        "organization_price_overrides",
        ["organization_id", "operacion", "alcance"],
        unique=True,
        postgresql_where=sa.text("valido_hasta IS NULL"),
    )
