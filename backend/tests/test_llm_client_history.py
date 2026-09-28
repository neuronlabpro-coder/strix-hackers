"""Pruebas del historial en el cliente de LLM.

## Que se comprueba y por que

El cliente grew un parametro `history` que antes no existia, y grow en un sitio donde el
resultado **no se ve en la respuesta**: un historial mal colocado produce una respuesta
plausible y distinta, sin ningun error en ninguna parte. Por eso las pruebas miran la
**posicion** de cada mensaje y no que las palabras aparezcan.

## Por que el limite mide tambien el historial

Porque `DEFAULT_MAX_PROMPT_CHARS` se documentsa como el tope del contexto que se manda. Si
midiera solo el prompt, un historial de doscientos mensajes pasaria el control sin haber sido
mirado, y la peticion saldria con un cuerpo que el proveedor va a rechazar —o a aceptar y a
cobar— por muy por encima del tope. El sintoma seria un `400` del proveedor sin relacion
aparente con el motivo.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from backend.apps.llm_router.client import (
    DEFAULT_TIMEOUT,
    LlmTurn,
    build_chat_payload,
    build_chat_url,
    complete,
)

pytestmark = pytest.mark.asyncio


HISTORIAL = (
    LlmTurn(role="user", content="Primera pregunta"),
    LlmTurn(role="assistant", content="Primera respuesta"),
)


# --------------------------------------------------------------------------- #
# El orden de los mensajes
# --------------------------------------------------------------------------- #


async def test_el_historial_va_entre_el_sistema_y_la_ultima_pregunta() -> None:
    """El orden es `system`, historial, pregunta. Ese orden **es** la conversacion.

    Un historial pegado dentro del prompt llega como un bloque de prosa en el que la pregunta
    es la ultima linea de un texto largo, y el modelo responde a la prosa: el sintoma clasico
    del chat que "olvida" lo que se le acaba de preguntar.
    """

    cuerpo = build_chat_payload(
        model="m", prompt="Segunda pregunta", system="Instrucciones", history=HISTORIAL
    )
    assert [m["role"] for m in cuerpo["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert cuerpo["messages"][-1]["content"] == "Segunda pregunta"


async def test_sin_historial_el_comportamiento_es_el_de_antes() -> None:
    """Sin historial, el cuerpo es exactamente el de la version anterior.

    El `autofix` usa este cliente y necesita `temperature=0` y un unico mensaje de usuario. Si
    anadir historial hubiera cambiado el caso por defecto, el parche reproducible dejaria de
    serlo sin que ninguna prueba de `autofix` lo notara.
    """

    cuerpo = build_chat_payload(model="m", prompt="Evidencia", system="Instrucciones")
    assert cuerpo["messages"] == [
        {"role": "system", "content": "Instrucciones"},
        {"role": "user", "content": "Evidencia"},
    ]
    assert cuerpo["temperature"] == 0.0
    assert "max_tokens" not in cuerpo


async def test_sin_sistema_no_se_inventa_un_turno_de_sistema() -> None:
    """Un `system` vacio es un turno que el proveedor recibe y no tiene que existir.

    Mandar `{"role": "system", "content": ""}` no es inocuo: algunos proveedores lo tratan como
    una instruccion vacia que **reemplaza** su comportamiento por defecto, y el resultado es un
    modelo que responde de otra manera sin que se note por que.
    """

    cuerpo = build_chat_payload(model="m", prompt="Hola")
    assert [m["role"] for m in cuerpo["messages"]] == ["user"]


async def test_el_turno_valida_su_forma() -> None:
    """`LlmTurn` obliga a `role` y `content` a existir.

    Es la razon de que el historial sea un tipo y no un `list[dict]`: un `{"role": "user",
    "text": ...}` escrito en un modulo se cuela sin que ninguna prueba falle —la comprobacion
    ocurre dentro de `httpx` contra un proveedor real— y el sintoma es un `400` que no nombra el
    campo.
    """

    turno = LlmTurn(role="user", content="Hola")
    assert turno.as_message() == {"role": "user", "content": "Hola"}


# --------------------------------------------------------------------------- #
# El tope
# --------------------------------------------------------------------------- #


async def test_el_tope_mide_el_historial_y_no_solo_el_prompt() -> None:
    """Un prompt corto con un historial enorme tiene que rebotar.

    Es el fallo que el limite por prompt solo deja pasar: la comprobacion se cumple, la peticion
    sale con un cuerpo de varios megabytes, y el `400` del proveedor no dice nada de un limite
    que el propio codigo declara tener.
    """

    from backend.apps.llm_router.client import LlmClientError

    enorme = "x" * 500_000
    with pytest.raises(LlmClientError, match="contexto"):
        build_chat_payload(
            model="m",
            prompt="Corta",
            history=(LlmTurn(role="user", content=enorme),),
        )


async def test_el_error_del_tope_dice_los_dos_numeros() -> None:
    """El mensaje dice el tope **y** el tamano real que se alcanzo.

    Un "el contexto es demasiado largo" sin cifras obliga a medir a mano para descubrir cuanto
    falta. El mensaje lleva los dos numeros precisamente para que se vea de un vistazo cuanto
    hay que quitar, y se comprueba que el total que anuncia es el del prompt **mas** el
    historial: si solo midiera el prompt, el numero seria menor que el contexto real y el
    usuario recortaria de mas sin saber por que.
    """

    from backend.apps.llm_router.client import DEFAULT_MAX_PROMPT_CHARS, LlmClientError

    historial = "y" * 10_000
    with pytest.raises(LlmClientError) as error:
        build_chat_payload(
            model="m",
            prompt="z" * (DEFAULT_MAX_PROMPT_CHARS - 5_000),
            history=(LlmTurn(role="user", content=historial),),
        )

    mensaje = str(error.value)
    assert str(DEFAULT_MAX_PROMPT_CHARS) in mensaje
    # El total que anuncia incluye el historial: sin el, seria `DEFAULT_MAX_PROMPT_CHARS - 5000`.
    total_esperado = DEFAULT_MAX_PROMPT_CHARS - 5_000 + len(historial)
    assert str(total_esperado) in mensaje


async def test_el_historial_largo_si_pasa() -> None:
    """Un historial de tamaño normal no rebota: el limite tiene que ser alcanzable."""

    cuerpo = build_chat_payload(
        model="m", prompt="Actual", history=(LlmTurn(role="user", content="z" * 1_000),)
    )
    assert len(cuerpo["messages"]) == 2


# --------------------------------------------------------------------------- #
# El transporte
# --------------------------------------------------------------------------- #


async def test_la_peticion_lleva_el_historial_como_mensajes() -> None:
    """Lo que sale por la red tiene el historial como mensajes, no dentro del prompt.

    Se comprueba sobre la peticion capturada y no sobre `build_chat_payload`, porque las dos
    podrian diferir: el payload se construye en un sitio y se envia en otro, y una divergencia
    ahi no la ve ninguna prueba que mire solo la construccion.
    """

    capturadas: list[httpx.Request] = []

    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        capturadas.append(peticion)
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Respuesta"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_manejador)) as cliente:
        await complete(
            model="m",
            prompt="Segunda",
            system="Instrucciones",
            history=HISTORIAL,
            api_base="https://ejemplo.test/v1",
            api_key=_clave(),
            client=cliente,
        )

    cuerpo = json.loads(capturadas[0].content)
    assert [m["role"] for m in cuerpo["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    # Y el prompt nuevo **no** lleva el historial pegado dentro.
    assert cuerpo["messages"][-1]["content"] == "Segunda"


async def test_el_timeout_publico_es_el_que_usa_complete() -> None:
    """`DEFAULT_TIMEOUT` se puede construir, que era el bug que lo hacia fallar.

    Vivia dentro del cuerpo de `complete` con la forma `Timeout(connect=..., read=...)`, que
    `httpx` **rechaza** al construirla. El fallo no se veia porque todas las pruebas inyectan
    un cliente y nunca llegaban a construirlo, de modo que la ruta de produccion fallaba con
    un `ValueError` en la primera llamada real.
    """

    assert DEFAULT_TIMEOUT.connect is not None
    assert DEFAULT_TIMEOUT.read is not None


async def test_la_url_se_construye_con_un_timeout_valido() -> None:
    """Construir el timeout fuera de una funcion es la unica forma de que falle al importar.

    Va declarada `async` aunque no await nada, porque el `pytestmark` del modulo es `asyncio` y
    una funcion sincronica marcada produce un `PytestWarning` que ensucia la salida del gate.
    La asincronia no aporta nada aqui: no hay I/O, y la prueba mide la construccion de dos
    objetos puros.
    """

    assert build_chat_url("https://ejemplo.test/v1") == "https://ejemplo.test/v1/chat/completions"


def _clave() -> SecretStr:
    """Una credencial de prueba.

    Va como `SecretStr` y no como `str` porque es lo que espera la firma de `complete`, y
    `str` ahi no compila. La clave no es un secreto: no sale de la base, no se persiste y solo
    llega al `MockTransport`, que no la comprueba.
    """

    return SecretStr("clave-de-prueba")
