"""Índice parcial para el barrido de renovación de credenciales Git.

## Por qué esta migración existe

Porque `token_refresh.py` acaba de ganar un barrido que se ejecuta desde Celery Beat y que pregunta
a `git_credentials` lo mismo en cada pasada: **qué credenciales OAuth caducan pronto**. Esa pregunta
no tenía índice: `token_expires_at` no estaba en ninguno, y el único índice de la tabla es
`ix_git_credentials_org_provider`, que no sirve para un `WHERE` por fecha.

Con la tabla de hoy el coste es ridículo —hay una fila por organización y proveedor, así que un
seq scan lee cientos de filas—. Eso es exactamente el argumento que no vale: el barrido corre cada
quince minutos, para siempre, y una consulta secuencial que hoy cuesta milisegundos es la que
aparece en un `pg_stat_statements` cuando hay dos mil tenants. Un índice parcial cuesta una entrada
en el esquema y quita la pregunta.

## Por qué **parcial**

Porque las filas que el barrido puede devolver son exactamente dos condiciones: tener fecha de
caducidad —los tokens personales de acceso la tienen a `NULL` y no caducan— y tener `refresh_token`
con el que renovarse. El predicado del índice es ese mismo filtro, de modo que el índice solo
contiene filas que el `WHERE` puede dejar pasar.

Un índice entero sobre `token_expires_at` también funcionaría, y sería más grande y peor: obligaría
a leer la entrada de cada token personal de acceso para descartarla, que es trabajo hecho para nada
en cada pasada y para siempre.

## Por qué `CREATE INDEX` y no `CONCURRENTLY`

Porque `CONCURRENTLY` no puede ejecutarse dentro de una transacción, y Alembic ejecuta las
migraciones dentro de una. El coste de no usarlo es un `LOCK` de tipo `SHARE` sobre `git_credentials`
durante la creación, que bloquea escrituras pero no lecturas.

Eso en esta tabla es aceptable y conviene decirlo en voz alta: `git_credentials` es diminuta y solo
se escribe cuando alguien conecta un repositorio o renueva su credencial. Si alguna vez la tabla
creciera hasta que ese bloqueo llegara a molestar, la salida es una migración fuera de la transacción
de Alembic, y no un `LOCK` más o menos.

## Reversión

`DROP INDEX` y nada más: no se toca ninguna fila ni ninguna columna, así que la reversión es
simétrica y no hay dato que recuperar.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7a1c5d9f4"
down_revision: str | None = "c3e4f5a6b7c8"
branch_labels: str | None = None
depends_on: str | None = None

NOMBRE_DEL_INDICE = "ix_git_credentials_por_vencer"
PREDICADO = "token_expires_at IS NOT NULL AND encrypted_refresh_token IS NOT NULL"


def upgrade() -> None:
    op.create_index(
        NOMBRE_DEL_INDICE,
        "git_credentials",
        ["token_expires_at"],
        unique=False,
        postgresql_where=sa.text(PREDICADO),
    )


def downgrade() -> None:
    op.drop_index(NOMBRE_DEL_INDICE, table_name="git_credentials")
