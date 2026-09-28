"""Fase 6 · Evento de auditoría para el consumo de chat no liquidado

## Qué hace

Añade un valor al enum `audit_action_enum` de PostgreSQL: `UNREPORTED_USAGE_UNBILLED`.

## Qué evento es

El proveedor de inferencia respondió con un bloque `usage` ausente o con Tokens a cero. Por
decisión contable, ese paso **no se cobra**: se entrega la respuesta al usuario y se guarda el
mensaje con `tokens_in=0`, `tokens_out=0` y `credits_cost=0`.

## Por qué va al rastro de auditoría y no a un `log`

Porque responde a una pregunta que el log no puede responder de forma fiable: **¿cuánto le
costó al cliente la semana pasada, y por qué hay días con cero gasto de chat en los que sí se
usó el chat?**

Un `log` es texto libre que se puede rotar, que no se filtra por organización y que nadie
consulta cuando hay una disputa de factura. Un asiento en el ledger no sirve, porque por
definición no existe: no se cobró. Y el mensaje persistido dice `credits_cost=0` sin explicar
si fue un fallo del proveedor o un cobro real de cero. Este evento es lo que convierte
"cero" en "cero **por esta razón**, en este mensaje, en este instante".

Es el mismo criterio por el que `WEBHOOK_AUTO_DISABLED` está en la tabla: una decisión que
tomó el sistema sin que nadie la pidiera y que solo se responde desde una fuente append-only.

## Por qué `entity_type` es `chat_message` y no `chat_conversation`

Porque el dato se consulta **por mensaje**. El evento dice "este mensaje concreto no se cobró",
y la pregunta de auditoría se hace de un mensaje, no de una conversación entera. Guardarlo
como conversación obligaría a recorrer todos los mensajes para contestar.

## Por qué `from_state` y `to_state` se dejan a `NULL`

Los dos estados de los demás eventos de este enum son el valor anterior y el nuevo de una
transición. Aquí no hay transición: hay una ausencia. Inventar un par para rellenar columnas
que existen para otra cosa es la forma de que un `NULL` acabe significando "paso gratis" en un
sitio y "no cobrado por falta de `usage`" en otro. Se deja a `NULL` y el nombre del evento
carga con el significado.

## Por qué el `downgrade` no deshace nada

Porque PostgreSQL 16 no tiene `ALTER TYPE ... DROP VALUE`. Quitar el valor exigiría recrear el
tipo, renombrar la columna, recrear la tabla y copiar las filas de un rastro que R4 declara
inmutable. Un valor de más en el enum es inocuo: no impide insertar ni consultar los demás, y
`alembic check` sigue limpio porque compara contra el enum de la base, que ya lo tiene.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Se declara sin `IF NOT EXISTS` por el mismo motivo que en las migraciones anteriores del
#: enum: un valor mal escrito pasaria en silencio en una base ya aplicada, y el `downgrade`
#: parcial de un `downgrade` honesto es peor que un fallo ruidoso.
ACCION: str = "UNREPORTED_USAGE_UNBILLED"


def upgrade() -> None:
    # En su propia sentencia, sin agrupar con otras. `ALTER TYPE ... ADD VALUE` no puede
    # usarse en la misma transaccion que lo anade, y aunque esta migracion no inserta ninguna
    # fila con el valor nuevo, dejar la sentencia aislada evita que quien anada una segunda
    # accion aqui dentro descubra el limite por la via del error.
    op.execute(f"ALTER TYPE audit_action_enum ADD VALUE '{ACCION}'")


def downgrade() -> None:
    """No deshace nada, y lo dice.

    PostgreSQL 16 no tiene `ALTER TYPE ... DROP VALUE`. Revertirlo exigiría recrear el tipo
    sobre una tabla de auditoría que R4 declara inmutable, copiar sus filas y volver a
    enlazarla. El enum se queda con un valor de más, que es el estado inocuo que ya comparten
    los valores de las migraciones anteriores.
    """
