#!/usr/bin/env python
"""Vectoriza los documentos de `workspace_knowledge_documents` que aún no tienen embedding.

## Por qué este script existe y no una migración

Porque la extensión `vector` no está disponible en el servidor, y esa es una decisión de
infraestructura —`apt install postgresql-16-pgvector` y un superusuario— que no se resuelve
desde aquí. `enable_vector_search.py` crea la columna y el índice cuando se puede; este script
rellena los datos cuando ya se pudo.

Y aunque la extensión estuviera, un `INSERT` que llama a la API de embeddings **no** puede ir en
una migración. Una migración se ejecuta dentro de una transacción, contra un bloqueo de esquema,
y tarda lo que tarden las peticiones de red. Con 188 documentos y un proveedor que puede tardar un
segundo por documento, eso son minutos de bloqueo, y una migración que tarda minutos en un
despliegue es un despliegue que alguien reinicia a mitad y deja la base en un estado raro.

## Por qué lotes de 20

Porque cada lote es una transacción. Ciento ochenta y ocho transacciones contra la tabla de
documentos de un cliente no son un problema, pero ciento ochenta y ocho transacciones con una
llamada de red dentro **sí** lo son: cada `COMMIT` mantiene el WAL de esa transacción abierto
durante la llamada, y el WAL retenido impide que el `VACUUM` recupere espacio y retrasa el
autovacuum de las tablas que se modifican alrededor.

El tamaño del lote está declarado abajo y no se lee del entorno, a diferencia de casi todo lo
demás en este repositorio. La razón es que es un valor que depende de una propiedad del
proveedor —cuánto aguanta su límite de peticiones por minuto y qué tasa de error tolera— y de
nada configurable. El día que uno de los dos cambie, se cambia aquí y se lee por qué.

## Qué hace cuando el proveedor falla

Se detiene, y dice por qué. No se lo salta. Un documento sin embedding no rompe la búsqueda
léxica, así que un fallo del proveedor se podría ignorar y el script "terminaría". Un script
que termina con la mitad de los documentos vectorizados y dice que ha ido bien es peor que uno
que se para en el primer error: el primero deja un estado que nadie va a notar hasta que una
búsqueda por similitud devuelva resultados pobres y nadie sepa por qué.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING


RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import text  # noqa: E402

from backend.apps.knowledge.embeddings import (  # noqa: E402
    EmbeddingsNotConfiguredError,
    EmbeddingsUpstreamError,
    embed_textos,
)
from backend.core.config import settings  # noqa: E402
from backend.core.database import AsyncSessionLocal  # noqa: E402


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("reindex_knowledge_embeddings")

#: Documentos por transaccion.
#:
#: Veinte es un compromiso entre dos costes opuestos. Ciento ochenta y ocho transacciones
#: contra la tabla de documentos de un cliente no son un problema; ciento ochenta y ocho con
#: una llamada de red dentro **si** lo son: cada `COMMIT` mantiene el WAL de esa transaccion
#: abierto durante la llamada, y el WAL retenido impide que el `VACUUM` recupere espacio y
#: retrasa el autovacuum de las tablas que se modifican alrededor.
#:
#: Y en la otra direccion, lotes de uno serian ciento ochenta transacciones y ciento ochenta
#: oportunidades de que una falle a mitad, cada una dejando trabajo a medias que hay que
#: reconciliar despues.
LOTE = 20

#: Pausa entre lotes, en segundos.
#:
#: Existe para no comerse el límite de peticiones por minuto del proveedor en una sola ráfaga. Sin
#: él, veinte peticiones seguidas desde la misma IP son veinte peticiones seguidas desde la misma
#: IP, y los proveedores aplican el límite por IP, no por cliente.
PAUSA_ENTRE_LOTES = 1.0

#: Repeticiones por documento antes de rendirse.
REINTENTOS = 3

#: Espera entre repeticiones. Crece de forma exponencial porque un proveedor saturado necesita
#: tiempo, y reintentar con el mismo intervalo solo garantiza saturarlo más.
ESPERA_INICIAL = 2.0


async def _documentos_pendientes(
    session: AsyncSession, limite: int, desplazamiento: int = 0
) -> list[tuple[object, str]]:
    """Los documentos sin embedding, en orden estable.

    El orden es por `created_at` y no por `id`. Con `id` —un UUIDv4— el orden de la consulta no
    es determinista, y un script que procesa los mismos documentos en un orden distinto en cada
    ejecución hace que dos ejecuciones parciales dejen el conjunto de documentos vectorizados en
    estados distintos. El desplazamiento lo usa el llamador para no volver a leer lo ya hecho.
    """

    resultado = await session.execute(
        text(
            "SELECT id, content FROM workspace_knowledge_documents "
            "WHERE embedding IS NULL "
            "ORDER BY created_at, id "
            "LIMIT :limite OFFSET :desplazamiento"
        ),
        {"limite": limite, "desplazamiento": desplazamiento},
    )
    return [(identificador, contenido) for identificador, contenido in resultado.all()]


async def _vectorizar_lote(session: AsyncSession, lote: list[tuple[object, str]]) -> int:
    """Vectoriza un lote y lo persiste. Devuelve cuántos se escribieron."""

    textos = [contenido for _identificador, contenido in lote]
    vector = await _vector_con_reintentos(textos)
    await session.execute(
        text(
            "UPDATE workspace_knowledge_documents AS d "
            "SET embedding = CAST(:vector AS vector) "
            "WHERE d.id = ANY(:ids)"
        ),
        {
            "vector": vector,
            # `uuid` como parametro de array: el driver lo envia como `uuid[]` y Postgres lo
            # compara con la columna sin problema.
            "ids": [identificador for identificador, _contenido in lote],
        },
    )
    return len(lote)


async def _vector_con_reintentos(textos: list[str]) -> str:
    """El vector en formato de texto, con reintentos.

    ## Por qué reintenta y por qué no reintenta todo

    Reintenta los tres fallos que son transitorios —red caída, `429`, `5xx`— porque se resuelven
solos con el tiempo, y no reintenta los dos que no: una clave sin configurar y una respuesta que
no se puede parsear. Reintentar un `NotConfigured` tres veces con dos segundos de espera es tres
veces inútil, y esconde el motivo real detrás de medio minuto de espera.
    """

    espera = ESPERA_INICIAL
    ultimo_error: Exception | None = None

    for intento in range(1, REINTENTOS + 1):
        try:
            vectores = await embed_textos(
                textos,
                api_base=settings.llm_api_base,
                api_key=settings.llm_api_key,
            )
        except EmbeddingsNotConfiguredError:
            # No es transitorio: reintentar no lo arregla y el mensaje es el que hay que dar.
            raise
        except EmbeddingsUpstreamError as error:
            ultimo_error = error
            if intento == REINTENTOS:
                break
            logger.warning(
                "Intento %d/%d fallido (%s). Reintento en %.1fs.",
                intento,
                REINTENTOS,
                error,
                espera,
            )
            await asyncio.sleep(espera)
            espera *= 2
            continue

        # El vector se pasa como texto porque un parametro de la columna `vector` no acepta un
        # `json`. El formato es el que espera la funcion `to_json` de PostgreSQL, y es el mismo
        # que devuelve la API en la respuesta.
        return json.dumps(vectores[0])

    raise SystemExit(
        f"No se pudo vectorizar el lote tras {REINTENTOS} intentos: {ultimo_error}"
    )


async def reindexar(limite_total: int | None, simular: bool) -> int:
    """Vectoriza los documentos pendientes. Devuelve cuántos escribió."""

    if not await hay_soporte_vectorial_solo_columna():
        logger.error(
            "La columna `embedding` no existe. Ejecuta primero:\n"
            "    uv run --project backend python scripts/enable_vector_search.py --aplicar"
        )
        return 2

    if not await _hay_extension():
        logger.error(
            "La extensión `vector` no está habilitada en la base. R5 y el motor siguen "
            "funcionando: la búsqueda por vectores es una mejora, no un requisito. El RAG "
            "sigue usando la recuperación ponderada de `retrieval.py`."
        )
        return 2

    if settings.llm_api_key.get_secret_value() == "":
        logger.error("LLM_API_KEY no está configurada: no hay con qué generar embeddings.")
        return 2

    if simular:
        pendientes = await _cuenta_pendientes()
        logger.info(
            "Simulacion: %d documento(s) pendientes de vectorizar. No se ha escrito nada.",
            pendientes,
        )
        return 0

    escritos = 0
    leidos = 0
    while limite_total is None or leidos < limite_total:
        tamano = LOTE if limite_total is None else min(LOTE, limite_total - leidos)
        async with AsyncSessionLocal() as session:
            lote = await _documentos_pendientes(session, tamano)
            if not lote:
                break
            await _vectorizar_lote(session, lote)
            await session.commit()

        leidos += len(lote)
        escritos += len(lote)
        logger.info("Vectorizados %d documento(s) de este lote.", len(lote))

        if limite_total is None or leidos < limite_total:
            await asyncio.sleep(PAUSA_ENTRE_LOTES)

    logger.info("Fin. %d documento(s) vectorizado(s).", escritos)
    return 0


async def _hay_extension() -> bool:
    async with AsyncSessionLocal() as session:
        resultado = await session.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        )
        return bool(resultado.scalar_one())


async def hay_soporte_vectorial_solo_columna() -> bool:
    """Si la columna existe, sin mirar la extensión.

    Es la mitad de la comprobación de `hay_soporte_vectorial`, y está aquí separada porque este
    script necesita **los dos** mensajes: "falta la extensión" y "falta la columna" son
    acciones distintas —una es un `apt install` en el servidor y la otra es ejecutar otro script—,
    y un mensaje único obligaría a quien lo lee a adivinar cuál de las dos le toca.
    """

    async with AsyncSessionLocal() as session:
        resultado = await session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'workspace_knowledge_documents' "
                "AND column_name = 'embedding')"
            )
        )
        return bool(resultado.scalar_one())


async def _cuenta_pendientes() -> int:
    async with AsyncSessionLocal() as session:
        resultado = await session.execute(
            text(
                "SELECT count(*) FROM workspace_knowledge_documents WHERE embedding IS NULL"
            )
        )
        return int(resultado.scalar_one())


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument(
        "--simular",
        action="store_true",
        help="dice cuántos documentos faltan y no escribe nada",
    )
    analizador.add_argument(
        "--limite",
        type=int,
        default=None,
        help="máximo de documentos a vectorizar en esta ejecución",
    )
    argumentos = analizador.parse_args()

    inicio = time.monotonic()
    codigo = asyncio.run(reindexar(argumentos.limite, argumentos.simular))
    logger.info("Transcurrido: %.1fs.", time.monotonic() - inicio)
    return codigo


if __name__ == "__main__":
    sys.exit(main())
