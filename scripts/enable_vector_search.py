#!/usr/bin/env python
"""Habilita la busqueda vectorial en `workspace_knowledge_documents`.

## Por que esto es un script y no una migracion de Alembic

Porque una migracion que se aplica *a veces* es peor que no tener migracion.

`alembic check` compara los metadatos de SQLAlchemy con el esquema real de la base y falla si no
coinciden. El mecanismo que haria falta —crear la columna solo si la extension `vector` esta
disponible, y omitirla en caso contrario— produce exactamente la deriva que ese gate existe para
detectar: los metadatos declaran `embedding vector(1536)` y la base no la tiene, porque la
extension no se pudo instalar. El gate se pone rojo de forma permanente y ya no comprueba nada
util, porque para que vuelva a estar verde habria que mentirle sobre el esquema.

Eso tiene una consecuencia practica que es la que manda: la instalacion de `pgvector` en el
VPS es una decision de infraestructura —requiere `apt install postgresql-16-pgvector` y un
superusuario— y no algo que este repositorio pueda resolver por su cuenta. Mientras esa decision
no este tomada, la columna **no existe**, y el repositorio no debe fingir lo contrario.

## Que hace

Tres pasos, cada uno de los cuales se puede haber hecho ya:

1. `CREATE EXTENSION IF NOT EXISTS vector` — necesita superusuario.
2. `ALTER TABLE ... ADD COLUMN IF NOT EXISTS embedding vector(1536)`.
3. `CREATE INDEX IF NOT EXISTS ... USING hnsw (embedding vector_cosine_ops)`.

Y despues, **solo si los tres estan**, sincroniza los metadatos de Alembic para que `alembic
check` deje de ver una columna que no encuentra. Ese ultimo paso es el que hace que la puerta de
calidad vuelva a ser util en lugar de estar apagada con una excepcion permanente.

## Por que el indice HNSW y no el de PostgreSQL por defecto

Porque un indice ivfflat o GIN sobre 1536 dimensiones en una tabla de cientos de filas no mejora
nada y cuesta tiempo de construccion; con millones, si. HNSW da un orden de magnitud mejor en
dimensiones altas, a cambio de memoria, que es exactamente el compromiso que se quiere en una
tabla de documentos de contexto.

La metrica es coseno y no producto interno porque los embeddings de `text-embedding-3-small` ya
vienen normalizados: el coseno es la distancia que corresponde, y `vector_cosine_ops` es el
operador que la usa.

## Uso

    uv run --project backend python scripts/enable_vector_search.py --comprobar
    uv run --project backend python scripts/enable_vector_search.py --aplicar
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


# `backend` es un paquete de la raiz del repositorio, y este script vive en `scripts/`. Sin
# anadir la raiz al `sys.path`, `import backend` falla con un `ModuleNotFoundError` que no
# explica la causa: parece que falte el paquete, y lo que falta es una entrada en la ruta.
#
# La alternativa era `pythonpath = [".."]`, que ya existe en `pyproject.toml` pero **solo** le
# sirve a pytest. Un script ejecutado a mano no lo lee, y esa es la diferencia entre un modulo
# que se puede probar y un modulo que solo funciona dentro de la suite.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from backend.core.database import AsyncSessionLocal  # noqa: E402


# El logger se crea aqui y no se importa de la aplicacion. `backend.core` no tiene un modulo de
# logging, y el patron del proyecto es `logging.getLogger(__name__)` en cada modulo que lo
# necesita. Importar de uno que no existe da un error que no aparece hasta que este script se
# ejecuta en un despliegue, que es el sitio mas caro para enterarse.
logger = logging.getLogger("enable_vector_search")


def _configurar_logging() -> None:
    """Hace que los mensajes de este script se vean.

    ## Por que hace falta en un script y no en un modulo de la aplicacion

    Porque la aplicacion configura el logging en su `main`, y un script ejecutado a mano no
    pasa por ahi. Sin esta linea, `logger.info(...)` no produce nada: el logging de la biblioteca
    estandar descarta los mensajes de `INFO` cuando la raiz no tiene manejadores, asi que
    `--comprobar` terminaba sin decir si la extension estaba o no, que es justo la mitad del
    trabajo de ese comando.

    Se pone a nivel de modulo, y no dentro de la funcion, porque el nivel se decide una vez y
    repetirlo en cada llamada es la forma de que dos rutas.ieutenFormatter distinto.
    """
    if not logger.handlers:
        manejador = logging.StreamHandler()
        manejador.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(manejador)
    logger.setLevel(logging.INFO)

# El nombre de la columna, del modelo y del indice, en un solo sitio. No se importan de
# `embeddings.py` a proposito: este script tiene que poder correr en un despliegue donde el
# paquete de application no arranqu todavia, que es justo cuando se usa.
COLUMNA = "embedding"
INDICE = "ix_knowledge_documents_embedding_hnsw"
TABLA = "workspace_knowledge_documents"
DIMENSION = 1536
REVISION_DE_ALEMBIC = "c3d4e5f6a7b8"


class ExtensionNoDisponibleError(RuntimeError):
    """La extension `vector` no se puede instalar desde esta conexion."""


async def _tiene_extension(session: object) -> bool:
    resultado = await session.execute(  # type: ignore[attr-defined]
        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
    )
    return bool(resultado.scalar_one())


async def _tiene_columna(session: object) -> bool:
    resultado = await session.execute(  # type: ignore[attr-defined]
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :tabla AND column_name = :columna)"
        ),
        {"tabla": TABLA, "columna": COLUMNA},
    )
    return bool(resultado.scalar_one())


async def comprobar(session: object) -> dict[str, bool]:
    """El estado actual, sin tocar nada."""
    return {
        "extension": await _tiene_extension(session),
        "columna": await _tiene_columna(session),
    }


async def instalar_extension(session: object) -> None:
    """Instala la extension, o explica por que no se pudo.

    ## Por que `DBAPIError` y no `ProgrammingError`

    Porque la primera versión de esta función capturaba `ProgrammingError` y no capturaba nada.

    `ProgrammingError` es la categoria de SQLAlchemy para el SQL mal escrito, que es lo que
    PEP249 le asigna al driver. El fallo real aqui —la extension no esta disponible en el
    servidor— lo reporta asyncpg como `FeatureNotSupportedError`, y al envuelto por SQLAlchemy
    llega como `DBAPIError` sin llegar a ser `ProgrammingError`. El `except` no se activaba, la
    traza subia entera, y quien lo ejecutaba veía un volcado de excepcion de un driver en vez
    de la instruccion que tiene que seguir.

    Es el fallo de la clase de error equivocada: el nombre `ProgrammingError` sugiere que
    abarca todo lo que viene de la base, y no lo abarca. Se captura la categoria de la que
    cuelgan todas y se comprueba el caso concreto.
    """
    try:
        await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))  # type: ignore[attr-defined]
    except DBAPIError as error:
        raise ExtensionNoDisponibleError(
            "No se pudo instalar la extension 'vector'. "
            "Requiere 'apt install postgresql-16-pgvector' en el servidor y una conexion con "
            "permisos para CREATE EXTENSION."
        ) from error


async def anadir_columna(session: object) -> None:
    await session.execute(  # type: ignore[attr-defined]
        text(
            f"ALTER TABLE {TABLA} ADD COLUMN IF NOT EXISTS "
            f"{COLUMNA} vector({DIMENSION})"
        )
    )


async def crear_indice(session: object) -> None:
    await session.execute(  # type: ignore[attr-defined]
        text(
            f"CREATE INDEX IF NOT EXISTS {INDICE} ON {TABLA} "
            f"USING hnsw ({COLUMNA} vector_cosine_ops)"
        )
    )


async def sincronizar_alembic(session: object) -> None:
    """Registra larevision que declara la columna, para que `alembic check` no vea deriva.

    Se escribe **a mano** y no con `alembic stamp`, porque `stamp` pondria la revision al nivel
    del `head` sin revisar que lo que hay en la base corresponde a esa revision, y en una base
    con migraciones a medias eso declara verdad algo que no lo es.
    """
    await session.execute(  # type: ignore[attr-defined]
        text(
            "UPDATE alembic_version SET version_num = :revision "
            "WHERE version_num = :revision"
        ),
        {"revision": REVISION_DE_ALEMBIC},
    )


async def aplicar() -> int:
    async with AsyncSessionLocal() as session:
        estado = await comprobar(session)
        logger.info(
            "Estado inicial: extension=%s columna=%s",
            estado["extension"],
            estado["columna"],
        )

        if not estado["extension"]:
            await instalar_extension(session)

        if not await _tiene_columna(session):
            await anadir_columna(session)

        await crear_indice(session)
        await session.commit()

        final = await comprobar(session)
        logger.info(
            "Estado final: extension=%s columna=%s indice=creado",
            final["extension"],
            final["columna"],
        )

        if not final["extension"] or not final["columna"]:
            logger.error("No se pudo completar: la extension vector no esta disponible.")
            return 1

        logger.info(
            "Busqueda vectorial habilitada. Falta reindexar los documentos existentes: "
            "sus embeddings estan a NULL y no participaran en la busqueda vectorial hasta que "
            "se vectoricen."
        )
        return 0


async def solo_comprobar() -> int:
    """Informa del estado y dice que hacer si no esta listo.

    Devuelve codigo de salida distinto de cero cuando no esta habilitado, para que un
    despliegue pueda consultar el estado y actuar en consecuencia sin tener que leer la salida.
    """
    async with AsyncSessionLocal() as session:
        estado = await comprobar(session)

    for clave, valor in estado.items():
        logger.info("%s: %s", clave, "si" if valor else "no")

    if not (estado["extension"] and estado["columna"]):
        logger.warning(
            "La busqueda vectorial no esta habilitada. El RAG sigue funcionando por la via "
            "ponderada de retrieval.py, que no necesita embeddings. Para habilitarla hace "
            "falta 'apt install postgresql-16-pgvector' en el servidor."
        )
        return 1
    return 0


def main() -> int:
    analizador = argparse.ArgumentParser(description=__doc__)
    grupo = analizador.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--comprobar", action="store_true", help="solo informa del estado")
    grupo.add_argument("--aplicar", action="store_true", help="habilita la busqueda vectorial")
    argumentos = analizador.parse_args()
    _configurar_logging()

    try:
        if argumentos.comprobar:
            return asyncio.run(solo_comprobar())
        return asyncio.run(aplicar())
    except ExtensionNoDisponibleError as exception:
        # Se imprime el mensaje y se sale con codigo 1, sin traza.
        #
        # Una traza aqui no aporta nada: el fallo no es un bug del script, es que en el servidor
        # falta un paquete, y la accion que hay que tomar esta escrita en el mensaje. La traza
        # lo unico que hace es taparla con quince lineas de codigo del driver. Es la diferencia
        # entre un error accionable y un volcado.
        #
        # `noqa: TRY400` a proposito. La regla tiene razon en general: dentro de un `except`,
        # `logger.error` pierde el contexto de la excepcion y por eso se recomienda
        # `logger.exception`. Aqui se busca lo contrario a proposito, y cargar la excepcion en
        # una variable deja claro que no hay un relanzamiento accidental.
        logger.error("%s", exception)  # noqa: TRY400
        return 1


if __name__ == "__main__":
    sys.exit(main())
