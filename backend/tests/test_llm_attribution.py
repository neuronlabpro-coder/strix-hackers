"""Pruebas de la atribución pública ante OpenRouter y del cliente HTTP del proveedor.

## Qué se comprueba y por qué

La atribución es el tipo de cosa que no falla: si la cabecera falta, la petición **se procesa
igual** y lo único que cambia es que la aplicación no aparece en el ranking público de
OpenRouter. Nadie ve un error, ningún log se queja, y el daño es silencioso y permanente.

Por eso las pruebas se centran en la **invariante**, no en un ejemplo:

1. Que la declaración es inmutable. Una cabecera que se puede mutar desde fuera desaparece
   sin que el código deje de decir la verdad.
2. Que **toda** petición pasa por el sitio que las añade. Se comprueba sobre la función de
   construcción de cabeceras y sobre el envío real con un transporte de mentira, que es la
   única forma de observar lo que sale sin red.
3. Que el entorno del contenedor lleva lo mismo que las cabeceras. Si divergen, la
   atribución funciona a medias y **parece** que funciona.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from backend.apps.llm_router import client as llm_client
from backend.apps.llm_router.attribution import (
    APP_TITLE,
    ATTRIBUTION_HEADERS,
    ENV_APP_TITLE,
    ENV_HTTP_REFERER,
    HEADER_APP_TITLE,
    HEADER_HTTP_REFERER,
    HTTP_REFERER,
    attribution_environment,
    attribution_headers,
)
from backend.apps.llm_router.pricing import compute_charge
from backend.workers.runner.docker_client import DockerClient
from backend.workers.runner.sandbox import StrixSandboxManager

# --------------------------------------------------------------------------- #
# La declaración
# --------------------------------------------------------------------------- #



def _sandbox_de_prueba() -> StrixSandboxManager:
    """Un gestor de sandbox con cliente de Docker de mentira.

    `container_environment()` no toca el cliente, pero el constructor lo exige. Se le pasa un
    doble por la misma razón que en el resto de pruebas del sandbox: levantar un cliente
    real intentaría conectarse al demonio de Docker, que en un entorno de CI no existe, y el
    fallo sería «no se encuentra el archivo» en lugar de un fallo de la atribución.
    """

    return StrixSandboxManager(
        run_id="00000000-0000-4000-8000-000000000001",
        target="https://ejemplo.invalid",
        llm_model="test/model",
        client=cast(DockerClient, MagicMock()),
    )

def test_los_valores_declarados_son_los_requeridos() -> None:
    """La identidad pública es la de Mind Guard, y está escrita entera.

    Se compara **valor a valor** contra los literales, no solo contra la constante de al lado.
    Una prueba que comparase `ATTRIBUTION_HEADERS["X-Title"] == APP_TITLE` pasaría aunque
    ambas tuvieran el valor equivocado, que es el fallo que hay que cazar.
    """

    assert HTTP_REFERER == "https://mindguard.tech"
    assert APP_TITLE == "Mind Guard Fenix"
    assert dict(ATTRIBUTION_HEADERS) == {
        "HTTP-Referer": "https://mindguard.tech",
        "X-Title": "Mind Guard Fenix",
    }


def test_las_cabeceras_declared_son_inmutables() -> None:
    """No se pueden modificar desde fuera.

    Un `dict` normal devuelto desde una función se puede mutar, y esa es la forma más
    discreta de perder la atribución: el código sigue diciendo la verdad, las peticiones
    dejan de llevar la cabecera, y no hay ningún error en ninguna parte.
    """

    with pytest.raises(TypeError):
        ATTRIBUTION_HEADERS[HEADER_HTTP_REFERER] = "https://otro-sitio.example"  # type: ignore[index]
    with pytest.raises(TypeError):
        ATTRIBUTION_HEADERS["X-Extra"] = "valor"  # type: ignore[index]
    with pytest.raises(TypeError):
        del ATTRIBUTION_HEADERS[HEADER_APP_TITLE]  # type: ignore[attr-defined]

    # Y la declaración sigue intacta después de los tres intentos.
    assert ATTRIBUTION_HEADERS[HEADER_APP_TITLE] == APP_TITLE
    assert len(ATTRIBUTION_HEADERS) == 2


def test_la_copia_devuelta_es_mutable_e_independiente() -> None:
    """La función devuelve una copia, no la declaración.

    Quien la recibe necesita añadir `Authorization` o `Content-Type`, y si devolviera el
    `MappingProxyType` se comería un `TypeError` en el camino caliente. Que sea una copia
    además significa que mutarla no afecta a la declaración.
    """

    primera = attribution_headers()
    primera["Authorization"] = "Bearer algo"
    primera[HEADER_HTTP_REFERER] = "https://otro-sitio.example"

    segunda = attribution_headers()
    assert "Authorization" not in segunda
    assert segunda[HEADER_HTTP_REFERER] == HTTP_REFERER
    assert "Authorization" not in ATTRIBUTION_HEADERS


def test_no_faltan_ninguna_de_las_dos_cabeceras() -> None:
    """Las dos cabeceras que OpenRouter exige, y solo esas.

    Se comprueba el conjunto completo, no que "esté la de Referer". Una cabecera de más no
    rompe nada hoy, pero una de menos sí, y el conjunto exacto es lo que lo dice.
    """

    assert set(attribution_headers()) == {HEADER_HTTP_REFERER, HEADER_APP_TITLE}


# --------------------------------------------------------------------------- #
# La invariante: toda petición lleva las cabeceras
# --------------------------------------------------------------------------- #


def test_las_cabeceras_de_la_peticion_llevan_la_atribucion() -> None:
    """La función por la que pasa toda petición a `/chat/completions` las incluye.

    Es la invariante del módulo, comprobada sobre el sitio donde se decide. Cualquier ruta
    HTTP nueva que no pase por aquí la rompería, y por eso la prueba está sobre esta función
    y no sobre el cliente entero: un cliente se puede probar con rutas que no existen.
    """

    cabeceras = dict(llm_client._build_request_headers())

    assert cabeceras[HEADER_HTTP_REFERER] == HTTP_REFERER
    assert cabeceras[HEADER_APP_TITLE] == APP_TITLE
    # Y las cabeceras técnicas, porque un cliente sin `Content-Type` no envía JSON.
    assert cabeceras["Accept"] == "application/json"
    assert cabeceras["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_la_peticion_enviada_lleva_la_atribucion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La petición que sale de verdad lleva las dos cabeceras.

    Se comprueba sobre la petición registrada por un `MockTransport`, que es lo que
    realmente se enviaría. Es la comprobación que cierra el circuito: si alguien añadiera una
    ruta en el cliente que construyera sus propias cabeceras, esta prueba lo detectaría.
    """

    registrada: dict[str, Any] = {}

    def handler(peticion: httpx.Request) -> httpx.Response:
        registrada["headers"] = dict(peticion.headers)
        registrada["url"] = str(peticion.url)
        registrada["body"] = json.loads(peticion.content.decode("utf-8"))
        return httpx.Response(
            status_code=200,
            json={
                "model": "test/model",
                "choices": [
                    {"message": {"content": "respuesta"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 45},
            },
        )

    transporte = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transporte) as cliente:
        resultado = await llm_client.complete(
            model="test/model",
            prompt="hola",
            api_base="https://ejemplo.invalid/v1",
            api_key=SecretStr("llave-de-prueba"),
            client=cliente,
        )

    cabeceras = registrada["headers"]
    # `httpx` normaliza los nombres a minúsculas, que es como viajan en HTTP.
    assert cabeceras[HEADER_HTTP_REFERER.lower()] == HTTP_REFERER
    assert cabeceras[HEADER_APP_TITLE.lower()] == APP_TITLE
    assert cabeceras["authorization"] == "Bearer llave-de-prueba"

    assert resultado.prompt_tokens == 120
    assert resultado.completion_tokens == 45
    assert resultado.text == "respuesta"
    assert registrada["url"] == "https://ejemplo.invalid/v1/chat/completions"
    assert registrada["body"]["model"] == "test/model"


# --------------------------------------------------------------------------- #
# La atribución viaja también al contenedor
# --------------------------------------------------------------------------- #


def test_el_entorno_del_contenedor_lleva_la_misma_atribucion() -> None:
    """El contenedor recibe lo mismo que declara las cabeceras.

    El motor habla con el proveedor desde dentro y su tráfico no pasa por el backend. Si el
    entorno y las cabeceras divergieran, la atribución funcionaría a medias, y esa es la
    forma de fallo más difícil de detectar: el backend sí manda las cabeceras, así que una
    prueba del backend daría verde.
    """

    sandbox = _sandbox_de_prueba()
    entorno = sandbox.container_environment()

    assert entorno[ENV_HTTP_REFERER] == attribution_headers()[HEADER_HTTP_REFERER]
    assert entorno[ENV_APP_TITLE] == attribution_headers()[HEADER_APP_TITLE]
    assert entorno[ENV_HTTP_REFERER] == HTTP_REFERER
    assert entorno[ENV_APP_TITLE] == APP_TITLE


def test_el_entorno_hereda_las_variables_nuevas() -> None:
    """`attribution_environment()` y el entorno del contenedor no pueden separarse.

    Se compara el conjunto de variables de atribución del entorno contra el que produce la
    función. Si alguien añadiera una cabecera a la declaración y olvidara el entorno, esta
    prueba lo dice por diferencia de conjuntos, no por el valor de una.
    """

    sandbox = _sandbox_de_prueba()
    entorno = sandbox.container_environment()
    esperado = attribution_environment()

    for clave, valor in esperado.items():
        assert entorno[clave] == valor, f"{clave} no coincide entre entorno y declaración"

    # La función no declara nada que el entorno no reciba.
    for clave in esperado:
        assert clave in entorno


# --------------------------------------------------------------------------- #
# Normalización de la respuesta
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cuerpo", "motivo"),
    [
        ({"choices": []}, "sin elecciones"),
        ({"choices": [{}]}, "elección sin mensaje"),
        ({"choices": [{"message": {}}]}, "mensaje sin contenido"),
        ({"choices": "no es una lista"}, "eliciones de otro tipo"),
    ],
)
def test_una_respuesta_que_no_tiene_texto_es_un_error(cuerpo: dict[str, Any], motivo: str) -> None:
    """Una respuesta sin texto utilizable es un fallo, no un parche vacío.

    Un parche vacío se publicaría como una PR sin cambios, que es peor que un error: el
    cliente ve una Pull Request abierta y no entiende que no arregla nada.
    """

    with pytest.raises(llm_client.LlmUpstreamError):
        llm_client.parse_chat_response(json.dumps(cuerpo))


def test_una_respuesta_sin_usage_es_una_respuesta_sin_cobrar() -> None:
    """Sin `usage` los tokens valen cero, y `has_usage` es `False`.

    La distinción importa en la tarificación: cero significa "el proveedor no lo dijo" y no
    "el modelo no consumió nada". Con la primera, el llamador decide si cobra; con la
    segunda, se cobraría un escaneo que el proveedor quizá no ha ejecutado.
    """

    resultado = llm_client.parse_chat_response(
        json.dumps({"choices": [{"message": {"content": "texto"}}]})
    )

    assert resultado.prompt_tokens == 0
    assert resultado.completion_tokens == 0
    assert resultado.has_usage is False
    assert resultado.finish_reason is None


def test_el_texto_en_lista_se_aplana() -> None:
    """Algunos proveedores devuelven `content` como lista de bloques.

    Es el formato de las respuestas con razonamiento, y para autofix lo que importa es el
    texto concatenado. Sin aplanarlo, un proveedor de ese estilo devolvería siempre un error
    aunque la respuesta fuera correcta.
    """

    resultado = llm_client.parse_chat_response(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "parte uno "},
                                {"type": "reasoning", "text": "pensamiento"},
                                {"type": "text", "text": "parte dos"},
                            ]
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
    )

    assert resultado.text == "parte uno parte dos"


def test_un_cuerpo_corrupto_es_un_error_controlado() -> None:
    """Un cuerpo que no es JSON no revienta con una excepción de la librería.

    Se traduce a `LlmUpstreamError`, que es lo que la ruta sabe convertir en un código HTTP.
    Dejar que escape un `json.JSONDecodeError` produciría un `500` sin mensaje útil.
    """

    with pytest.raises(llm_client.LlmUpstreamError):
        llm_client.parse_chat_response("esto no es json")


def test_el_prompt_mas_grande_que_el_tope_se_rechaza_antes_de_la_red() -> None:
    """Un prompt desmedido falla local, sin gastar una petición.

    El contexto de un repositorio grande puede pasarse de largo, y la petición se aceptaría
    con un `400` del proveedor tras haber enviado varios cientos de kilobytes. Rechazarlo
    aquí es más barato y el mensaje dice cuál es el límite.
    """

    largo = "x" * (llm_client.DEFAULT_MAX_PROMPT_CHARS + 1)

    with pytest.raises(llm_client.LlmClientError, match="excede"):
        llm_client.build_chat_payload(model="test/model", prompt=largo)


@pytest.mark.parametrize(
    "base",
    [
        "https://ejemplo.invalid/v1/",
        "https://ejemplo.invalid/v1",
        "https://ejemplo.invalid/v1///",
    ],
)
def test_la_url_se_construye_sin_barra_duplicada(base: str) -> None:
    """La URL no lleva doble separador aunque la base termine en barra.

    `LLM_API_BASE` se declara con barra en algunos entornos y sin ella en otros, y el `404`
    que produce un doble separador depende del proxy: con uno funciona y con otro no, que es
    el peor tipo de fallo de configuración porque no se puede reproducir.

    Se prueban las tres formas porque quitar **una** barra no basta: un entorno puede venir
    con tres y la comprobacion ingenua seguiría produciendo un separador duplicado.
    """

    assert llm_client.build_chat_url(base) == "https://ejemplo.invalid/v1/chat/completions"


@pytest.mark.parametrize("base", ["", "   ", "/"])
def test_sin_url_base_no_hay_url(base: str) -> None:
    """Sin base utilizable el error dice que falta, en vez de construir una ruta relativa.

    Una peticion a `/chat/completions` sin host es un `InvalidURL` de la libreria, cuyo
    mensaje no nombra la variable de entorno que falta ni dice cuál de las dos es.

    Los tres casos vacios se prueban porque los tres son reales: una variable declarada pero
    vacia en el entorno, una que solo tiene espacios, y una que se dejo como `/`.
    """

    with pytest.raises(llm_client.LlmNotConfiguredError, match="LLM_API_BASE"):
        llm_client.build_chat_url(base)


@pytest.mark.asyncio
async def test_sin_clave_no_se_intenta_la_peticion() -> None:
    """Sin clave el fallo ocurre **antes** de abrir la conexion.

    Es la distincion entre "el proveedor me dijo que no" y "no tengo credencial". La primera
    tiene cuerpo de error y un codigo; la segunda es un fallo de configuracion local y por
    eso se comprueba que el transporte **nunca** llego a invocarse.
    """

    invocado = False

    def handler(peticion: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal invocado
        invocado = True
        return httpx.Response(status_code=200, json={})

    transporte = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transporte) as cliente:
        with pytest.raises(llm_client.LlmNotConfiguredError, match="LLM_API_KEY"):
            await llm_client.complete(
                model="test/model",
                prompt="hola",
                api_base="https://ejemplo.invalid/v1",
                api_key=SecretStr(""),
                client=cliente,
            )

    assert invocado is False, "la peticion no deberia haberse enviado"


def test_una_respuesta_sin_uso_produce_un_desglose_en_ceros() -> None:
    """Cero tokens llegan a la tarificacion como cero, y no como ausencia de dato.

    Es la comprobacion cruzada con `compute_charge`: un `0` debe producir un desglose con
    importe cero y no un `None`. Si devolviera `None`, la capa de tarificacion no podria
    distinguir "el proveedor no lo dijo" de "el modelo no consumio nada", que son dos
    decisiones de cobro distintas.
    """

    resultado = llm_client.parse_chat_response(
        json.dumps({"choices": [{"message": {"content": "texto"}}]})
    )
    assert resultado.has_usage is False

    desglose = compute_charge(
        base_cost_input_m=Decimal("3.00"),
        base_cost_output_m=Decimal("15.00"),
        markup_pct=Decimal("25"),
        prompt_tokens=resultado.prompt_tokens,
        completion_tokens=resultado.completion_tokens,
        credits_per_usd=Decimal("1.00"),
    )

    assert desglose is not None, "cero tokens siguen siendo un dato de consumo"
    assert desglose.base_cost_usd == Decimal("0")
    assert desglose.client_cost_credits == Decimal("0")
