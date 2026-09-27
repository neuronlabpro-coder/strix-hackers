"""Transporte JSON-RPC 2.0 del servidor MCP.

## Por qué el protocolo vive aparte de las herramientas

Porque son dos cosas que cambian por motivos distintos. El sobre —`jsonrpc`, `id`, `method`,
`params` y los códigos de error— lo define una especificación y no cambia porque el producto
cambie. Las herramientas las pide el roadmap. Meter las dos en el router haría que añadir una
herramienta significara tocar el código que decide si `id` es un entero o un `string`, que es
donde un error no se ve: la respuesta sale con `200` y el cliente la descarta por un detalle de
forma.

## Por qué la respuesta es siempre `200` y el error va en el cuerpo

Es lo que exige JSON-RPC 2.0, y no es una manía de la especificación sino una decisión que
evita un problema real: un agente que llama a una herramienta recibe un error de transporte si
el servidor responde `4xx`, y lo trata como "la herramienta falló" sin mirar el cuerpo. Con un
`200` y un `error` dentro, el agente lee el código y puede decidir —reintentar, informar al
usuario, pedir otro token—.

La **excepción** es la autenticación, y también es deliberada: un `401` y un `403` los
comprende cualquier cliente HTTP antes de leer el JSON, y encerrarlos en un `200` obligaría a
un cliente mal configurado a no poder distinguir "tu token no vale" de "la herramienta no
existe", que es el peor de los dos diagnósticos. La autenticación se responde fuera del
sobre; los errores de protocolo, dentro.

## Por qué `id` se conserva tal cual en vez de normalizarlo

Porque la especificación dice que el `id` de la respuesta es el de la petición, sin cambios.
Normalizarlo a `string` o a `int` parece inocuo y rompe la correlación en cuanto hay dos
peticiones en vuelo con ids de tipos distintos, que es exactamente lo que hace un agente que
envía varias llamadas en paralelo. Y un `id` ausente es una **notificación**: no lleva
respuesta, y por eso el manejador devuelve `None`.

## Por qué se admiten lotes

Porque un lote es parte del protocolo y los clientes lo usan para reducir idas y vueltas.
Rechazarlo con un `-32600` sería inventar una restricción, y el cliente que lo usara fallaría
con un error que no apunta a la causa real: pensaría que su versión del protocolo está mal.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Versión del protocolo. Es un literal, no una opción: es lo que identifica el sobre.
JSONRPC_VERSION: Final[str] = "2.0"


class JsonRpcErrorCode(IntEnum):
    """Códigos de error de JSON-RPC 2.0, más dos propios de la plataforma.

    Los cinco primeros son los que la especificación reserva. Los dos siguientes están en el
    rango que deja al servidor —de `-32099` a `-32000`— precisamente para que una
    implementación declare sus propios fallos. No se reutiliza un código de la norma para
    algo que la norma no describe, porque un cliente que reconozca `-32602` sabe qué hacer con
    él: corregir los parámetros.
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    #: Fallo de una herramienta que sí existe y se ha ejecutado. Distinto de
    #: `INTERNAL_ERROR`: el sobre era válido y la herramienta corrió, así que quien lo recibe
    #: puede reintentar o pedir los datos de otra forma.
    TOOL_ERROR = -32000
    #: Falta un scope. Tiene código propio y no comparte con el `403` de HTTP, porque es la
    #: respuesta que un agente necesita distinguir de las demás para **no** reintentar:
    #: reintentar sin el scope da el mismo error, y un bucle de reintentos contra un permiso
    #: denegado es denegación de servicio contra la propia plataforma.
    INSUFFICIENT_SCOPE = -32001


@dataclass(frozen=True, slots=True)
class JsonRpcError(Exception):
    """Un error de JSON-RPC listo para serializar.

    Hereda de `Exception` a propósito: el despachador lanza para abortar, y un tipo que no
    fuera excepción obligaría a devolver un `Result` que cada herramienta tendría que
    envolver. El coste es que hay que capturarlo **antes** que el `except Exception` que
    traduce lo no controlado.
    """

    code: JsonRpcErrorCode
    message: str
    data: Any | None = None

    def as_dict(self) -> dict[str, Any]:
        cuerpo: dict[str, Any] = {"code": int(self.code), "message": self.message}
        if self.data is not None:
            cuerpo["data"] = self.data
        return cuerpo


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """Una petición ya validada.

    ## Por qué `method` es `str` y no un enum

    Porque un método desconocido **no** es un error de validación: es un `-32601` de la
    especificación, y se responde con el nombre que el cliente envió para que pueda ver
    exactamente qué no se reconoció. Aceptar el `str` y validar el método en la fase
    siguiente es lo que permite distinguir "sobre mal formado" de "método que no existe", que
    son errores distintos con causas distintas.
    """

    id: str | int | None
    method: str
    params: dict[str, Any] | None
    #: `True` en una notificación: sin `id`, sin respuesta. La especificación lo llama
    #: notificación y es una petición que no espera contestación, no un caso especial.
    is_notification: bool


#: Firma del despachador.
#:
#: Acepta un valor **o** una corrutina a la espera, y el manejador resuelve cuál con
#: `inspect.isawaitable`. La razón es práctica: la mitad de las pruebas del protocolo usan un
#: doble síncrono de dos líneas, y obligarlas a ser `async` solo para poder decir "devuelve
#: una cadena" haría que el protocolo —que no tiene nada de asíncrono— se probara peor.
#:
#: Lo que no se admite es **olvidar** el `await`. Un despachador asíncrono que se llama sin
#: esperar devuelve una corrutina, y una corrutina como `result` pasa a la respuesta sin
#: ejecutar la herramienta: el cliente recibe un `200` con un sobre imposible de serializar y
#: el efecto de la llamada —un escaneo encolado, un crédito cobrado— ocurre o no según el
#:apsedo. Por eso se espera explícitamente en lugar de fiarse del valor devuelto.
Dispatch = Callable[[JsonRpcRequest], "Any | Awaitable[Any]"]


def _es_id_valido(valor: Any) -> bool:
    """Si el `id` es del tipo que la especificación admite.

    Se acepta `int` y `str`, y se rechaza `float`, `bool`, `null`, listas y objetos. Es
    deliberadamente estricto: un `id` que no se puede devolver idéntico rompe la correlación
    de forma silenciosa —el cliente recibe un `id` distinto del que mandó y busca una
    respuesta que no existe—. Rechazarlo en la validación da un `-32600` que sí se puede
    corregir.
    """

    if isinstance(valor, bool):
        # `bool` es subclase de `int` en Python, así que sin esto un `true` pasaría como `1`
        # y volvería como `1`. El cliente seguiría correlacionando por el valor, pero el
        # sobre ya no es lo que el cliente mandó.
        return False
    return isinstance(valor, (int, str))


def parse_request(payload: Any) -> JsonRpcRequest | JsonRpcError:
    """Valida un sobre y devuelve la petición o el error.

    ## Por qué no admite lotes aquí

    Porque un lote es una **lista** de sobres, no un sobre. Si esta función aceptara una
    lista, un `params` que fuera una lista pasaría sin que nadie se quejara, que es justo el
    tipo de ambigüedad que hace que un servidor JSON-RPC se comporte de forma distinta según
    qué mande el cliente. El lote se detecta en el nivel de encima, que es donde está la
    regla.
    """

    if not isinstance(payload, dict):
        return JsonRpcError(
            JsonRpcErrorCode.INVALID_REQUEST,
            "Invalid Request",
            "El sobre JSON-RPC debe ser un objeto",
        )

    if payload.get("jsonrpc") != JSONRPC_VERSION:
        return JsonRpcError(
            JsonRpcErrorCode.INVALID_REQUEST,
            "Invalid Request",
            f"jsonrpc debe ser {JSONRPC_VERSION!r}",
        )

    method = payload.get("method")
    if not isinstance(method, str) or method == "":
        return JsonRpcError(
            JsonRpcErrorCode.INVALID_REQUEST,
            "Invalid Request",
            "method debe ser una cadena no vacía",
        )

    tiene_id = "id" in payload
    id_ = payload.get("id")
    if tiene_id and id_ is not None and not _es_id_valido(id_):
        return JsonRpcError(
            JsonRpcErrorCode.INVALID_REQUEST,
            "Invalid Request",
            "id debe ser una cadena, un entero o null",
        )

    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        # La especificación admite un array posicional. **No** se admite aquí, y es una
        # decisión: la forma posicional obliga a que el orden de los argumentos sea parte del
        # contrato, y un cliente que reordena el array obtiene una llamada válida con
        # argumentos cambiados. Con objeto, añadir un parámetro es compatible con los
        # clientes que no lo envían, que es lo que hace falta cuando el catálogo de
        # herramientas evoluciona.
        return JsonRpcError(
            JsonRpcErrorCode.INVALID_PARAMS,
            "Invalid params",
            "params debe ser un objeto con los parámetros por nombre",
        )

    return JsonRpcRequest(
        id=id_,
        method=method,
        params=params,
        is_notification=not tiene_id,
    )


def success_response(id_: str | int | None, result: Any) -> dict[str, Any]:
    """El sobre de éxito.

    `result` va siempre, incluso con `null`. Omitirlo rompería a un cliente que distingue
    "sin resultado" de "resultado nulo", y la especificación exige la clave.
    """

    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "result": result}


def error_response(id_: str | int | None, error: JsonRpcError) -> dict[str, Any]:
    """El sobre de error."""

    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "error": error.as_dict()}


def _id_del_sobre(payload: Any) -> str | int | None:
    """El `id` de un sobre todavía sin validar, para poder responder a un error de validación.

    Se devuelve tal cual **solo si** es del tipo válido. Con un `id` que no se puede devolver
    idéntico, la respuesta lleva `null`, que es lo que la especificación manda cuando el
    sobre es ilegible: es preferible un `null` a un `id` corrupto que el cliente correlaciona
    con la petición equivocada.
    """

    if not isinstance(payload, dict):
        return None
    id_ = payload.get("id")
    return id_ if id_ is None or _es_id_valido(id_) else None


async def handle_payload(payload: Any, dispatch: Dispatch) -> Any:
    """Atiende un sobre o un lote de sobres.

    ## Por qué un fallo dentro de una herramienta no tumba el lote

    Porque la especificación lo pide y porque el comportamiento alternativo es peor: un agente
    que lanza cuatro llamadas en un lote y una falla —porque un repositorio ya no existe—
    recibiría un error de transporte y **no sabría cuáles de las cuatro se ejecutaron**. Al
    devolver el error dentro del elemento correspondiente, las otras tres llegan con su
    resultado y el cliente puede volver a pedir solo la que falló.

    ## Por qué un sobre mal formado aborta todo el lote

    La especificación lo deja como decisión de la implementación, y aquí se elige abortar. Un
    lote con un sobre mal formado indica un cliente roto, no una llamada rara: seguir
    ejecutando las demás gastaría cuota de escaneo de un cliente que va a fallar en todo lo que
    mande a partir de ahí. Es la lectura conservadora, y para un agente que reintenta
    automáticamente es la que menos daño hace.
    """

    if isinstance(payload, list):
        if not payload:
            return error_response(
                None,
                JsonRpcError(
                    JsonRpcErrorCode.INVALID_REQUEST,
                    "Invalid Request",
                    "Un lote no puede estar vacío",
                ),
            )
        respuestas: list[dict[str, Any]] = []
        for elemento in payload:
            if isinstance(elemento, list):
                return error_response(
                    None,
                    JsonRpcError(
                        JsonRpcErrorCode.INVALID_REQUEST,
                        "Invalid Request",
                        "Los lotes anidados no están admitidos",
                    ),
                )
            respuesta = await _handle_one(elemento, dispatch)
            if respuesta is not None:
                respuestas.append(respuesta)
        # Un lote compuesto **solo** de notificaciones no genera respuesta: es lo que dice
        # la especificación, y es lo que permite que un cliente mande un lote de
        # notificaciones y no reciba un `[]` que interpretaría como respuesta vacía.
        return respuestas or None

    return await _handle_one(payload, dispatch)


async def _handle_one(payload: Any, dispatch: Dispatch) -> dict[str, Any] | None:
    parseado = parse_request(payload)
    if isinstance(parseado, JsonRpcError):
        return error_response(_id_del_sobre(payload), parseado)

    try:
        resultado = dispatch(parseado)
        if inspect.isawaitable(resultado):
            resultado = await resultado
    except JsonRpcError as error:
        # Una notificación que falla no produce respuesta, pero sí se registra: si no, un
        # cliente que manda notificaciones y ninguna aparece en el log parece un servidor
        # que funciona.
        if parseado.is_notification:
            logger.warning(
                "Notificacion %s fallida: %s (%s)",
                parseado.method,
                error.message,
                int(error.code),
            )
            return None
        return error_response(parseado.id, error)
    except Exception:
        # Cualquier otra excepción es un fallo del servidor. Se registra **con la traza** y
        # se devuelve un error genérico: incluir el detalle en la respuesta HTTP le revelaría
        # al cliente nombres de tablas y estructura interna del proyecto.
        logger.exception("Fallo no controlado en el método MCP %s", parseado.method)
        if parseado.is_notification:
            return None
        return error_response(
            parseado.id,
            JsonRpcError(
                JsonRpcErrorCode.INTERNAL_ERROR,
                "Internal error",
                "La llamada falló por un error interno del servidor",
            ),
        )

    if parseado.is_notification:
        return None
    return success_response(parseado.id, resultado)


__all__ = [
    "JSONRPC_VERSION",
    "Dispatch",
    "JsonRpcError",
    "JsonRpcErrorCode",
    "JsonRpcRequest",
    "error_response",
    "handle_payload",
    "parse_request",
    "success_response",
]
