"""Cliente HTTP del proveedor de LLM, para las llamadas que hace la **plataforma**.

## Por qué existe cuando el motor ya habla con el proveedor

Porque el motor corre en un contenedor y solo cubre el tráfico de los escaneos. Hay llamadas
que la plataforma tiene que hacer **en su propio proceso**, y son las que no existían antes
de este módulo:

- La generación del parche de *autofix*, que es una llamada con `use_case="AUTOFIX"` que no
  pasa por un contenedor porque no escanea nada: redacta un diff a partir de evidencia que ya
  está en la base.

Esas llamadas necesitan las mismas dos cosas que las del contenedor —la atribución pública y
el registro del consumo— y por eso viven aquí y no repartidas por los módulos que las usan.

## Por qué la atribución se aplica en un solo sitio

Porque una cabecera que se añade "cuando se acuerde" es una cabecera que un día falta. Aquí
está en `_build_request_headers`, que es el **único** sitio por el que pasa cualquier
petición a `/chat/completions`. Añadir otra ruta HTTP sin pasar por ahí es posible, y por eso
el módulo documenta la invariante y hay una prueba que la comprueba sobre la función, no
sobre el cliente entero.

## Por qué se usa `httpx` y no `requests`

Porque es asíncrono, tiene timeouts por fase separados —conectar y leer son problemas
distintos— y su `AsyncClient` se puede compartir y cerrar de forma ordenada. El motor usa la
biblioteca propia; la plataforma usa la suya, y las dos son-legítimas porque no comparten
proceso.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

import httpx
from pydantic import SecretStr

from backend.apps.llm_router.attribution import attribution_headers

logger = logging.getLogger(__name__)

#: Ruta de chat completions, relativa a `llm_api_base`.
CHAT_COMPLETIONS_PATH: Final[str] = "/chat/completions"

#: Identificador del protocolo de la aplicación hacia el proveedor. Lo usan algunos
#: proveedores para la atribución de la app y es inocuo para los que no.
CLIENT_IDENTIFIER: Final[str] = "mind-guard-fenix-platform/1"

#: Tope del cuerpo de la respuesta. Una respuesta de chat son unos pocos kilobytes; un
#: `megabytes` es holgado y un `10 MB` convertiría una fuga de memoria en un fallo de red
#: mucho más difícil de diagnosticar.
MAX_RESPONSE_BYTES: Final[int] = 8 * 1024 * 1024

#: Tope del prompt. Un contexto de código puede ser largo, pero pasado cierto tamaño el
#: modelo deja de ser útil y el gasto se dispara. El valor lo fija la configuración.
DEFAULT_MAX_PROMPT_CHARS: Final[int] = 400_000


class LlmClientError(RuntimeError):
    """Fallo controlado hablando con el proveedor de LLM.

    Es una excepción de **dominio**, no un `HTTPException`: la decisión de qué código
    devolver la toma la ruta que la captura.
    """


class LlmNotConfiguredError(LlmClientError):
    """Falta la credencial o la URL base del proveedor."""


class LlmUpstreamError(LlmClientError):
    """El proveedor respondió con un error, o con algo que no es una respuesta válida.

    Guarda el `status` porque el llamador lo necesita: un `429` merece reintento con otro
    modelo de la cadena y un `400` no, y una respuesta que no distingue los dos obliga a
    reintentar contra un error permanente hasta agotar la cadena.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    """Una respuesta de chat ya normalizada."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None

    @property
    def has_usage(self) -> bool:
        """Si el proveedor publicó el consumo.

        Cero tokens **si** es un dato valido, y por eso no se representa con `None` en la
        parte superior: la ausencia de `usage` entero es lo que significa "no lo sabemos".
        """

        return self.prompt_tokens > 0 or self.completion_tokens > 0


def _build_request_headers() -> Mapping[str, str]:
    """Las cabeceras de **toda** petición al proveedor. El único sitio donde se añaden.

    Se construye a partir de `attribution_headers()` y no con literales, para que el valor
    que viaja y el valor declarado no puedan separarse. La de autorización se añade después y
    lleva su propio esquema, porque es la única que depende de configuración.
    """

    headers: dict[str, str] = dict(attribution_headers())
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    headers["X-Client-Info"] = CLIENT_IDENTIFIER
    return MappingProxyType(headers)


def build_chat_url(api_base: str) -> str:
    """La URL completa de chat completions, a partir de la base del proveedor.

    Se quitan **todas** las barras finales, porque un entorno puede venir con tres y quitar
    solo una dejaría un separador duplicado. OpenRouter publica el endpoint en
    `https://openrouter.ai/api/v1` sin barra, y un doble separador devuelve `404` en
    algunos proxies y funciona en otros, que es la forma más difícil de diagnosticar.

    ## Por qué la base es un parámetro y no el global de configuración

    Porque `Settings` es **congelado**, y un objeto congelado no se puede sobreescribir en una
    prueba. Leyéndolo del global, comprobar este módulo exigía mutarlo con
    `object.__setattr__`, que es un truco que se cuela en el código de producción y acaba
    debilitando la garantía de inmutabilidad que lo motivó.

    Con la base como parámetro, la prueba pasa un valor y el código de producción pasa
    `settings.llm_api_base`. El cliente deja de depender del entorno, que es lo que lo
    convierte en comprobable sin trucos.
    """

    # Se quitan los espacios antes que las barras. Una variable de entorno cargada desde un
    # fichero `.env` mal indented llega como `"  "` o `"/  "`, y sin este `strip` pasarían el
    # `if not base` y producirían una URL que `httpx` rechaza con un error que no nombra la
    # variable. El error que dice qué falta es el que cuesta tiempo.
    base = api_base.strip().rstrip("/")
    if not base:
        raise LlmNotConfiguredError("LLM_API_BASE no está configurada")
    return f"{base}{CHAT_COMPLETIONS_PATH}"


def build_chat_payload(
    *,
    model: str,
    prompt: str,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """El cuerpo de una petición de chat.

    `temperature=0` por defecto porque casi todas las llamadas de la plataforma que usan esto
    son **autofix**, donde la salida tiene que ser un diff reproducible: la misma evidencia
    tiene que producir el mismo parche, o no hay forma de auditar por que cambio.
    """

    if len(prompt) > DEFAULT_MAX_PROMPT_CHARS:
        raise LlmClientError(
            f"El prompt excede {DEFAULT_MAX_PROMPT_CHARS} caracteres "
            f"({len(prompt)}); el contexto es demasiado grande para el modelo"
        )

    messages: list[dict[str, str]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        # `temperature` a 0 y `top_p` a 1 son la misma idea desde dos sitios, y algunos
        # proveedores rechazan que los dos jrreen. Se manda solo uno.
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _extract_text(data: Mapping[str, Any]) -> str:
    """El texto de la primera elección, o un error si la respuesta no lo trae.

    Se toma la **primera** eleccion y no la ultima, y no se recorren todas buscando una que
    tenga contenido: una respuesta con varias elecciones es un bug del llamador, y
    quedarse con la que "parece" la buena lo esconde en vez deQwuarlo.
    """

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmUpstreamError("La respuesta del proveedor no trae elecciones", status=None)

    first = choices[0]
    if not isinstance(first, Mapping):
        raise LlmUpstreamError("La primera elección no es un objeto", status=None)

    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LlmUpstreamError("La elección no trae un mensaje", status=None)

    content = message.get("content")
    if isinstance(content, str):
        return content

    # Algunos proveedores con salida de razonamiento devuelven `content` como lista de
    # bloques mezclando el texto de la respuesta con el razonamiento interno. **Solo** se
    # concatenan los bloques de tipo `text`.
    #
    # Es el punto donde un bug sería silencioso y caro: el razonamiento del modelo es prosa
    # sobre el código, y pegarla al principio del diff produce un parche que no aplica y una
    # Pull Request que parece un arreglo. No da ningún error en ningún sitio: sale un texto
    # más largo y con otro aspecto.
    #
    # Un bloque sin `type` se acepta como texto, porque hay proveedores que lo omiten y
    # filtrar por `type == "text"` les tiraría la respuesta entera.
    if isinstance(content, list):
        partes: list[str] = []
        for bloque in cast(list[object], content):
            if not isinstance(bloque, Mapping):
                continue
            tipo = bloque.get("type")
            if tipo is not None and tipo != "text":
                continue
            texto = bloque.get("text")
            if isinstance(texto, str):
                partes.append(texto)
        return "".join(partes)

    raise LlmUpstreamError("El mensaje no trae texto utilizable", status=None)


def _extract_usage(data: Mapping[str, Any]) -> tuple[int, int]:
    """Tokens de entrada y salida, o `(0, 0)` si el proveedor no los publica."""

    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        int(prompt) if isinstance(prompt, (int, float)) and not isinstance(prompt, bool) else 0,
        int(completion)
        if isinstance(completion, (int, float)) and not isinstance(completion, bool)
        else 0,
    )


def parse_chat_response(raw: str) -> LlmCompletion:
    """Normaliza el cuerpo de una respuesta de chat.

    Separado del transporte a propósito: es la parte que hay que probar con capturas reales de
    proveedores —respuesta correcta, sin `usage`, `content` en lista, cuerpo corrupto— y
    probarla sin red.
    """

    try:
        payload: object = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise LlmUpstreamError("El cuerpo de la respuesta no es JSON válido") from error
    if not isinstance(payload, Mapping):
        raise LlmUpstreamError("La respuesta del proveedor no es un objeto JSON")

    data = payload
    choices = data.get("choices")
    finish_reason: str | None = None
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        raw_finish = choices[0].get("finish_reason")
        finish_reason = raw_finish if isinstance(raw_finish, str) else None

    model = data.get("model")
    prompt_tokens, completion_tokens = _extract_usage(data)

    return LlmCompletion(
        text=_extract_text(data),
        model=model if isinstance(model, str) else "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
    )


async def complete(
    *,
    model: str,
    prompt: str,
    api_base: str,
    api_key: SecretStr,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> LlmCompletion:
    """Envía una petición de chat y devuelve la respuesta normalizada.

    `api_base` y `api_key` son explícitos y no se leen del global. Ver la nota de
    `build_chat_url`: es lo que hace este módulo comprobable sin mutar un `Settings`
    congelado.

    `client` es inyectable para que las pruebas usen `MockTransport` y no una red. Cuando es
    `None` se abre y se cierra una conexión por llamada, que es lo correcto para un servicio
    que hace pocas llamadas y no quiere mantener un pool para nada.
    """

    if not api_key.get_secret_value():
        raise LlmNotConfiguredError("LLM_API_KEY no está configurada")

    url = build_chat_url(api_base)
    payload = build_chat_payload(
        model=model, prompt=prompt, system=system, temperature=temperature, max_tokens=max_tokens
    )
    headers = dict(_build_request_headers())
    headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0))
    try:
        response = await http.post(url, headers=headers, json=payload)
    except httpx.HTTPError as error:
        raise LlmUpstreamError(f"Fallo de red hablando con el proveedor: {error}") from error
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code >= 400:
        # El cuerpo del error se registra, no se propaga al usuario: puede contener el
        # identificador de la petición del proveedor, que sirve para soporte y no para el
        # cliente final.
        logger.warning(
            "El proveedor respondió %s: %s",
            response.status_code,
            response.text[:500],
        )
        raise LlmUpstreamError(
            f"El proveedor respondió {response.status_code}", status=response.status_code
        )

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise LlmUpstreamError("La respuesta del proveedor excede el tamaño permitido")

    return parse_chat_response(response.text)


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "CLIENT_IDENTIFIER",
    "MAX_RESPONSE_BYTES",
    "LlmClientError",
    "LlmCompletion",
    "LlmNotConfiguredError",
    "LlmUpstreamError",
    "attribution_headers",
    "build_chat_payload",
    "build_chat_url",
    "complete",
    "parse_chat_response",
]
