"""Fase 5 · Nuevas acciones de auditoría de organización y miembros

## Qué hace

Añade tres valores al enum `audit_action_enum` de PostgreSQL: `ORGANIZATION_RENAMED`,
`MEMBER_ROLE_CHANGED` y `MEMBER_REMOVED`.

`ALTER TYPE ... ADD VALUE` es la operación de esquema más restringida que existe en
PostgreSQL, y sus límites son los que marcan las dos decisiones de este archivo:

1. **Se añade en su propia transacción.** No se puede meter un `ALTER TYPE` con otros
   cambios en la misma transacción de Alembic, porque PostgreSQL no permite usar el valor
   nuevo hasta que la transacción que lo añadió se confirme. Una migración que creara la
   tabla y añadiera el enum no podría rellenarla con el valor nuevo en el mismo `upgrade`.

2. **Es irreversible sin rodeos.** No hay `DROP VALUE` en PostgreSQL 16. Por eso
   el `downgrade` no puede deshacerlo, y en vez de fingir que sí, lo dice y deja el enum con
   un valor de más. Es la misma situación que ya existía con los valores anteriores del
   enum, y es inocua: un valor extra en un enum no impide insertar ni consultar los demás.

## Por qué `ADD VALUE` sin `IF NOT EXISTS`

`ADD VALUE IF NOT EXISTS` existe, pero con él un valor mal escrito en la migración pasaría
silenciosamente en una base ya aplicada y en una nueva daría error. Sin el `IF NOT EXISTS`,
la migración falla ruidosamente si el valor ya está, que es lo que revela un `downgrade`
imparcial. Se prefiere el fallo.

## Por qué los valores van al final

`ADD VALUE` sin `BEFORE`/`AFTER` añade al final de la lista. El orden de un enum no
significa nada en el modelo —se compara por etiqueta, no por posición—, así que añadir al
final no altera ninguna consulta existente. Reordenar exigiría recrear el tipo, lo que en
una tabla de auditoría que nunca se vacía es innecesario.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Los tres valores nuevos, en el orden en que se añaden.
NUEVAS_ACCIONES = (
    "ORGANIZATION_RENAMED",
    "MEMBER_ROLE_CHANGED",
    "MEMBER_REMOVED",
)


def upgrade() -> None:
    # Un `ALTER TYPE` por sentencia y sintransactiono agrupado: cada uno es su propia
    # transacción implícita dentro de la de Alembic, y PostgreSQL exige que el valor esté
    # confirmado antes de poder usarse en un `INSERT`. En el `upgrade` de esta migración no
    # se inserta ninguno —los endpoints que los usan llegan después—, así que la limitación
    # no se nota aquí; se documenta para quien añada una cuarta acción e intente
    # rellenar la tabla en la misma migración.
    for accion in NUEVAS_ACCIONES:
        op.execute(f"ALTER TYPE audit_action_enum ADD VALUE '{accion}'")


def downgrade() -> None:
    """No deshace nada, y lo dice.

    PostgreSQL 16 no tiene `ALTER TYPE ... DROP VALUE`. Quitar estos valores exigiría
    recrear el tipo, renombrar la columna, recrear la tabla y copiar las filas —y
    `audit_log` es **append-only por R4**: sus triggers bloquean el `DELETE` y el `DROP` de
    cualquier fila, y esa protección existe para que el rastro tenga validez probatoria.
    Reconstruir la tabla para deshacer una migración no es una operación que se vaya a hacer
    en una base con rastro de auditoría real, ni de noche.

    Lo que se deja es un enum con tres valores más de los que el código anterior usaba. Es
    inocuo: los endpoints de organización no existen en la revisión anterior, así que
    ninguna fila insertada entonces lleva estos valores, y nada que quedara escrito
    referenciándolos se ve afectado.
    """

    # `pass` con esta docstring es intencionado, no un `TODO` olvidado. Un
    # `raise NotImplementedError` haría que `alembic downgrade` no pudiera ejecutarse, y un
    # despliegue que necesita volver atrás quedaría atascado por una migración cuya
    # operación es imposible. Es preferible que el `downgrade` funcione y deje un valor
    # inerte a que no funcione.
    return None
