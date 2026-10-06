"""¿Merece `agent_jobs` un índice `(organization_id, created_at, id)`?

## Qué pregunta

`listar_trabajos`, en `agents/service.py`, filtra por `organization_id` y ordena por
`created_at DESC`. Los índices de `agent_jobs` que hay hoy son `(organization_id, status)`,
`(status, priority_order, created_at)`, `(status, lease_expires_at)` y `(agent_id, status)`.
**Ninguno** cubre «los trabajos de este tenant, del más nuevo al más viejo»: el que empieza por
`organization_id` termina en `status`, así que no puede dar el orden.

El mismo hueco lo tiene `resumen_agente`, que hace `ORDER BY created_at DESC LIMIT 200` sobre
el mismo filtro.

## Por qué se mide con varios tenants y no con uno

Porque sembrar 50.000 jobs de **un** tenant y pedir los de ese tenant es el caso que más favorece
al secuencial: el filtro no descarta nada, así que la tabla entera es la respuesta y ordenar es
barato. El caso real es el contrario —muchos tenants, y el que pregunta tiene una fracción—, y
ahí el secuencial lee filas que va a tirar. Por eso se siembran los jobs repartidos entre
`TENANTS_DE_RUIDO` organizaciones: es lo que obliga al planificador a preferir un índice.

## Sin ensuciar la base

Todo dentro de una transacción que se revierte; `CREATE INDEX` también se deshace con `ROLLBACK`.

Uso:

    $env:DB_CONNECT_TIMEOUT_SECONDS = "60"
    $env:PYTHONPATH = "."
    uv run --project backend python scripts/_medir_indice_agent_jobs.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import uuid

from sqlalchemy import text

from backend.core.database import engine

#: Jobs del tenant que se pregunta. La cola de escaneos de un cliente grande.
JOBS_DEL_TENANT = 2_000

#: Jobs repartidos entre el resto de tenants. Es lo que hace que el filtro del tenant
#: discrimine y lo que obliga a decidir entre un índice y un recorrido.
TENANTS_DE_RUIDO = 2_000
JOBS_POR_TENANT_DE_RUIDO = 25

#: Cuántas veces se mide cada consulta. La mediana es el número que se informa; la primera
#: ejecución se descarta porque paga el calor de caché.
REPETICIONES = 7

#: `resumen_agente` no pagina: hace `limit(200)`, que es un tope, no un desplazamiento.
CONSULTA_LISTADO = """
select id, created_at, status, kind
from agent_jobs
where organization_id = :organizacion
order by created_at desc, id desc
limit 20 offset 0
"""

CONSULTA_RESUMEN = """
select id, created_at, status, kind, result
from agent_jobs
where organization_id = :organizacion
order by created_at desc, id desc
limit 200
"""

#: La variante con filtro de estado, que es lo que hace la pantalla cuando se filtra. El
#: índice `(organization_id, status)` sí la puede servir, así que también se mide: un índice
#: nuevo que no ayude a esta consulta es dinero tirado.
CONSULTA_CON_ESTADO = """
select id, created_at, status, kind
from agent_jobs
where organization_id = :organizacion and status = 'QUEUED'
order by created_at desc, id desc
limit 20 offset 0
"""

INSERTAR_JOBS = """
insert into agent_jobs (id, organization_id, kind, target, status, created_at, updated_at)
select gen_random_uuid(), :org, 'CONTAINER_SCAN', 'medicion/' || serie, :estado,
       now() - (serie || ' seconds')::interval, now()
from generate_series(1, :filas) as serie
"""


def _titulo(texto: str) -> None:
    print(f"\n{'=' * 96}\n{texto}\n{'=' * 96}")


async def _plan(
    conexion: object, consulta: str, params: dict[str, object]
) -> tuple[str, float, int, float]:
    await conexion.execute(text(f"explain (analyze, buffers, format json) {consulta}"), params)  # type: ignore[attr-defined]
    tiempos: list[float] = []
    cadena = ""
    buffers = 0
    coste = 0.0
    for numero in range(REPETICIONES):
        raiz = (
            (
                await conexion.execute(  # type: ignore[attr-defined]
                    text(f"explain (analyze, buffers, format json) {consulta}"), params
                )
            )
            .scalars()
            .first()[0]
        )
        if not cadena:
            nodo = raiz["Plan"]
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
    return cadena, statistics.median(tiempos), buffers, coste


async def _medir(
    conexion: object, etiqueta: str, consulta: str, params: dict[str, object]
) -> None:
    cadena, mediana, buffers, coste = await _plan(conexion, consulta, params)
    print(f"  {etiqueta:<26} {cadena:<62} mediana {mediana:>8.3f} ms  {buffers:>5} buf  (coste {coste:.1f})")


async def main() -> int:
    async with engine.connect() as conexion:
        transaccion = await conexion.begin()
        try:
            tenant = uuid.uuid4()
            organizaciones = [tenant] + [uuid.uuid4() for _ in range(TENANTS_DE_RUIDO)]
            for org in organizaciones:
                await conexion.execute(
                    text(
                        "insert into organizations "
                        "(id, name, slug, plan_tier, credit_balance, created_at, updated_at) "
                        "values (:id, 'medicion', :slug, 'PRO', 0, now(), now())"
                    ),
                    {"id": org, "slug": f"medicion-{org.hex[:12]}"},
                )
            await conexion.execute(
                text(INSERTAR_JOBS),
                {"org": tenant, "filas": JOBS_DEL_TENANT, "estado": "QUEUED"},
            )
            await conexion.execute(
                text(INSERTAR_JOBS),
                {
                    "org": organizaciones[1],
                    "filas": JOBS_POR_TENANT_DE_RUIDO,
                    "estado": "QUEUED",
                },
            )
            # El resto de tenants, cada uno con su propia organizations fila.
            for org in organizaciones[2:]:
                await conexion.execute(
                    text(INSERTAR_JOBS),
                    {"org": org, "filas": JOBS_POR_TENANT_DE_RUIDO, "estado": "RUNNING"},
                )
            await conexion.execute(text("analyze agent_jobs"))

            total = JOBS_DEL_TENANT + TENANTS_DE_RUIDO * JOBS_POR_TENANT_DE_RUIDO
            tamano = (
                await conexion.execute(
                    text("select pg_size_pretty(pg_total_relation_size('agent_jobs'))")
                )
            ).scalar_one()
            print(
                f"agent_jobs: {total} jobs en {TENANTS_DE_RUIDO + 1} tenants, {tamano}\n"
                f"el tenant que pregunta tiene {JOBS_DEL_TENANT} de esos {total} jobs\n"
                f"mediana de {REPETICIONES} ejecuciones, la primera descartada"
            )
            params = {"organizacion": tenant}

            _titulo("SIN índice nuevo — el estado de hoy")
            await _medir(conexion, "listado (limit 20)", CONSULTA_LISTADO, params)
            await _medir(conexion, "resumen (limit 200)", CONSULTA_RESUMEN, params)
            await _medir(conexion, "listado con estado", CONSULTA_CON_ESTADO, params)

            await conexion.execute(
                text(
                    "create index ix_agent_jobs_org_created_id "
                    "on agent_jobs (organization_id, created_at, id)"
                )
            )
            await conexion.execute(text("analyze agent_jobs"))

            _titulo("CON (organization_id, created_at, id)")
            await _medir(conexion, "listado (limit 20)", CONSULTA_LISTADO, params)
            await _medir(conexion, "resumen (limit 200)", CONSULTA_RESUMEN, params)
            await _medir(conexion, "listado con estado", CONSULTA_CON_ESTADO, params)
        finally:
            await transaccion.rollback()
            print("\nTransacción revertida: la base queda como estaba.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
