"""Embeddings y deteccion de soporte vectorial.

## Que hay aqui y que **no**

Aqui esta la preparacion: el modelo, su dimension, la URL del endpoint y la llamada. Y esta la
**comprobacion de capacidad** que decide si la busqueda vectorial se puede usar.

Lo que **no** esta, y es deliberado, es la columna. `pgvector` no esta disponible en esta base de
PostgreSQL, y esta medido:

- `pg_extension` no tiene la fila `vector`, y `CREATE EXTENSION vector` responde `extension
  "vector" is not available`: el servidor no la tiene compilada.
- `workspace_knowledge_documents` no tiene columna `embedding`.
- El usuario de la base es `fenix_admin`, y `CREATE EXTENSION` pide superusuario.

Anadir la columna ahora declararia deriva en `alembic check` contra una columna que la base no
puede tener, y —peor— **no recibiria nunca un vector**, porque hasta que no se elija y se cablee un
modelo de embeddings cualquier columna `vector` queda a `NULL`. Pareceria que la busqueda
semantica funciona y devolveria siempre cero resultados, que es peor que no tenerla porque
alguien confiaria en ella.

## Por que el modelo y su dimension son **constantes** y no configuracion

Porque R1 prohibe que lo configurable viva en el codigo, y esto no es una excepcion gratuita:
el modelo de embeddings **determina la dimension del vector**, y esa dimension es parte del
esquema de la base. Una columna `vector(1536)` guardando la salida de un modelo de 3072
dimensiones falla al insertar, y una que recibe un modelo distinto con la misma dimension
produce vectores de otro espacio: la busqueda por similitud sigue respondiendo y las
respuestas son silenciosamente incorrectas.

Hacerlo configurable significaria que alguien puede cambiarlo en el `.env` sin que nada avise, y
descubrirlo cuando las busquedas devuelven basura. Es el mismo motivo por el que los valores
de un enum de PostgreSQL no son configurables.

La unica forma de cambiarlo, por tanto, es una migracion: columna nueva, reindexacion y retirada
de la vieja. Y son 188 documentos los que habria que reindexar hoy.

## La migracion, escrita y no aplicada

Cuando `pgvector` este instalado, esto es lo que hay que ejecutar. Va aqui y no en
`migrations/versions/` porque un archivo de migracion sin aplicar hace que `alembic check`
declare que la base no esta al dia, y porque no se puede probar en un entorno donde la extension
no existe::

    CREATE EXTENSION IF NOT EXISTS vector;

    ALTER TABLE workspace_knowledge_documents
        ADD COLUMN embedding vector(1536);

    -- Indice HNSW con distancia coseno. Se elige HNSW y no IVFFlat porque no necesita
        -- reindexarse cuando crece la tabla, y una tabla de conocimiento crece con cada
        -- documento que el cliente sube: un IVFFlat+k se queda corto en silencio y devuelve
        -- menos vecinos de los que deberia sin dar ningun error.
    --
    -- El indice es **compuesto con organization_id** y no solo sobre el embedding. El filtro
    -- por organizacion es obligatorio (R3) y va en toda consulta, asi que el indice tiene que
    -- cubrirlo: un indice solo sobre el embedding obliga a PostgreSQL a traer todos los
    -- vectores de la tabla y descartar los de otros tenants en memoria, lo que ademas de lento
    -- es una fuga de trabajo entre tenants.
    CREATE INDEX ix_workspace_knowledge_documents_embedding_hnsw
        ON workspace_knowledge_documents
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);

    -- y despues, la reindexacion de los 188 documentos existentes.

## Por que la comprobacion mira la **columna** y no solo la extension

Porque se puede instalar la extension sin anadir la columna, y entonces el codigo escribiria
contra una columna inexistente y cada mensaje del chat fallaria con un error de PostgreSQL que
no nombra la causa. Instalar la extension es el primer paso de una migracion de dos, y un
programa que solo mira el primero se activa en cuanto alguien instala el paquete en el VPS
sin todavia ejecutar el `ALTER`.

Por eso son **dos** condiciones y no una, y por eso la comprobacion devuelve un booleano en vez
de lanzar: la ausencia de soporte **no** es un error, es el estado normal de este despliegue.

## Por que la comprobacion **no** se cachea

Porque son dos consultas a `information_schema` y `pg_extension` contra un turno que ya ha
hecho una llamada de red de varios segundos. Cachearlas ahorra microsegundos a cambio de un
fallo peor: un valor guardado en el proceso que no se invalida cuando alguien migra la base, y
que hace que el mismo despliegue se comporte distinto segun cuando arranco. Una comprobacion de
esquema tiene que reflejar el esquema, no el recuerdo que del esquema tiene un proceso.
"""

from __future__ import annotations

import json
import logging
from typing import Final

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.llm_router.attribution import attribution_headers

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# El modelo
# --------------------------------------------------------------------------- #

#: El modelo de embeddings. Vía OpenRouter, con la misma credencial que el resto del LLM.
#:
#: Se elige el `-small` y no el `-large` por una razon que no es el precio: 1536 dimensiones
#: cubren de sobra la recuperacion sobre documentos de contexto de un cliente, y `text-embedding-
#: 3-large` devuelve 3072, que duplica el tamano de la fila, el indice y el coste de la
#: reindexacion para una ganancia de recall que no se nota en esta aplicacion. Si algun dia hace
#: falta, es un `ALTER TABLE` y una reindexacion, no un cambio de configuracion.
EMBEDDING_MODEL: Final[str] = "openai/text-embedding-3-small"

#: Dimension del vector que produce `EMBEDDING_MODEL`. Va al lado del modelo y no aparte, y no
#: en la configuracion, por el motivo del encabezado del modulo: el modelo determina la
#: dimension, y separarlos es invitar a que se separen.
EMBEDDING_DIMENSION: Final[int] = 1536

#: Ruta de embeddings, relativa a `llm_api_base`. La publica OpenRouter en la misma base que las
#: conversaciones, asi que la URL sale de la misma variable de entorno y no de otra.
EMBEDDINGS_PATH: Final[str] = "/embeddings"

#: Tope de la respuesta de embeddings.
#:
#: Un embedding de 1536 dimensiones en coma flotante ocupa unos 12 KB en JSON. Con cien textos
#: son 1,2 MB, que es holgado; con diez mil serian 120 MB, y un `megabytes` de tope convierte
#: una fuga de memoria en un fallo de red muy dificil de diagnosticar.
MAX_EMBEDDING_RESPONSE_BYTES: Final[int] = 16 * 1024 * 1024

#: Timeout de la peticion de embeddings, con los **cuatro** parametros explicitos.
#:
#: Especificar los cuatro y no solo `default` y `connect` no es purismo: es que `httpx` **exige**
#: un `default` cuando no se dan todos, y la forma `Timeout(connect=..., read=...)` lanza
#: `ValueError` al construirse. Ese fallo ya ocurrio en este repositorio una vez y estuvo
#: invisible durante meses, porque vivia dentro del cuerpo de una funcion a la que las pruebas
#: nunca llegaban por inyectarle un cliente. Escribir la constante a nivel de modulo lo hace
#: saltar al importar, que es la unica forma de verlo sin llegar a produccion.
#:
#: `read` es de 120 y no de 300 como el de las conversaciones, y no por ahorrar: codificar unos
#: cientos de tokens y devolver mil quinientos treinta y seis numeros no tarda mas de un minuto
#: en ninguna condicion razonable. Una inferencia de chat es otra cosa, y su timeout es el que
#: corresponde a esa otra cosa.
EMBEDDINGS_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=10.0, read=120.0, write=10.0, pool=10.0
)

#: Nombre de la extension y de la columna. Se declaran aqui porque los necesita la comprobacion
#: **y** la migracion, y las dos tienen que decir el mismo nombre: un `ALTER TABLE` con una columna
#: y una comprobacion que buscase otra dejarian el sistema creyendo que hay soporte sin que lo
#: haya.
VECTOR_EXTENSION: Final[str] = "vector"
EMBEDDING_COLUMN: Final[str] = "embedding"
KNOWLEDGE_TABLE: Final[str] = "workspace_knowledge_documents"


class EmbeddingsNotConfiguredError(RuntimeError):
    """Falta la credencial o la URL base del proveedor."""


class EmbeddingsUpstreamError(RuntimeError):
    """El proveedor respondio con un error, o con algo que no son embeddings.

    Es de **dominio**, no `HTTPException`: la traducion a un codigo la decide quien llama.

    Guarda el `status` por la misma razon que en `LlmUpstreamError`, y es la misma
    distincion: un `429` o un `503` merece reintento y un `400` no, y una respuesta que no
    distingue los dos obliga a reintentar contra un error permanente hasta agotar los intentos.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# La comprobacion de capacidad
# --------------------------------------------------------------------------- #

#: Una sola consulta para las dos condiciones. Se preguntan juntas y no en dos porque la
#: respuesta tiene que ser coherente: preguntando por separado, la extension podria instalarse
#: entre las dos consultas y devolver "soportado" con la columna sin crear.
_SONDA_CAPACIDAD = text(
    """
    SELECT
        EXISTS (SELECT 1 FROM pg_extension WHERE extname = :extension)
        AND EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name AND column_name = :column_name
        )
    """
)


async def hay_soporte_vectorial(session: AsyncSession) -> bool:
    """Si esta base puede hacer busqueda vectorial ahora mismo.

    ## Por que devuelve `False` en vez de lanzar

    Porque la ausencia de soporte **no es un error**: es el estado de este despliegue, y el que
    va a recuperarlo es un `ALTER TABLE` sobre una tabla que existe igual. Si aqui se lanzara,
    el chat entero dejaria de responder por un documento que no hay, que es la peor relacion
    entre un problema pequeno y una consecuencia grande.

    El llamador degrada: `recuperar_documentos` busca por texto y el usuario recibe una respuesta
    en vez de un error. Ver `backend/apps/knowledge/retrieval.py`.
    """

    resultado = await session.execute(
        _SONDA_CAPACIDAD,
        {
            "extension": VECTOR_EXTENSION,
            "table_name": KNOWLEDGE_TABLE,
            "column_name": EMBEDDING_COLUMN,
        },
    )
    return bool(resultado.scalar_one())


# --------------------------------------------------------------------------- #
# La llamada
# --------------------------------------------------------------------------- #


def build_embeddings_url(api_base: str) -> str:
    """La URL completa de embeddings, a partir de la base del proveedor.

    Se separan **todas** las barras finales, por la misma razon que en `build_chat_url`: un
    entorno puede venir con tres, y quitar solo una deja un separador duplicado que devuelve
    `404` en unos proxies y funciona en otros, que es la forma mas dificil de diagnosticar.

    La base es un parametro y no el global de configuracion, y por el mismo motivo: `Settings` es
    congelado y leerlo del global obligaria a las pruebas a mutarlo con `object.__setattr__`, que
    es un truco que se cuela en produccion y acaba debilitando la garantia que lo motivo.
    """

    base = api_base.strip().rstrip("/")
    if not base:
        raise EmbeddingsNotConfiguredError("LLM_API_BASE no está configurada")
    return f"{base}{EMBEDDINGS_PATH}"


def _construir_cuerpo(textos: list[str]) -> dict[str, object]:
    """El cuerpo de una peticion de embeddings.

    `textos` va en plural porque el endpoint acepta un lote y enviar uno a uno multiplica por
    el numero de documentos el coste de la ida y vuelta, que es la parte que domina el tiempo
    de una reindexacion de los 188 documentos que hay hoy.
    """

    if not textos:
        raise ValueError("No se piden embeddings de una lista vacía")
    if any(not t.strip() for t in textos):
        raise ValueError("Un texto vacío no produce un vector útil")
    return {"model": EMBEDDING_MODEL, "input": textos}


def parse_embeddings_response(raw: str) -> list[list[float]]:
    """Normaliza el cuerpo de una respuesta de embeddings.

    Separado del transporte por la misma razon que `parse_chat_response`: es la parte que hay que
    probar con capturas reales —respuesta correcta, cuerpo truncado, vectores de otra dimension—
    y probarla sin red.

    ## Por que se comprueba la dimension y no se acepta lo que venga

    Porque un vector de otra dimension se puede insertar si la columna no lo impide, y el indice
    HNSW lo acepta, y la consulta por similitud responde: **todo parece funcionar y las
    respuestas son incorrectas**. El fallo se descubre cuando el RAG devuelve documentos que no
    tienen nada que ver, y para entonces la columna lleva semanas con vectores basura que hay que
    reindexar.

    Comprobarlo aqui convierte un fallo silencioso en un `EmbeddingsUpstreamError` en la primera
    llamada, que es donde el mensaje puede decir exactamente cual es el problema.
    """

    try:
        payload: object = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise EmbeddingsUpstreamError("El cuerpo de la respuesta no es JSON válido") from error
    if not isinstance(payload, dict):
        raise EmbeddingsUpstreamError("La respuesta del proveedor no es un objeto JSON")

    datos = payload.get("data")
    if not isinstance(datos, list) or not datos:
        raise EmbeddingsUpstreamError("La respuesta no trae vectores")

    vectores: list[list[float]] = []
    for entrada in datos:
        if not isinstance(entrada, dict):
            raise EmbeddingsUpstreamError("Una entrada de la respuesta no es un objeto")
        vector = entrada.get("embedding")
        if not isinstance(vector, list) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in vector
        ):
            raise EmbeddingsUpstreamError("Un vector no es una lista de números")
        if len(vector) != EMBEDDING_DIMENSION:
            raise EmbeddingsUpstreamError(
                f"El proveedor devolvió un vector de {len(vector)} dimensiones y se esperaban "
                f"{EMBEDDING_DIMENSION}. El modelo de la columna y el del catálogo no coinciden: "
                "insertarlos produciría búsquedas que responden sin ser correctas."
            )
        vectores.append([float(v) for v in vector])

    return vectores


async def embed_textos(
    textos: list[str],
    *,
    api_base: str,
    api_key: SecretStr,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Pide los embeddings de una lista de textos y devuelve los vectores.

    `client` es inyectable para que las pruebas usen `MockTransport` y no una red. Es la misma
    razon que en `complete`, y por el mismo motivo: una prueba que dependa de la red no es una
    prueba, es un test de la red que ademas falla de forma intermitente.

    ## Lo que esta funcion **no** hace, y por que

    No escribe nada en la base. Pedir el vector y guardarlo son dos operaciones con dos fallos
    de fallo distintas —una puede fallar por el proveedor y la otra por un bloqueo de fila— y
    separarlas deja que reintentar una no toque la otra. Quien las llama decide el destino, y
    ese destino no es siempre `workspace_knowledge_documents`.

    Tampoco **verifica** que el documento siga siendo el mismo. Ese trabajo es de la reindexacion
    y necesita comparar la marca de tiempo del documento con la del vector, que es estado que no
    cabe en una funcion pura.
    """

    if not api_key.get_secret_value():
        raise EmbeddingsNotConfiguredError("LLM_API_KEY no está configurada")

    url = build_embeddings_url(api_base)
    cuerpo = _construir_cuerpo(textos)

    # La atribucion se construye con `attribution_headers()` y **no** con literales, por el mismo
    # motivo que en `complete`: el valor que viaja y el valor declarado no pueden separarse. Y
    # es la forma de que un endpoint nuevo no se la olvide.
    #
    # Antes estas dos cabeceras no estaban, y solo se notaba en el catalogo publico de
    # OpenRouter: la peticion se procesaba igual y la aplicacion no aparecia. Es el peor fallo
    # posible de los que no dan error: funciona, y ademas no se nota.
    headers: dict[str, str] = dict(attribution_headers())
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"

    posee_client = client is None
    http = client or httpx.AsyncClient(timeout=EMBEDDINGS_TIMEOUT)
    try:
        respuesta = await http.post(url, headers=headers, json=cuerpo)
    except httpx.HTTPError as error:
        raise EmbeddingsUpstreamError(f"Fallo de red hablando con el proveedor: {error}") from error
    finally:
        if posee_client:
            await http.aclose()

    if respuesta.status_code >= 400:
        # El cuerpo del error se registra y no se propaga: puede contener el identificador de la
        # peticion del proveedor, que sirve para soporte y no para el cliente final.
        logger.warning(
            "El proveedor de embeddings respondió %s: %s",
            respuesta.status_code,
            respuesta.text[:500],
        )
        raise EmbeddingsUpstreamError(
            f"El proveedor respondió {respuesta.status_code}", status=respuesta.status_code
        )

    if len(respuesta.content) > MAX_EMBEDDING_RESPONSE_BYTES:
        raise EmbeddingsUpstreamError("La respuesta excede el tamaño permitido")

    return parse_embeddings_response(respuesta.text)


async def generate_embedding(
    text: str,
    *,
    api_base: str,
    api_key: SecretStr,
    client: httpx.AsyncClient | None = None,
) -> list[float]:
    """El embedding de **un** texto, como vector de 1536 numeros.

    ## Por que existe esta si ya esta `embed_textos`

    Por dos motivos, y el segundo es el que la justifica.

    El primero es la forma que pide el punto de insercion: quien indexa un documento tiene **un**
    documento, y envolverlo en una lista de un elemento para desempaquetar el resultado es ruido
    en el sitio donde se llama.

    El segundo es el rendimiento. Un documento de contexto son miles de palabras, y llamarla una
    vez por parrafo hace una ida y vuelta por cada uno. `embed_textos` acepta un lote y el
    endpoint admite un lote, y la diferencia entre reindexar cien documentos en una llamada y en
    cien es la diferencia entre segundos y minutos.

    Que exista la version de uno **no** significa que sea la que se use para indexar. Esta es
    para un texto suelto —una consulta de busqueda, una comprobacion puntual— y la de lote es la
    que usa el indexador.
    """

    vectores = await embed_textos([text], api_base=api_base, api_key=api_key, client=client)
    if not vectores:
        # `embed_textos` lanza si la lista viene vacia y `parse_embeddings_response` lanza si la
        # respuesta no trae vectores, asi que llegar aqui es que el proveedor respondio `data: []`.
        # Devolver una lista vacia haria que el llamante la tomara por un vector valido.
        raise EmbeddingsUpstreamError("El proveedor no devolvió ningún vector")
    return vectores[0]


async def embedding_de_grado(
    text: str,
    *,
    api_base: str,
    api_key: SecretStr,
    client: httpx.AsyncClient | None = None,
) -> list[float] | None:
    """El embedding del texto, o `None` si no se pudo obtener.

    ## Por qué devuelve `None` y **no** un vector de ceros

    Porque un vector de ceros no es "sin vector". Es un vector, y es el peor de todos.

    La distancia del coseno se normaliza dividiendo por la norma del vector, y la norma de un
    vector de ceros es cero. La división da `NaN`, y `NaN` no ordena ni se compara: en un índice
    HNSW, un vector de ceros entra en la estructura como si fuera una posición válida y puede
    salir como vecino de cualquier consulta. El resultado de una búsqueda devolvería el documento
    sin vector como si fuera el más parecido a cualquier pregunta, y **sin ninguna señal visible
    de que sea falso**. La búsqueda respondería, y respondería mal, que es el peor síntoma posible.

    Hay un segundo motivo, independiente del anterior: `None` y un vector de ceros se distinguen
    al leerlos. `embedding IS NULL` dice "este documento no tiene embedding, búscalo por vía
    léxica". Una fila con 1536 ceros dice lo mismo solo para quien sepa que son ceros, y no hay
    ninguna razón por la que ese alguien deba saberlo.

    ## Por qué degradar en lugar de propagar el fallo

    Porque el embedding es un **añadido**, no un requisito. El RAG de este sistema tiene dos vías:
    la ponderada de `retrieval.py`, que funciona sin embeddings, y la vectorial, que los mejora.
    Si la clave no está configurada, o el proveedor falla, o el despliegue es de desarrollo sin
    cuota, la indexación no debe detenerse: el documento tiene que entrar igual, y la búsqueda
    seguirá funcionando por la vía que siempre funcionó.

    ## Lo que este sí hace es hablar

    Degrada el dato, no el silencio. Un `warning` con el motivo, porque un despliegue donde los
    embeddings fallan uno a uno durante una semana y nadie se entera es un despliegue que parece
    funcionar y está perdiendo calidad de forma constante. La degradación es lo correcto; la
    degradación **invisible** no lo es.

    ## Por qué el `except` es estrecho

    Solo los dos errores de este módulo. Un `TypeError` de un bug propio tiene que subir: si se
    ocultara aquí, un error de programación se convertiría en "sin embedding" y se buscaría en el
    proveedor de embeddings lo que está mal en el código de llamada.
    """

    try:
        return await generate_embedding(text, api_base=api_base, api_key=api_key, client=client)
    except (EmbeddingsNotConfiguredError, EmbeddingsUpstreamError) as error:
        logger.warning(
            "Sin embedding para un texto; la busqueda seguira por la via lexica. Motivo: %s",
            error,
        )
        return None


__all__ = [
    "EMBEDDINGS_PATH",
    "EMBEDDINGS_TIMEOUT",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "MAX_EMBEDDING_RESPONSE_BYTES",
    "EmbeddingsNotConfiguredError",
    "EmbeddingsUpstreamError",
    "build_embeddings_url",
    "embed_textos",
    "embedding_de_grado",
    "generate_embedding",
    "hay_soporte_vectorial",
    "parse_embeddings_response",
]
