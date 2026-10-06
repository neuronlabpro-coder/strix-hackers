"""¿Merece `webhook_endpoints` un índice `(organization_id, created_at, id)`?

## Por qué esta pregunta se ha vuelto a medir

Porque la primera medición dio un resultado que no se sostiene. Con una distribución en la que
un tenant tenía 100.000 endpoints y el resto 400.000, el planificador elegía `Index Scan` con el
índice de tres columnas y `Sort <- Bitmap Scan` sin él. Con otra distribución —el tenant grande
con 20 y el resto 400.000— elige `Sort <- Bitmap Heap Scan` en **los dos** casos, con los mismos
buffers, y además con un **coste estimado mayor** usando el índice de tres columnas, que es más
ancho.

Eso no es una contradicción: es la misma respuesta a dos preguntas distintas, y la lección es que
**una medición de plan sin la distribución de filas no mide nada**. El plan depende de cuántas
filas toca leer, así que el caso que hay que medir es el de una plataforma con muchos tenants
repartidos, que es el real.

## La pregunta de fondo

`list_webhooks` **no pagina**: devuelve las filas enteras del tenant, sin `LIMIT`. Sin tope, el
planificador tiene que leer todas las filas del tenant y ordenarlas, y ordenar unas cientos de
filas en memoria sale más barato que leerlas en orden de índice. Un índice de orden solo
compensaría si el `LIMIT` permitiera dejar de leer.

Eso es lo que hay que comprobar, no un `EXPLAIN` suelto: si con el índice el plan es el mismo
`Sort`, el índice no aporta y es escritura de más en cada alta de endpoint.

## Sin ensuciar la base

Todo dentro de una transacción que se revierte; `CREATE INDEX` y `DROP INDEX` también se
deshacen con `ROLLBACK`.

Uso:

    $env:DB_CONNECT_TIMEOUT_SECONDS = "60"
    $env:PYTHONPATH = "."
    uv run --project backend python scripts/_medir_indice_endpoints.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import uuid

from sqlalchemy import text

from backend.core.database import engine

#: Tenants sembrados. Muchos y ninguno enorme: es una plataforma con clientes, no un caso de
#: laboratorio. Un solo tenant gigante haría que la respuesta fuera «ordena», que es la
#: respuesta trivial y no la que interesa.
TENANTS = 3_000

#: Endpoints por tenant. La mayoría tiene uno o dos, que es lo normal; unos pocos tienen cientos.
ENDPOINTS_POR_TENANT = 3

#: El tenant que se mide en cada tamaño: se le siembran `TAMANOS` endpoints encima de lo que ya
#: tenga, para poder ver las tres escalas sobre la misma tabla.
TAMANOS = (5, 500, 20_000)

#: Cuántas veces se mide cada consulta. La mediana es el número que se informa.
REPETICIONES = 9

CONSULTA = """
select id, url, created_at, is_active, consecutive_failures
from webhook_endpoints
where organization_id = :organizacion
order by created_at desc, id desc
"""

CONSULTA_SIN_DESEMPATE = """
select id, url, created_at, is_active, consecutive_failures
from webhook_endpoints
where organization_id = :organizacion
order by created_at desc
"""

INSERAR = """
insert into webhook_endpoints (
    id, organization_id, url, encrypted_secret, event_types, is_active,
    consecutive_failures, created_at, updated_at
)
select gen_random_uuid(), :org, 'https://medicion.invalid/' || serie,
       'whsec_' || lpad((:prefijo || serie)::text, 40, '0'), :eventos, true, 0,
       now() - (serie || ' seconds')::interval, now()
from generate_series(1, :filas) as serie
"""


def _titulo(texto: str) -> None:
    print(f"\n{'=' * 100}\n{texto}\n{'=' * 100}")


async def _plan(conexion: object, consulta: str, org: uuid.UUID) -> tuple[str, float, int, float]:
    await conexion.execute(text(f"explain (analyze, buffers, format json) {consulta}"), {"organizacion": org})  # type: ignore[attr-defined]
    tiempos: list[float] = []
    cadena = ""
    buffers = 0
    coste = 0.0
    for _ in range(REPETICIONES):
        raiz = (
            (
                await conexion.execute(  # type: ignore[attr-defined]
                    text(f"explain (analyze, buffers, format json) {consulta}"),
                    {"organizacion": org},
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


async def _medir(conexion: object, etiqueta: str, consulta: str, org: uuid.UUID) -> None:
    cadena, mediana, buffers, coste = await _plan(conexion, consulta, org)
    print(
        f"  {etiqueta:<16} {cadena:<58} mediana {mediana:>7.3f} ms  {buffers:>4} buf  (coste {coste:.1f})"
    )


async def main() -> int:
    async with engine.connect() as conexion:
        transaccion = await conexion.begin()
        try:
            organizaciones: list[uuid.UUID] = [uuid.uuid4() for _ in range(TENANTS)]
            for org in organizaciones:
                await conexion.execute(
                    text(
                        "insert into organizations "
                        "(id, name, slug, plan_tier, credit_balance, created_at, updated_at) "
                        "values (:id, 'medicion', :slug, 'PRO', 0, now(), now())"
                    ),
                    {"id": org, "slug": f"medicion-{org.hex[:12]}"},
                )
            for indice, org in enumerate(organizaciones):
                await conexion.execute(
                    text(INSERAR),
                    {
                        "org": org,
                        "filas": ENDPOINTS_POR_TENANT,
                        "prefijo": str(indice + 1),
                        "eventos": '["pentest.completed"]',
                    },
                )
            # Los tres tenants que se miden, cada uno con su tamaño.
            medidos: list[tuple[int, uuid.UUID]] = []
            for indice, filas in enumerate(TAMANOS):
                org = organizaciones[indice]
                medidos.append((filas, org))
                await conexion.execute(
                    text(INSERAR),
                    {
                        "org": org,
                        "filas": filas,
                        # El prefijo va en el `encrypted_secret`, que es único en toda la base.
                        # Los tenants de ruido usan `1..TENANTS` y este rango empieza en un
                        # orden de magnitud aparte, así que no se solapa con ninguno de ellos.
                        "prefijo": str(10_000_000_000 * (indice + 1)),
                        "eventos": '["pentest.completed"]',
                    },
                )
            await conexion.execute(text("analyze webhook_endpoints"))

            total = TENANTS * ENDPOINTS_POR_TENANT + sum(TAMANOS)
            tamano = (
                await conexion.execute(
                    text("select pg_size_pretty(pg_total_relation_size('webhook_endpoints'))")
                )
            ).scalar_one()
            print(
                f"webhook_endpoints: {total} endpoints en {TENANTS} tenants, {tamano}\n"
                f"mediana de {REPETICIONES} ejecuciones, la primera descartada"
            )

            for con_indice in (False, True):
                await conexion.execute(
                    text("drop index if exists ix_webhook_endpoints_org_created_id")
                )
                if con_indice:
                    await conexion.execute(
                        text(
                            "create index ix_webhook_endpoints_org_created_id "
                            "on webhook_endpoints (organization_id, created_at, id)"
                        )
                    )
                await conexion.execute(text("analyze webhook_endpoints"))
                _titulo(
                    "CON el indice compuesto (organization_id, created_at, id)"
                    if con_indice
                    else "SIN el indice compuesto: solo (organization_id, created_at)"
                )
                for filas, org in medidos:
                    print(f"\ntenant con {filas} endpoints:")
                    await _medir(conexion, "con desempate", CONSULTA, org)
                    await _medir(conexion, "sin desempate", CONSULTA_SIN_DESEMPATE, org)
        finally:
            await transaccion.rollback()
            print("\nTransacción revertida: la base queda como estaba.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))