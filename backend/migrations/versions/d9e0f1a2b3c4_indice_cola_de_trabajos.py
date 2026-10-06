"""Índice compuesto para la cola de trabajos del tenant, en `agent_jobs`.

## Qué corrige

`listar_trabajos` y `resumen_agente`, en `agents/service.py`, filtran por `organization_id` y
ordenan por `created_at DESC`, desempata por `id`. Los índices de `agent_jobs` que había son
`(organization_id, status)`, `(status, priority_order, created_at)`, `(status, lease_expires_at)`
y `(agent_id, status)`.

**Ninguno** cubre esa consulta: el que empieza por `organization_id` —`ix_agent_jobs_org_status`—
termina en `status`, así que no puede dar el orden, y los otros empiezan por `status` o por
`agent_id`. El desempate por `id` que se añadió a esas dos funciones es correcto y no es la causa
de nada: el hueco es anterior, y este índice lo cierra.

## Medido, y por qué este sí

Medido con `EXPLAIN (ANALYZE, BUFFERS)`, mediana de siete ejecuciones con la primera descartada,
sobre 52.000 trabajos repartidos en 2.001 tenants y un tenant que pregunta con 2.000:

| Consulta | Antes | Después |
| :--- | :--- | :--- |
| `listar_trabajos` (`limit 20`) | `Sort <- Bitmap Heap Scan`, **1,305 ms**, 32 buffers | `Index Scan`, **0,079 ms**, 4 buffers |
| `resumen_agente` (`limit 200`) | `Sort <- Bitmap Heap Scan`, **1,328 ms**, 32 buffers | `Index Scan`, **0,165 ms**, 8 buffers |

Dieciséis veces y ocho veces. Y el `LIMIT` es la mitad del motivo por el que esto funciona: con
tope, el índice puede dejar de leer después de las primeras filas. Sin tope no podría, porque
habría que leerlas todas igual.

## Por qué aquí sí y en `webhook_endpoints` no

Porque `list_webhooks` **no pagina**: devuelve las filas enteras del tenant, sin `LIMIT`. Medido
en 29.505 endpoints repartidos en 3.000 tenants, con un tenant de 5, de 500 y de 20.000: el plan
es un `Sort` **igual con y sin** el índice `(organization_id, created_at, id)`, con los mismos
buffers, y el coste estimado sale incluso **mayor** con el índice de tres columnas, que es más
ancho. Sin tope hay que leer todas las filas del tenant, y ordenar unas cientos en memoria sale
más barato que leerlas en orden de índice. Por eso el índice de `webhook_endpoints` se queda como
está y esta migración no lo toca: un índice de más es escritura de más en cada alta de endpoint,
y para nada.

La lección de esa medición, escrita aquí porque es la que justifica el método: **una medición de
plan sin la distribución de filas no mide nada**. Una primera corrida, con un tenant de 100.000
filas y el resto de la tabla repartida en otro, dio un resultado que después no se reprodujo con
la distribución real. El plan depende de cuántas filas hay que leer, así que la tabla tiene que
parecer una plataforma con muchos clientes y ninguno enorme.

## Lo que NO se sustituye

`ix_agent_jobs_org_status` sigue sirviendo el filtro por estado, que es otra consulta —la
pantalla de trabajos filtrada por `QUEUED`—, y el planificador lo elige para ella. Quitarlo sería
tirar un índice que funciona. Los otros tres tampoco se tocan: son de la cola del agente, del
reaper y del reclamo de trabajo, y no tienen nada que ver con la vista del tenant.

## Por qué `CREATE INDEX` y no `CONCURRENTLY`

Porque `CONCURRENTLY` no puede ejecutarse dentro de una transacción, y Alembic ejecuta las
migraciones dentro de una. El coste es un `LOCK` de tipo `SHARE` durante la creación, que bloquea
escrituras y no lecturas.

Y aquí conviene decirlo en voz alta, porque `agent_jobs` es la tabla que más escribe el sistema:
una fila por cada trabajo de escaneo, y los trabajos **nacen a ráfaga** —un webhook con cinco
eventos encola cinco trabajos en la misma transacción—. El `LOCK` dura lo que tarde en
construirse el índice sobre toda la tabla. Hoy eso son segundos sobre una tabla de miles de filas,
que es aceptable. Si la tabla creciera hasta que ese bloqueo molestara, la salida es una migración
fuera de la transacción de Alembic con `CREATE INDEX CONCURRENTLY`, y no un `LOCK` más o menos.
Conviene saber que la puerta existe y que es lo que habría que hacer, en vez de subir el tiempo de
bloqueo de una migración que se ejecuta una vez.

## Reversión

`DROP INDEX` y nada más: no se toca ninguna fila ni ninguna columna, así que la reversión es
simétrica y no hay dato que recuperar.
"""

from __future__ import annotations

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "b3e7a1c5d9f4"
branch_labels: str | None = None
depends_on: str | None = None

#: El índice que se añade en `agent_jobs`. **No** sustituye a ninguno: `ix_agent_jobs_org_status`
#: sigue siendo el del filtro por estado, que es otra consulta, y el planificador lo elige para
#: esa.
INDICE = "ix_agent_jobs_org_created_id"
COLUMNAS = ["organization_id", "created_at", "id"]


def upgrade() -> None:
    op.create_index(INDICE, "agent_jobs", COLUMNAS, unique=False)


def downgrade() -> None:
    op.drop_index(INDICE, table_name="agent_jobs")
