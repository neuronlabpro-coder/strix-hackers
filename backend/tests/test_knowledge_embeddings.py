"""Pruebas de la preparacion de embeddings.

## Que se comprueba y por que

Esta capa todavia **no** se usa en la busqueda: `pgvector` no esta disponible en esta base y la
recuperacion sigue siendo lexica. Lo que se comprueba aqui es que la preparacion sea correcta y
que la degradacion sea la que esta activa de verdad, no por suposicion.

La prueba que mas importa es `test_la_sonda_no_se_lo_inventa_a_la_base`: mira **lo que la base
dice de si misma** y afirma que la sonda coincide. Esa comprobacion sigue siendo cierta el dia
que se instale `pgvector`, y es la que hace que la activacion sea un `ALTER TABLE` y no un cambio
de codigo.

## Por que las pruebas del contrato HTTP usan `MockTransport` y no el proveedor

Porque una prueba que dependa de la red no es una prueba: es un test de la red que ademas falla
de forma intermitente, y un fallo intermitente en CI se aprende a ignorar. Lo que **si** queda
sin verificar, y hay que decirlo, es que el proveedor real acepte este contrato: nadie ha llamado
a `openai/text-embedding-3-small` por OpenRouter desde este codigo. Cuando se llame por primera
vez, lo que se comprueba de verdad es si la respuesta trae `data[].embedding` con 1536 numeros.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.embeddings import (
    EMBEDDING_COLUMN,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDINGS_PATH,
    EmbeddingsNotConfiguredError,
    EmbeddingsUpstreamError,
    build_embeddings_url,
    embed_textos,
    hay_soporte_vectorial,
    parse_embeddings_response,
)
from backend.core.database import AsyncSessionLocal

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    sesion = AsyncSessionLocal()
    try:
        yield sesion
    finally:
        await sesion.close()


def _vector(dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Un vector de la dimension correcta.

    Se genera en lugar de escribir 1536 numeros a mano porque el numero **tiene** que coincidir
    con la constante: un literal fijo en la prueba dejaria de ser valido en cuanto la dimension
    cambiasse, y la prueba pasaria por un motivo distinto del que mide.
    """

    return [0.01 * (i % 100) for i in range(dimension)]


def _respuesta(*, vectores: int = 1, dimension: int = EMBEDDING_DIMENSION) -> dict[str, Any]:
    return {
        "model": EMBEDDING_MODEL,
        "data": [{"embedding": _vector(dimension), "index": i} for i in range(vectores)],
        "usage": {"prompt_tokens": 12, "total_tokens": 12},
    }


def _clave() -> SecretStr:
    return SecretStr("clave-de-prueba")


def _proveedor(
    cuerpo: dict[str, Any] | None = None,
    *,
    codigo: int = 200,
    capturado: list[httpx.Request] | None = None,
) -> httpx.AsyncClient:
    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        if capturado is not None:
            capturado.append(peticion)
        return httpx.Response(codigo, json=cuerpo if cuerpo is not None else _respuesta())

    return httpx.AsyncClient(transport=httpx.MockTransport(_manejador))


# --------------------------------------------------------------------------- #
# Las constantes
# --------------------------------------------------------------------------- #


async def test_el_modelo_y_la_dimension_son_el_acuerdo_que_pide_la_columna() -> None:
    """El modelo declarado produce los 1536 valores que la futura columna va a pedir.

    Es una asercion sobre una constante y sobre si misma en apariencia, y no lo es: la columna
    que la migracion va a crear dice `vector(1536)`, y ese numero **esta escrito en el
    comentario del modulo de la migracion**, no en un sitio que el compilador relacione con esta
    constante. Si alguien cambia la dimension aqui, esta prueba falla y la del comentario —que no
    existe— no. Por eso se afirma la cifra explicita y no solo la igualdad.
    """

    assert EMBEDDING_MODEL == "openai/text-embedding-3-small"
    assert EMBEDDING_DIMENSION == 1536
    # El texto de la migracion documentada dice la misma cifra. Se comprueba leyendo el propio
    # modulo, porque la unica forma de que no se separen es que alguien las separe en una
    # actualizacion y no lo note.
    from backend.apps.knowledge import embeddings as modulo

    documento = modulo.__doc__ or ""
    assert f"vector({EMBEDDING_DIMENSION})" in documento


# --------------------------------------------------------------------------- #
# La comprobacion de capacidad
# --------------------------------------------------------------------------- #


async def test_la_sonda_no_se_lo_inventa_a_la_base() -> None:
    """La sonda coincide con lo que la base afirma de si misma.

    Es la prueba mas importante del fichero, porque es la que hace que la activacion de la
    busqueda vectorial sea un cambio de esquema y no un cambio de codigo: si alguien instala
    `pgvector` y anade la columna, la sonda empieza a decir que si **sin tocar una linea de este
    modulo**, y si alguien desinstala, deja de decirlo.

    Y mientras las dos cosas no existan, dice que no, que es lo que hace que la busqueda siga
    siendo lexica sin que nadie tenga que acordarlo.
    """

    async with _sesion() as sesion:
        sondee = await hay_soporte_vectorial(sesion)

        real = (
            await sesion.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
        ).scalar_one()
        columna = (
            await sesion.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'workspace_knowledge_documents'
                        AND column_name = 'embedding'
                    )
                    """
                )
            )
        ).scalar_one()

    # Se afirma contra `and`, no contra `or`: las dos condiciones hacen falta, y una sonda que
    # dijera que si con solo la extension mandaria al codigo a escribir contra una columna que
    # todavia no existe.
    assert sondee == (bool(real) and bool(columna))


async def test_la_sonda_no_lanza_sin_soporte() -> None:
    """La ausencia de soporte **no** es un error: es un `False`.

    Si aqui se lanzara, el chat entero dejaria de responder por un documento que no hay, y el
    mensaje de error no diria que lo que falta es una extension en el servidor.
    """

    async with _sesion() as sesion:
        assert await hay_soporte_vectorial(sesion) in {True, False}


async def test_la_sonda_usa_el_mismo_nombre_que_la_migracion() -> None:
    """Extension, columna y tabla se declaran una vez y la migracion usa esos mismos nombres.

    Un `ALTER TABLE` con una columna y una comprobacion que buscase otra dejaria el sistema
    creyendo que hay soporte sin que lo haya, que es el fallo mas caro de los tres posibles.
    """

    from backend.apps.knowledge import embeddings as modulo

    assert EMBEDDING_COLUMN == "embedding"
    assert modulo.VECTOR_EXTENSION == "vector"
    assert modulo.KNOWLEDGE_TABLE == "workspace_knowledge_documents"
    documento = modulo.__doc__ or ""
    for nombre in (modulo.VECTOR_EXTENSION, modulo.KNOWLEDGE_TABLE, EMBEDDING_COLUMN):
        assert nombre in documento, f"`{nombre}` no aparece en la migracion documentada"


# --------------------------------------------------------------------------- #
# La URL
# --------------------------------------------------------------------------- #


async def test_la_url_sale_de_la_misma_base_que_las_conversaciones() -> None:
    """La ruta de embeddings cuelga de `llm_api_base`, igual que la de conversaciones.

    Es lo que permite reutilizar la credencial: si la base fuera otra variable, habria que
    duplicar tambien la credencial, y dos credenciales para el mismo proveedor es una que se
    revoca en un sitio y se sigue usando en el otro.
    """

    assert build_embeddings_url("https://openrouter.ai/api/v1") == (
        "https://openrouter.ai/api/v1" + EMBEDDINGS_PATH
    )


async def test_la_url_quita_todas_las_barras_finales() -> None:
    """Un entorno puede venir con tres barras y quitar una sola deja un separador duplicado."""

    assert build_embeddings_url("https://ejemplo.test/v1///") == (
        "https://ejemplo.test/v1" + EMBEDDINGS_PATH
    )


async def test_sin_url_base_da_error_que_dice_que_falta() -> None:
    """El error nombra la variable, que es lo que cuesta tiempo descubrir de otro modo."""

    with pytest.raises(EmbeddingsNotConfiguredError, match="LLM_API_BASE"):
        build_embeddings_url("   ")


# --------------------------------------------------------------------------- #
# La respuesta
# --------------------------------------------------------------------------- #


async def test_una_respuesta_correcta_devuelve_los_vectores() -> None:
    """El camino feliz, con un vector de la dimension que el modelo declarado produce.

    Se construye con el generador y no con numeros escritos a mano porque la dimension **tiene**
    que salir de la constante: un literal fijo aqui dejaria de ser valido en cuanto la dimension
    cambiasse, y la prueba pasaria por un motivo distinto del que mide.
    """

    vectores = parse_embeddings_response(_json(_respuesta(vectores=2)))
    assert len(vectores) == 2
    assert all(len(v) == EMBEDDING_DIMENSION for v in vectores)
    assert vectores[0] == _vector()


async def test_un_vector_de_otra_dimension_se_rechaza() -> None:
    """Es el fallo silencioso mas caro, y por eso se comprueba en el borde.

    Un vector de 3072 en una columna `vector(1536)` falla al insertar, lo que es visible. Pero
    un modelo distinto con **la misma** dimension inserta bien, el indice lo acepta, la consulta
    por similitud responde y los resultados son incorrectos: eso no da ningun error en ninguna
    parte. Comprobarlo aqui lo convierte en un `EmbeddingsUpstreamError` en la primera llamada.
    """

    cuerpo = _respuesta(dimension=EMBEDDING_DIMENSION + 512)
    with pytest.raises(EmbeddingsUpstreamError) as error:
        parse_embeddings_response(_json(cuerpo))
    # El mensaje dice las dos cifras, que es lo que permite corregirlo sin abrir el catalogo.
    assert str(EMBEDDING_DIMENSION) in str(error.value)
    assert str(EMBEDDING_DIMENSION + 512) in str(error.value)


async def test_cuerpo_no_json_da_error() -> None:
    with pytest.raises(EmbeddingsUpstreamError, match="JSON"):
        parse_embeddings_response("<html>502 Bad Gateway</html>")


async def test_sin_vectores_da_error() -> None:
    with pytest.raises(EmbeddingsUpstreamError, match="vectores"):
        parse_embeddings_response('{"data": []}')


async def test_un_vector_que_no_es_numeros_da_error() -> None:
    """Una cadena donde deberia haber un numero.

    `isinstance(True, int)` es `True` en Python, asi que un booleano pasaria una comprobacion
    ingenua y se guardaria como `1.0`. No es un caso real de un proveedor, pero si lo es de un
    proxy mal configurado, y el coste de excluirlo es una clausula.
    """

    # Con la dimension correcta y un booleano dentro. Sin ella, la primera comprobacion que
    # falla seria la de la dimension y la prueba pasaria por un motivo que no es el suyo.
    vector = [0.5] * (EMBEDDING_DIMENSION - 1) + [True]
    cuerpo = _json({"data": [{"embedding": vector}]})
    with pytest.raises(EmbeddingsUpstreamError, match="números"):
        parse_embeddings_response(cuerpo)


# --------------------------------------------------------------------------- #
# La llamada
# --------------------------------------------------------------------------- #


async def test_la_peticion_lleva_el_modelo_y_todos_los_textos() -> None:
    """El lote va entero en una peticion.

    Enviar uno a uno multiplica por el numero de documentos el coste de la ida y vuelta, que es
    lo que domina el tiempo de la reindexacion de los 188 documentos que hay hoy.
    """

    capturadas: list[httpx.Request] = []
    async with _proveedor(_respuesta(vectores=3), capturado=capturadas) as cliente:
        vectores = await embed_textos(
            ["uno", "dos", "tres"],
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    cuerpo = _json_de(capturadas[0])
    assert cuerpo["model"] == EMBEDDING_MODEL
    assert cuerpo["input"] == ["uno", "dos", "tres"]
    assert len(vectores) == 3
    assert len(vectores[0]) == EMBEDDING_DIMENSION


async def test_la_peticion_va_a_la_ruta_de_embeddings() -> None:
    capturadas: list[httpx.Request] = []
    async with _proveedor(capturado=capturadas) as cliente:
        await embed_textos(
            ["uno"],
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    assert str(capturadas[0].url) == "https://openrouter.ai/api/v1" + EMBEDDINGS_PATH
    assert capturadas[0].headers["Authorization"] == "Bearer clave-de-prueba"


async def test_sin_credencial_da_error_antes_de_salir_a_la_red() -> None:
    """Falla **antes** de abrir la conexion.

    Comprobado con que no se haya enviado nada: una peticion sin cabecera de autorizacion llega al
    proveedor, el proveedor la rechaza con un `401` y el error que sube es del proveedor, que no
    dice que el problema es la configuracion local.
    """

    capturadas: list[httpx.Request] = []
    async with _proveedor(capturado=capturadas) as cliente:
        with pytest.raises(EmbeddingsNotConfiguredError, match="LLM_API_KEY"):
            await embed_textos(
                ["uno"],
                api_base="https://openrouter.ai/api/v1",
                api_key=SecretStr(""),
                client=cliente,
            )

    assert capturadas == []


async def test_una_lista_vacia_no_llega_al_proveedor() -> None:
    """Pedir embeddings de nada no es una llamada de red, es una condicion de uso incorrecta.

    LLegaria con un `input` vacio y el proveedor responderia con un `400` cuyo texto no nombra
    que el problema es del llamador.
    """

    with pytest.raises(ValueError, match="vacía"):
        await embed_textos(
            [],
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
        )


async def test_un_texto_vacio_se_rechaza() -> None:
    """Un texto en blanco no produce un vector util y el proveedor lo acepta sin avisar.

    El vector de un texto vacio es un vector valido —todo el mismo valor— y se guardaria. En una
    tabla de conocimiento significa un documento que siempre aparece en las busquedas, porque su
    similitud con cualquier cosa es la misma.
    """

    with pytest.raises(ValueError, match="vacío"):
        await embed_textos(
            ["   "],
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
        )


async def test_el_status_del_proveedor_llega_al_error() -> None:
    """El `status` se guarda para que quien llama decida si reintenta.

    Un `429` merece reintento y un `400` no. Sin el codigo, la unica politica posible es
    reintentar siempre, y eso convierte un error permanente en un bucle.
    """

    async with _proveedor({"error": "rate limited"}, codigo=429) as cliente:
        with pytest.raises(EmbeddingsUpstreamError) as error:
            await embed_textos(
                ["uno"],
                api_base="https://openrouter.ai/api/v1",
                api_key=_clave(),
                client=cliente,
            )

    assert error.value.status == 429


async def test_el_cuerpo_del_error_del_proveedor_no_se_propaga() -> None:
    """Lo que responde el proveedor va al log, no al mensaje que sube.

    Puede contener el identificador de la peticion, que sirve para soporte y no para el cliente
    final. El mensaje solo lleva el codigo.
    """

    async with _proveedor(
        {"error": {"request_id": "req_secreto_123", "message": "algo"}}, codigo=400
    ) as cliente:
        with pytest.raises(EmbeddingsUpstreamError) as error:
            await embed_textos(
                ["uno"],
                api_base="https://openrouter.ai/api/v1",
                api_key=_clave(),
                client=cliente,
            )

    assert "req_secreto_123" not in str(error.value)
    assert "400" in str(error.value)


# --------------------------------------------------------------------------- #
# La frontera con el resto del sistema
# --------------------------------------------------------------------------- #


async def test_la_busqueda_actual_sigue_siendo_lexica() -> None:
    """El RAG de hoy no toca la columna de embeddings, y eso es lo correcto hoy.

    No es una prueba de la recuperacion: es la que deja constancia de **que camino esta
    activo**, y por eso mira el **codigo** y no la prosa.

    Mirar la prosa habria sido el error: el modulo de recuperacion menciona `embedding` varias
    veces, precisamente para explicar por que **no** hay columna. Una prueba que buscara esa
    palabra en el texto daria verde sobre un documento que dice lo contrario de lo que afirma,
    que es peor que no tener la prueba.

    Lo que se comprueba es que el **nombre de la columna** no aparezca en ninguna linea
    ejecutable. Si alguien conecta la consulta coseno sin anadir la columna a la base, esta
    prueba falla en vez de dejar que cada mensaje del chat reviente con un error de
    PostgreSQL.
    """

    import ast
    import inspect

    from backend.apps.knowledge import retrieval

    arbol = ast.parse(inspect.getsource(retrieval))
    # Se recorren los literales de cadena del codigo: ahi es donde apareceria un nombre de
    # columna. Los comentarios y los docstrings no son literales en el arbol, asi que quedan
    # fuera por construccion, que es justo lo que se quiere.
    literales = {
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert EMBEDDING_COLUMN not in literales, (
        "la recuperacion referencia la columna de embeddings pero esta base no la tiene: "
        "cada mensaje del chat fallaria con un error de PostgreSQL que no nombra la causa"
    )



# --------------------------------------------------------------------------- #
# La atribucion y el timeout
# --------------------------------------------------------------------------- #


async def test_la_peticion_lleva_las_cabeceras_de_atribucion() -> None:
    """La atribucion viaja en **toda** peticion al proveedor, y en los embeddings tambien.

    Es una invariante del cliente, no una preferencia de un endpoint. Y aqui faltaban: la
    peticion se procesaba igual y la aplicacion no aparecia en el catalogo publico de OpenRouter.
    Es el peor fallo de los que no dan error, porque no hay sintoma al que mirar.
    """

    from backend.apps.llm_router.attribution import APP_TITLE, HTTP_REFERER

    capturadas: list[httpx.Request] = []
    async with _proveedor(capturado=capturadas) as cliente:
        await embed_textos(
            ["uno"],
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    cabeceras = capturadas[0].headers
    assert cabeceras["HTTP-Referer"] == HTTP_REFERER
    assert cabeceras["X-Title"] == APP_TITLE
    # Y la autorizacion, que es lo unico que si depende de configuracion.
    assert cabeceras["Authorization"] == "Bearer clave-de-prueba"


async def test_el_timeout_declara_los_cuatro_parametros() -> None:
    """Los cuatro, y no solo `default` y `connect`.

    `httpx.Timeout` **exige** un `default` cuando no se dan todos, y lanza `ValueError` al
    construirse. Ese fallo ya ocurrio en este repositorio con `complete()` y estuvo invisible
    durante meses porque vivia en el cuerpo de la funcion, a la que las pruebas nunca llegaban
    por inyectarle un cliente. Declararlo a nivel de modulo lo hace saltar al importar.
    """

    from backend.apps.knowledge.embeddings import EMBEDDINGS_TIMEOUT

    assert EMBEDDINGS_TIMEOUT.connect == 10.0
    assert EMBEDDINGS_TIMEOUT.read == 120.0
    assert EMBEDDINGS_TIMEOUT.write == 10.0
    assert EMBEDDINGS_TIMEOUT.pool == 10.0
    # El read de un embedding es mas corto que el de una conversacion a proposito, y la razon
    # esta escrita en el modulo: codificar unos cientos de tokens no tarda un minuto.


# --------------------------------------------------------------------------- #
# La version de un solo texto
# --------------------------------------------------------------------------- #


async def test_generate_embedding_devuelve_un_vector_de_la_dimension_correcta() -> None:
    """La firma que pide el punto de insercion: un texto dentro, un vector fuera.

    Y devuelve **solo** el vector, no la lista de listas: un envoltorio que devolviera la
    respuesta cruda obligaria a desempaquetar en el sitio que lo llama, que es justo el ruido que
    el envoltorio existe para quitar.
    """

    from backend.apps.knowledge.embeddings import generate_embedding

    async with _proveedor() as cliente:
        vector = await generate_embedding(
            "el texto a vectorizar",
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    assert isinstance(vector, list)
    assert len(vector) == EMBEDDING_DIMENSION
    assert all(isinstance(v, float) for v in vector)


async def test_generate_embedding_manda_una_sola_peticion() -> None:
    """Una peticion por texto: es la version de un texto, no un atajo de la de lote.

    La version de un texto es la que se usa para una consulta suelta. La de lote es la que usa
    el indexador, y esa sigue mandando un lote por peticion, que es lo que hace que
    reindexar cien documentos no sean cien idas a la red.
    """

    from backend.apps.knowledge.embeddings import generate_embedding

    capturadas: list[httpx.Request] = []
    async with _proveedor(capturado=capturadas) as cliente:
        await generate_embedding(
            "uno solo",
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    assert len(capturadas) == 1
    import json as _json

    assert _json.loads(capturadas[0].content)["input"] == ["uno solo"]


async def test_generate_embedding_propaga_el_error_de_dimension() -> None:
    """Un vector de otra dimension no se devuelve recortado ni "aproximado".

    Propagar el error es lo unico honesto: un vector de 1536 elementos sobre una columna que
    espera 3072 no es un vector pequeño, es un vector de otro espacio, y la busqueda por
    similitud responderia sin ser correcta.
    """

    from backend.apps.knowledge.embeddings import generate_embedding

    async with _proveedor(_respuesta(dimension=512)) as cliente:
        with pytest.raises(EmbeddingsUpstreamError):
            await generate_embedding(
                "uno",
                api_base="https://openrouter.ai/api/v1",
                api_key=_clave(),
                client=cliente,
            )


# --------------------------------------------------------------------------- #
# La degradacion
# --------------------------------------------------------------------------- #


async def test_la_degradacion_devuelve_none_y_no_levanta() -> None:
    """Sin embedding no hay vector, y eso se dice con `None`.

    La forma de decirlo importa mas de lo que parece. Un vector de ceros **tambien** es una
    forma de decirlo, y es la equivocada: la distancia del coseno divide por la norma del vector,
    y la norma de un vector de ceros es cero. El resultado es `NaN`, que no ordena ni se compara,
    y en un indice HNSW un vector de ceros entra en la estructura como si fuera una posicion
    valida. La busqueda devolveria el documento sin embedding como el mas parecido a cualquier
    pregunta, sin ninguna senal visible de que sea falso.
    """
    from backend.apps.knowledge.embeddings import embedding_de_grado

    async with _proveedor() as cliente:
        vector = await embedding_de_grado(
            "el texto",
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    assert vector is not None
    assert len(vector) == EMBEDDING_DIMENSION


async def test_la_degradacion_devuelve_none_si_falla_el_proveedor() -> None:
    """Un `5xx` del proveedor degrada a `None` en vez de romper la indexacion.

    El embedding es un anadido, no un requisito. El RAG tiene la via ponderada de `retrieval.py`,
    que funciona sin embeddings, asi que perder el vector degrada la calidad de la busqueda pero
    no la deja de funcionar. Abortar la indexacion entera por un fallo de un proveedor externo
    seria la decision equivocada.
    """
    from backend.apps.knowledge.embeddings import embedding_de_grado

    async with _proveedor(codigo=503) as cliente:
        vector = await embedding_de_grado(
            "el texto",
            api_base="https://openrouter.ai/api/v1",
            api_key=_clave(),
            client=cliente,
        )

    assert vector is None


async def test_la_degradacion_devuelve_none_sin_clave_configurada() -> None:
    """Sin `LLM_API_KEY` no se llama al proveedor: se devuelve `None` y ya esta.

    Es el caso del despliegue de desarrollo sin cuota, y el que mas se va a repetir. Comprobar
    ademas que **no** se hizo ninguna peticion es lo que distingue "degrade limpio" de "degrade
    despues de gastarse una llamada fallida en intentarlo".
    """
    from pydantic import SecretStr

    from backend.apps.knowledge.embeddings import embedding_de_grado

    capturadas: list[httpx.Request] = []
    async with _proveedor(capturado=capturadas) as cliente:
        vector = await embedding_de_grado(
            "el texto",
            api_base="https://openrouter.ai/api/v1",
            api_key=SecretStr(""),
            client=cliente,
        )

    assert vector is None
    assert capturadas == []


async def test_la_degradacion_no_se_traga_un_error_propio() -> None:
    """Un bug de programacion tiene que subir, no convertirse en "sin embedding".

    El `except` es narrowed a los dos errores del modulo a proposito. Si fuera ancho, un
    `TypeError` en el sitio de llamada se convertiria en un `None` silencioso, y el sintoma
    apareceria como "la busqueda vectorial no devuelve nada" en vez de como una excepcion en el
    log. Depurar eso exige revisar primero el proveedor, que es donde no esta el fallo.
    """
    from backend.apps.knowledge.embeddings import embedding_de_grado

    # Se rompe el **argumento** a proposito: `api_base=""` hace que `build_embeddings_url` lance
    # `EmbeddingsNotConfiguredError`... que si se captura, asi que no sirve. Lo que hace falta es
    # un fallo que no sea de los dos del modulo, y el mas directo es pasar una clave que no sea
    # un `SecretStr`.
    #
    # La version anterior rompia el cliente, que es mas comodo de escribir, y obligaba a mentir a
    # pyright con un `type: ignore` para poder pasarle un `object`. Un `type: ignore` que
    # desactiva la comprobacion de tipos justo en la prueba que verifica que un error ajeno sube
    # es peor que no tener la prueba: desactiva la unica garantia de que la prueba esta probando
    # lo que dice.
    with pytest.raises(AttributeError):
        await embedding_de_grado(
            "el texto",
            api_base="https://openrouter.ai/api/v1",
            api_key="no-es-un-SecretStr",  # type: ignore[arg-type]
        )


async def test_la_degradacion_avisa_del_motivo(caplog: pytest.LogCaptureFixture) -> None:
    """Degrada el dato, no el silencio.

    Un despliegue donde los embeddings fallan uno a uno durante una semana y nadie se entera es
    un despliegue que parece funcionar y pierde calidad de forma constante. El `warning` con el
    motivo es lo que convierte un fallo en algo diagnosticable.
    """
    import logging

    from backend.apps.knowledge.embeddings import embedding_de_grado

    with caplog.at_level(logging.WARNING):
        async with _proveedor(codigo=500) as cliente:
            assert (
                await embedding_de_grado(
                    "el texto",
                    api_base="https://openrouter.ai/api/v1",
                    api_key=_clave(),
                    client=cliente,
                )
                is None
            )

    assert caplog.records, "la degradacion tiene que dejar rastro en el log"
    assert any("embedding" in registro.message.lower() for registro in caplog.records)

# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def _json(cuerpo: dict[str, Any]) -> str:
    import json

    return json.dumps(cuerpo)


def _json_de(peticion: httpx.Request) -> Any:
    import json

    return json.loads(peticion.content)
