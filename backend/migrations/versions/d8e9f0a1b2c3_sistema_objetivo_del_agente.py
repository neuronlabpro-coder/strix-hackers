"""Añade `sistema_objetivo` al agente: el sistema para el que el operador dice desplegarlo.

## Por qué una columna y no reusar `platform_hint`

Porque `platform_hint` lo **mide** el agente cuando se conecta, y hasta que se conecta está a
`NULL`. En la tabla del panel, un alta recién hecha salía con «—» en la columna de plataforma:
justo en el momento en que la pregunta del operador es «¿esto funciona en su Windows Server?»,
sin ninguna forma de contestarla porque la casilla que lo resolvería no existe.

## Por qué se llama `sistema_objetivo` y no `sistema`

Porque no es un hecho medido. El agente puede declarar `windows` y_connectar en una máquina que
`platform.system()` reporta como `Linux`, por ejemplo. Y el par de columnas queda explícito:

| Columna             | Quién la escribe    | Para qué                                  |
| :------------------ | :------------------ | :---------------------------------------- |
| `sistema_objetivo`  | el operador, al alta | elegir las instrucciones de despliegue    |
| `platform_hint`     | el agente, latido   | diagnosticar cuando algo va mal           |

Que el medido no sobrescriba al declarado es deliberado: que un operador se equivocara al
declararlo no es un error de la plataforma, y borrar su respuesta dejaría la fila sin la única
pista con la que empezar.

## Por qué `server_default` y no solo el default de Pydantic

Porque la columna es `NOT NULL`. Un default en el modelo de Pydantic solo cubre los inserts que
pasan por el esquema, y un agente puede backersearse desde el script de demostración o desde
cualquier otra ruta que escriba en la tabla sin pasar por el esquema. Con el default en el
servidor, esas filas nacen con `desconocido` en vez de fallar con un `NOT NULL` que no explica
nada.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `c2d3e4f5a6b7` es la cabeza de la cadena en el momento de escribir esto, según
#: `alembic heads`. No se dedujo del nombre del fichero: hay treinta y pico revisiones y varios
#: `f…` que no son la última. El gate `alembic check` avisa si la cadena no encaja, y lo avisa
#: diciendo que hay dos cabezas, que es la forma más lenta de descubrir un `down_revision`
#: inventado.
COLUMNA = "sistema_objetivo"
VALOR_POR_DEFECTO = "desconocido"
#: Tamaño del `VARCHAR`. El enum más largo es `desconocido` (11), pero se deja margen para que
#: añadir un sistema no obligue a una migración solo por el ancho.
LARGO = 16


def upgrade() -> None:
    op.add_column(
        "scanner_agents",
        sa.Column(
            COLUMNA,
            sa.String(length=LARGO),
            nullable=False,
            server_default=VALOR_POR_DEFECTO,
        ),
    )
    # El `server_default` se quita después de rellenarla, por el motivo de siempre: dejar un
    # default permanente en la base significa que un INSERT futuro que se olvide del campo
    # no falla: se queda probado en silencio. La columna sigue siendo NOT NULL, que es lo que
    # quiere el modelo; lo que no quiere es que el valor aparezca solo.
    # La sentencia va **entera y literal**, no con un f-string sobre las constantes de arriba.
    #
    # ## Por qué
    #
    # Porque no hay nada que interpolar: la columna y el valor son fijos, y la razón de ese
    # `UPDATE` es que la columna nace NOT NULL con default, así que las filas anteriores ya
    # tienen valor y esta sentencia solo protege el caso de que alguien la haya creado a mano
    # sin default. Es una red, no un camino.
    #
    # ## Por qué no `VALOR_POR_DEFECTO` en la sentencia
    #
    # Porque si alguien cambia la constante y no esta línea, la migración sigue pareciendo
    # correcta y rellena con el valor viejo. Escribir el literal en los dos sitios hace que la
    # diferencia se vea en el `git diff`, que es donde se mira.
    op.execute("UPDATE scanner_agents SET sistema_objetivo = 'desconocido' WHERE sistema_objetivo IS NULL")
    op.alter_column(
        "scanner_agents",
        COLUMNA,
        server_default=None,
        existing_type=sa.String(length=LARGO),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.drop_column("scanner_agents", COLUMNA)
