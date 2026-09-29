"""La clave del proveedor no entra en el contenedor sin que alguien lo haya decidido.

## Que hay que decidir, y por que el codigo no puede decidirlo

La clave **tiene** que entrar en el contenedor: Strix habla con el proveedor de inferencia por su
cuenta, desde dentro del contenedor, y ese trafico no pasa por el backend. Sin la clave no hay
escaneo.

El problema es que el contenedor tambien lleva shell, Python, Playwright, caido y `jwt_tool`, y
que su entrada no es solo el operador: es **el objetivo que se esta escaneando**. Un README
malicioso, una pagina que el agente visita, un `postinstall` que imprime el entorno, y el agente
tiene ordenes de cumplir. Lo que hay al lado es la credencial de la **plataforma**, que da
acceso al gasto de todos los clientes.

Las tres salidas reales —proxy de inferencia en el worker, credencial con tope por escaneo
emitida contra la API de aprovisionamiento, proxy de salida allowlistado por host— son
decisiones de despliegue con coste, y ninguna se toma desde el codigo. Lo que si se hace aqui es
que la exposicion sea un **hecho declarado**: en produccion, sin la frase de reconocimiento, el
runner no arranca.

## Por que una frase exacta y no un interruptor booleano

Porque un `true` se pone sin que nadie lea lo que se esta activando, y porque dentro de tres
meses nadie recuerda si lo que estaba activado era "lo se" o "lo se porque si no no escaneaba".
Exigir el valor exacto convierte el interruptor en un acta.

## Por que el valor por defecto es **exigir**

Porque el valor por defecto decide que pasa en un despliegue en el que nadie se ha acordado de
nada. Si por defecto fuese no exigir, la proteccion estaria desactivada en cualquier
instalacion que no la activara, y no habria ningun sintoma. Ese es el modo de fallo que un
interruptor de seguridad no puede tener, y es el mismo criterio que usa
`test_runner_egress_fence.py` para el cerco de salida.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import backend.workers.runner.llm_key_exposure as modulo
from backend.core.config import settings
from backend.workers.runner.llm_key_exposure import (
    FRASE_DE_RECONOCIMIENTO,
    SALIDAS,
    LlmKeyExposureNotAcknowledgedError,
    exigir_reconocimiento_de_exposicion,
)


def _con(**cambios: object):
    """Una copia de los ajustes **del modulo**, con los campos cambiados encima.

    ## Por que parte del modulo y no del `settings` importado

    Porque las pruebas de este fichero se **apilan**: un `autouse` pone el interruptor a
    exigir, y cada prueba pone encima lo suyo. Si `_con` partiese del `settings` del entorno,
    cada prueba **borraria** lo que puso el `autouse` y volveria a leer el `.env` de la
    maquina —que en desarrollo tiene el interruptor apagado—, con lo que las pruebas que esperan
    un rechazo no lo obtendrian.

    Es la diferencia entre componer y sustituir, y aqui hay que componer: el fixture
    establece la base y la prueba solo cambia lo que le importa.

    Se **copia** en vez de mutar porque `Settings` es congelado —la garantia de la que ya se
    sirve `llm_router.client`—, y mutarlo exigiria `object.__setattr__`, que es justo el truco
    que la inmutabilidad existe para evitar.
    """

    return modulo.settings.model_copy(update=cambios)


@pytest.fixture(autouse=True)
def exigir_siempre(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fuerza el interruptor a exigir en todas las pruebas de este fichero.

    ## Por que hace falta

    Porque sin el, estas pruebas leen el `.env` de la maquina. Y en desarrollo ese fichero
    tiene `STRIX_REQUIRE_LLM_KEY_EXPOSURE_ACK=false` —que es lo correcto para poder lanzar
    escaneos en local—, con lo que `exigir_reconocimiento_de_exposicion()` no levanta y las
    pruebas que esperan un rechazo pasan sin comprobar nada.

    Pasaron cuando se ejecutaron en solitario, antes de tocar el `.env`, y fallaron en la
    bateria completa. Esa diferencia es exactamente la que distingue una prueba de su entorno de
    una prueba de su codigo, y por eso el interruptor se fija aqui y no se lee de ningun sitio.
    """
    monkeypatch.setattr(
        modulo, "settings", _con(strix_require_llm_key_exposure_ack=True)
    )


@pytest.fixture
def con_reconocimiento(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Deja el modulo con la frase puesta y el interruptor exigiendo."""

    monkeypatch.setattr(
        modulo, "settings", _con(strix_llm_key_exposure_ack=FRASE_DE_RECONOCIMIENTO)
    )
    yield


# --------------------------------------------------------------------------- #
# Sin reconocimiento, no se entrega la clave
# --------------------------------------------------------------------------- #


def test_sin_reconocimiento_no_se_entrega_la_clave() -> None:
    with pytest.raises(LlmKeyExposureNotAcknowledgedError):
        exigir_reconocimiento_de_exposicion()


@pytest.mark.parametrize(
    "valor",
    [
        "true",
        "1",
        "si",
        "aceptado",
        "SI",
        " " + FRASE_DE_RECONOCIMIENTO + " ",
        "si-entiendo-el-riesgo",
    ],
)
def test_cualquier_valor_que_no_sea_la_frase_exacta_falla(
    valor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un booleano se pone sin leer. La frase es lo que obliga a leerla.

    La comparacion es **exacta**: `"true"`, `"si"`, la misma frase en otro caso y la misma frase
    con espacios alrededor fallan igual que una cadena vacia. Una comparacion permisiva con
    `.strip()` y `.lower()` seria un interruptor con disfraz de frase, que es peor que un
    interruptor porque parece un acta.
    """

    monkeypatch.setattr(
        modulo, "settings", _con(strix_llm_key_exposure_ack=valor)
    )
    with pytest.raises(LlmKeyExposureNotAcknowledgedError):
        exigir_reconocimiento_de_exposicion()


# --------------------------------------------------------------------------- #
# Con la frase exacta, se entrega
# --------------------------------------------------------------------------- #


def test_con_la_frase_exacta_se_entrega_la_clave(con_reconocimiento: None) -> None:
    clave = exigir_reconocimiento_de_exposicion()
    assert clave == settings.llm_api_key.get_secret_value()
    assert clave, "la clave entregada no puede estar vacia"


def test_la_clave_entregada_es_la_misma_que_la_de_la_configuracion(
    con_reconocimiento: None,
) -> None:
    """La funcion que decide es la que entrega: no puede haber dos valores.

    Si una parte leyera la clave de la configuracion y otra la recibiera del modulo, un dia
    dejarian de coincidir y el contenedor receberia una credencial distinta de la que se
    comprobo.
    """

    assert exigir_reconocimiento_de_exposicion() == settings.llm_api_key.get_secret_value()


# --------------------------------------------------------------------------- #
# El interruptor
# --------------------------------------------------------------------------- #


def test_con_el_interruptor_apagado_pasa_sin_reconocer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """En desarrollo no hay que escribir la frase para probar un escaneo."""

    monkeypatch.setattr(
        modulo,
        "settings",
        _con(
            strix_require_llm_key_exposure_ack=False,
            strix_llm_key_exposure_ack="",
        ),
    )
    clave = exigir_reconocimiento_de_exposicion()
    assert clave == settings.llm_api_key.get_secret_value()


def test_el_valor_por_defecto_del_interruptor_es_exigir() -> None:
    """Si por defecto no se exigiera, un despliegue nuevo no tendria proteccion."""

    campo = settings.model_fields["strix_require_llm_key_exposure_ack"]
    assert campo.default is True, (
        "STRIX_REQUIRE_LLM_KEY_EXPOSURE_ACK deberia exigir por defecto; si por defecto no se "
        "exige, la credencial de la plataforma entra en la caja de herramientas de un atacante "
        "en cualquier instalacion en la que nadie se haya acordado de nada"
    )


def test_el_reconocimiento_viene_vacio_por_defecto() -> None:
    """Si el valor por defecto de la frase fuera la frase, exigir no exigiria nada."""

    campo = settings.model_fields["strix_llm_key_exposure_ack"]
    assert (campo.default or "") == "", (
        "STRIX_LLM_KEY_EXPOSURE_ACK no debe venir con la frase puesta de serie: el "
        "reconocimiento tiene que ser de quien despliega, no del repositorio"
    )


# --------------------------------------------------------------------------- #
# El mensaje de error
# --------------------------------------------------------------------------- #


def test_el_error_dice_que_hacer_y_que_riesgo() -> None:
    with pytest.raises(LlmKeyExposureNotAcknowledgedError) as error:
        exigir_reconocimiento_de_exposicion()

    mensaje = str(error.value)

    # Nombra la variable y el valor exacto: sin eso, quien lee el error tiene que buscar el
    # nombre del ajuste en el codigo para poder seguir.
    assert "STRIX_LLM_KEY_EXPOSURE_ACK" in mensaje
    assert FRASE_DE_RECONOCIMIENTO in mensaje

    # Explica el riesgo, no solo que falta una variable. Un error que dice "pon la variable"
    # hace que se ponga y no que se entienda por que.
    assert "plataforma" in mensaje
    assert "escanea" in mensaje or "objetivo" in mensaje

    # Y ofrece las salidas reales, para no obligar a quien recibe el error a proponer una.
    for salida in SALIDAS:
        nombre = salida.split(":")[0]
        assert nombre in mensaje, f"el mensaje no menciona la salida {nombre!r}"


# --------------------------------------------------------------------------- #
# Las salidas
# --------------------------------------------------------------------------- #


def test_las_salidas_son_tres_y_distintas() -> None:
    """Que sean tres no es un detalle: son las unicas tres salidas que hay.

    Se comprueba que cada una se nombra **y** explica en que consiste, con dos puntos. Una lista
    de salidas con separadores distintos no se puede parsear, y un mensaje de error que se arma
    con una lista mal formada enseña menos de lo que el autor creia.
    """

    assert len(SALIDAS) == 3
    assert len(set(SALIDAS)) == 3
    for salida in SALIDAS:
        assert ":" in salida, (
            f"la salida {salida!r} no explica en que consiste: le falta el separador"
        )
        nombre, explicacion = salida.split(":", 1)
        assert nombre.strip() and explicacion.strip(), (
            f"la salida {salida!r} tiene un lado vacio"
        )


def test_la_frase_de_reconocimiento_no_es_un_bandera() -> None:
    """Una frase de cuatro palabras se pondria de memoria sin leer nada."""

    palabras = FRASE_DE_RECONOCIMIENTO.split("-")
    assert len(palabras) >= 6, (
        "la frase de reconocimiento es demasiado corta para que nadie tenga que pensar en si "
        "la sabe o no: ponerla tiene que costar un segundo"
    )
    assert "aceptado" in FRASE_DE_RECONOCIMIENTO
