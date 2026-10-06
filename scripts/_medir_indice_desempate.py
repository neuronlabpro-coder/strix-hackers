"""Medición del `EXPLAIN` de los dos listados paginados cuyo desempate se ha añadido.

## Por qué estos dos y no `webhook_endpoints`

Porque `list_webhooks` **no pagina**: devuelve las filas enteras, así que el planificador puede
elegir `Sort` sin miedo y el desempate no le quita nada. Un `LIMIT` es lo que cambia: con
`LIMIT`, el índice que da el orden en verdad **evita trabajo**, porque se puede dejar de leer
después de las primeras filas. Ahí es donde un `ORDER BY ..., id` añadido puede tumbar el plan.

Los dos medidos:

- `GET /api/v1/webhooks/{id}/deliveries`, en `webhooks/router.py`: `created_at DESC, attempt
  DESC` y ahora un tercer criterio. Índice actual: `(endpoint_id, created_at)`.
- `GET /api/v1/agents/jobs`, en `agents/service.py`: `created_at DESC` y ahora `id DESC`.
  Índices actuales: `(organization_id, status)` y los de la cola.

De cada uno se mide el plan **con** y **sin** desempate, en el mismo estado del árbol, para que
la comparación sea de la fila a la fila.

## Por qué se repite y se descarta la primera

Porque la primera ejecución de una consulta sobre datos recién sembrados paga el calor de
caché y el primeiro contacto con cada página, y eso no es coste de la consulta. Un número de
una sola ejecución mide dos cosas y solo informa de una. Se mide `REPETICIONES` veces y se
informa **la mediana**, y además se imprimen todas para que se vea la dispersión: si dos
consultas que deberían costar lo mismo difieren un factor de dos entre ejecuciones, el número
no mide el plan y no sirve para comparar nada.

## Sin ensuciar la base

Todo dentro de una transacción que se revierte; `CREATE INDEX` también se deshace con
`ROLLBACK`. La base de demostración queda exactamente como estaba.

Uso:

    $env:DB_CONNECT_TIMEOUT_SECONDS = "60"
    $env:PYTHONPATH = "."
    uv run --project backend python scripts/_medir_indice_desempate.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import uuid

from sqlalchemy import text

from backend.core.database import engine

#: Entregas de un endpoint. Un historial de webhook llega a cientos, no a miles.
ENTREGAS = 20_000

#: Trabajos de un tenant. La cola de escaneos de un tenant grande.
TRABAJOS = 50_000

#: Cuántas veces se mide cada consulta. La mediana de esto es el número que se informa.
REPETICIONES = 7

CONSULTA_ENTREGAS_ANTES = """
select id, attempt, created_at, event_type, status_code
from webhook_deliveries
where endpoint_id = :endpoint and organization_id = :organizacion
order by created_at desc, attempt desc
limit 20 offset 0
"""

CONSULTA_ENTREGAS_DESPUES = """
select id, attempt, created_at, event_type, status_code
from webhook_deliveries
where endpoint_id = :endpoint and organization_id = :organizacion
order by created_at desc, attempt desc, id desc
limit 20 offset 0
"""

CONSULTA_TRABAJOS_ANTES = """
select id, created_at, status, kind
from agent_jobs
where organization_id = :organizacion
order by created_at desc
limit 20 offset 0
"""

CONSULTA_TRABAJOS_DESPUES = """
select id, created_at, status, kind
from agent_jobs
where organization_id = :organizacion
order by created_at desc, id desc
limit 20 offset 0
"""

#: `payload` es `JSONB`: se pasa como parámetro, no se escribe en el SQL.
INSERTAR_ENTREGAS = """
insert into webhook_deliveries (
    id, endpoint_id, organization_id, event_type, payload, status_code, attempt,
    created_at
)
select gen_random_uuid(), :endpoint, :org, 'pentest.completed', :payload, 200, 1,
       now() - (serie || ' seconds')::interval
from generate_series(1, :filas) as serie
"""

INSERTAR_TRABAJOS = """
insert into agent_jobs (
    id, organization_id, kind, target, status, created_at, updated_at
)
select gen_random_uuid(), :org, 'CONTAINER_SCAN', 'medicion/' || serie, 'QUEUED',
       now() - (serie || ' seconds')::interval, now()
from generate_series(1, :filas) as serie
"""


def _titulo(texto: str) -> None:
    print(f"\n{'=' * 96}\n{texto}\n{'=' * 96}")


async def _plan(
    conexion: object, consulta: str, params: dict[str, object]
) -> tuple[str, list[float], int, float]:
    """Ejecuta la consulta `REPETICIONES` veces y devuelve `(cadena del plan, tiempos, buffers, coste)`."""

    # La primera se descarta: paga el calor de caché y el primer contacto con cada página.
    await conexion.execute(text(f"explain (analyze, buffers, format json) {consulta}"), params)  # type: ignore[attr-defined]
    tiempos: list[float] = []
    buffers = 0
    coste = 0.0
    cadena = ""
    for _ in range(REPETICIONES):
        # `EXPLAIN` con `format json` devuelve una lista de planes, uno por sentencia
        # enviada. Aquí solo se envía una, así que el interesting es el primero.
        raiz = (
            (
                await conexion.execute(  # type: ignore[attr-defined]
                    text(f"explain (analyze, buffers, format json) {consulta}"), params
                )
            )
            .scalars()
            .first()[0]
        )
        nodo = raiz["Plan"]
        if not cadena:
            partes: list[str] = []
            while nodo is not None:
                trozo = nodo["Node Type"]
                if nodo.get("Index Name"):
                    trozo += f" [{nodo['Index Name']}]"
                partes.append(trozo)
                hijos = nodo.get("Plans") or []
                nodo = hijos[0] if hijos else None
            cadena = " <- ".join(partes)
            buffers = raiz["Plan"].get("Shared Hit Blocks", 0) + raiz["Plan"].get(
                "Shared Read Blocks", 0
            )
            coste = raiz["Plan"]["Total Cost"]
        tiempos.append(raiz["Execution Time"])
    return cadena, tiempos, buffers, coste


async def _medir_y_pintar(
    conexion: object, etiqueta: str, consulta: str, params: dict[str, object]
) -> None:
    """Mide e imprime en una línea. Existe para que `main` no desmonte la tupla."""

    cadena, tiempos, buffers, coste = await _plan(conexion, consulta, params)
    print(_resumir(etiqueta, cadena, tiempos, buffers, coste))


def _resumir(etiqueta: str, cadena: str, tiempos: list[float], buffers: int, coste: float) -> str:
    mediana = statistics.median(tiempos)
    return (
        f"  {etiqueta:<12} {cadena:<66} mediana {mediana:>7.3f} ms  "
        f"[{min(tiempos):.3f}–{max(tiempos):.3f}]  {buffers:>4} buf  (coste {coste:.1f})"
    )


async def main() -> int:
    async with engine.connect() as conexion:
        transaccion = await conexion.begin()
        try:
            org = uuid.uuid4()
            endpoint = uuid.uuid4()
            await conexion.execute(
                text(
                    "insert into organizations "
                    "(id, name, slug, plan_tier, credit_balance, created_at, updated_at) "
                    "values (:id, 'medicion', :slug, 'PRO', 0, now(), now())"
                ),
                {"id": org, "slug": f"medicion-{org.hex[:12]}"},
            )
            await conexion.execute(
                text(
                    "insert into webhook_endpoints "
                    "(id, organization_id, url, encrypted_secret, event_types, is_active, "
                    "consecutive_failures, created_at, updated_at) "
                    "values (:id, :org, 'https://medicion.invalid/hook', 'whsec_medicion', "
                    "'[\"pentest.completed\"]'::jsonb, true, 0, now(), now())"
                ),
                {"id": endpoint, "org": org},
            )
            await conexion.execute(
                text(INSERTAR_ENTREGAS),
                {
                    "endpoint": endpoint,
                    "org": org,
                    "filas": ENTREGAS,
                    "payload": '{"evento": "medicion"}',
                },
            )
            await conexion.execute(text(INSERTAR_TRABAJOS), {"org": org, "filas": TRABAJOS})
            await conexion.execute(text("analyze webhook_deliveries"))
            await conexion.execute(text("analyze agent_jobs"))

            tamano = (
                await conexion.execute(
                    text(
                        "select pg_size_pretty(pg_total_relation_size('webhook_deliveries')) "
                        "|| ' / ' "
                        "|| pg_size_pretty(pg_total_relation_size('agent_jobs'))"
                    )
                )
            ).scalar_one()
            print(
                f"webhook_deliveries con {ENTREGAS} entregas, agent_jobs con {TRABAJOS}: {tamano}\n"
                f"mediana de {REPETICIONES} ejecuciones, la primera descartada"
            )

            params = {"endpoint": endpoint, "organizacion": org}
            _titulo("Historial de entregas: created_at DESC, attempt DESC  [+ id]")
            await _medir_y_pintar(conexion, "sin desemp.", CONSULTA_ENTREGAS_ANTES, params)
            await _medir_y_pintar(conexion, "con desemp.", CONSULTA_ENTREGAS_DESPUES, params)

            params_trabajos = {"organizacion": org}
            _titulo("Cola de trabajos del tenant: created_at DESC  [+ id]")
            await _medir_y_pintar(conexion, "sin desemp.", CONSULTA_TRABAJOS_ANTES, params_trabajos)
            await _medir_y_pintar(conexion, "con desemp.", CONSULTA_TRABAJOS_DESPUES, params_trabajos)

            _titulo(
                "¿Merece la pena un índice compuesto para el desempate?\n"
                "Se crea a propósito dentro de la transacción y se deshace con el ROLLBACK."
            )
            await conexion.execute(
                text(
                    "create index ix_webhook_deliveries_endpoint_created_attempt_id "
                    "on webhook_deliveries (endpoint_id, created_at, attempt, id)"
                )
            )
            await conexion.execute(
                text(
                    "create index ix_agent_jobs_org_created_id "
                    "on agent_jobs (organization_id, created_at, id)"
                )
            )
            await conexion.execute(text("analyze webhook_deliveries"))
            await conexion.execute(text("analyze agent_jobs"))
            await _medir_y_pintar(conexion, "entregas", CONSULTA_ENTREGAS_DESPUES, params)
            await _medir_y_pintar(conexion, "trabajos", CONSULTA_TRABAJOS_DESPUES, params_trabajos)
        finally:
            await transaccion.rollback()
            print("\nTransacción revertida: la base queda como estaba.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
