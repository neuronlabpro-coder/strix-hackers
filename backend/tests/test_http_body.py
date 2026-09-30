"""Pruebas de la lectura acotada del cuerpo de un webhook.

## Qué va a fallar si alguien deshace esto

El módulo `core/http_body.py` existe por un defecto concreto que ya estuvo en el árbol: el
webhook de Stripe hacía `body = await request.body()` y **después** miraba `len(body)`. Esa
comprobación da la sensación de acotar y no acota, porque el cuerpo ya está copiado entero en
memoria cuando se ejecuta. Quien no tiene la firma —que es el caso de esa ruta, porque la
firma se verifica después de leer— gana la asignación antes de que el tope se aplique.

La batería cubre los cinco caminos por los que se puede intentar colar un cuerpo grande:

1. **`Content-Length` honesto y enorme.** El atajo: se rechaza sin leer.
2. **`Content-Length` que miente.** Declara diez bytes y manda un megas. Solo lo detecta el
   conteo real durante la lectura.
3. **`Content-Length` ausente o corrupto.** `Transfer-Encoding: chunked` lo lleva, y es el
   camino por defecto de quien no quiere que le miren.
4. **Cuerpo repartido en trozos que cruzan el tope.** El `body()` de antes no miraba nada
   hasta el final; aquí cada trozo se compara al llegar.
5. **Un límite no positivo.** Falla al llamado, no al final del bucle y con un `413` que no dice
   que el problema era de configuración.

## Por qué estos tests no usan ni HTTP ni base de datos

Porque la función recibe un `Request` y solo usa tres cosas de él: `headers`, `url` y
`stream()`. Una doble de tres líneas ejercita lo mismo que un `TestClient` real y no obliga a
levantar la aplicación, que es lo que cuesta tiempo aquí. El tipo `Any` del doble es
`SimpleNamespace`: no se quiere que el doble imponga un contrato que la función real no
tiene, solo cumplir el que esta declara.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.core.http_body import (
    content_length_declarado,
    leer_cuerpo_acotado,
)

#: Tope de los tests. Small y redondo para que los números de la batería se lean de un vistazo.
TOPE = 64 * 1024


class PeticionDePrueba:
    """Lo mínimo que `leer_cuerpo_acotado` toca de un `Request`.

    ## Por qué no `TestClient`

    Porque para leer un cuerpo no hace falta un servidor: hacen falta `headers`, `url` y un
    `stream()`. Montar la aplicación entera para comprobar un `len()` que cruza un tope
    sería un test que tarda más de lo que explica, y el fallo —si lo hubiera— se vería en el
    `stream()`, que es justo lo que la doble sustituye.
    """

    def __init__(self, trozos: list[bytes], cabeceras: dict[str, str] | None = None) -> None:
        self._trozos = trozos
        self.headers = cabeceras or {}
        self.url = SimpleNamespace(path="/api/v1/billing/webhooks")

    async def stream(self) -> AsyncIterator[bytes]:
        """Entrega los trozos uno a uno, como hace Starlette.

        Se usa un generador asíncrono de verdad y no una lista devuelta de golpe porque el
        defecto que se está cubriendo **es** el troceado: una implementación que acumulara
        primero y midiera después pasaría un test con una lista y fallaría en producción.
        """

        for trozo in self._trozos:
            yield trozo


# --------------------------------------------------------------------------- #
# El atajo: `Content-Length` declarado
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rechaza_sin_leer_cuando_el_content_length_declara_demas() -> None:
    """El caso bueno del atajo: no se lee ni un byte del cuerpo.

    Y se comprueba **que no se leyó**, no solo que salió un `413`: un `413` después de haber
    leído el cuerpo entero seguiría siendo el defecto original con otro mensaje. Por eso la
    doble cuenta los trozos que le piden.
    """

    leidos: list[bytes] = []

    class PeticionContada(PeticionDePrueba):
        async def stream(self) -> AsyncIterator[bytes]:
            for trozo in self._trozos:
                leidos.append(trozo)
                yield trozo

    peticion = PeticionContada([b"x" * 8], {"content-length": str(TOPE * 10)})

    with pytest.raises(HTTPException) as error:
        await leer_cuerpo_acotado(peticion, TOPE)

    assert error.value.status_code == 413
    assert leidos == [], "el atajo tiene que rechazar antes de tocar el stream"


# --------------------------------------------------------------------------- #
# `Content-Length` que no dice la verdad
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rechaza_cuando_el_content_length_miente_por_debajo() -> None:
    """Declara diez bytes y manda un cuerpo mayor que el tope.

    Este es el caso que un filtro basado solo en la cabecera no ve. Y es el más cómodo de
    usar: no hace falta saber nada del servidor, solo poner un número pequeño.
    """

    peticion = PeticionDePrueba([b"x" * (TOPE * 2)], {"content-length": "10"})

    with pytest.raises(HTTPException) as error:
        await leer_cuerpo_acotado(peticion, TOPE)

    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_rechaza_cuando_el_content_length_falta() -> None:
    """Sin `Content-Length` —que es lo que hace `Transfer-Encoding: chunked`— el conteo real
    es la única defensa, y por eso el stream se lee troceo a troceo."""

    peticion = PeticionDePrueba([b"x" * (TOPE // 2)] * 4)

    with pytest.raises(HTTPException) as error:
        await leer_cuerpo_acotado(peticion, TOPE)

    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_rechaza_cuando_el_content_length_no_es_un_entero() -> None:
    """`Content-Length: banana` no es un cuerpo vacío, es una cabecera que no dice la verdad.

    Se trata igual que la ausente: no se fía y lee con el tope. Un `0` aquí haría que la ruta
    creyera que no hay cuerpo y devolviera un `400` de «falta la firma» en vez de un `413` que
    explica lo que pasa.
    """

    peticion = PeticionDePrueba([b"x" * (TOPE + 1)], {"content-length": "banana"})

    with pytest.raises(HTTPException) as error:
        await leer_cuerpo_acotado(peticion, TOPE)

    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_deja_de_leer_en_cuanto_el_tope_se_cruza() -> None:
    """La mitad que importa: el `413` sale **antes** de consumir el cuerpo entero.

    ## Por qué este test existe y los otros cinco no bastan

    Porque los otros cinco comprueban que sale un `413`, y un `413` sigue saliendo aunque el
    corte durante la lectura desaparezca: basta con acumularlo entero y mirarlo al final. Eso
    es exactamente el defecto que el módulo arregla —materializar el cuerpo para después
    decir que era grande— reintroducido por la puerta de atrás, y los cinco tests lovementsan
    en verde.

    Se vio al reintroducir los defectos uno a uno: con el corte desactivado, `16 passed`.

    ## Qué mide

    Cuántos trozos del stream se pidieron antes de fallar. El stream de este test lleva
    muchos más trozos de los que el tope necesita, así que la diferencia entre «se paró a
    tiempo» y «lo leyó todo y luego lo miró» es de varios trozos, no de uno: no es un detalle
    de redondeo que un ajuste de constants pueda tapar.
    """

    leidos: list[bytes] = []

    class PeticionContada(PeticionDePrueba):
        async def stream(self) -> AsyncIterator[bytes]:
            for trozo in self._trozos:
                leidos.append(trozo)
                yield trozo

    # Diez trozos de medio tope: si se leyera todo, serían diez. Con el corte en su sitio,
    # el quinto ya cruza la línea.
    peticion = PeticionContada([b"x" * (TOPE // 2)] * 10)

    with pytest.raises(HTTPException) as error:
        await leer_cuerpo_acotado(peticion, TOPE)

    assert error.value.status_code == 413
    assert len(leidos) <= 6, (
        f"se leyeron {len(leidos)} de 10 trozos: el cuerpo se acumuló entero antes de comprobar "
        "el tope, que es el defecto que este módulo existe para evitar"
    )


# --------------------------------------------------------------------------- #
# Cuerpos que caben
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_acepta_un_cuerpo_pequeno_con_y_sin_content_length() -> None:
    """Los dos caminos de un cuerpo legítimo tienen que devolver lo mismo.

    Se comprueba el contenido, no solo que no lance: una función que devolviera `b""` siempre
    pasaría un `assert not error`, y el webhook de Stripe la rechazaría con un `400` de firma
    inválida que no es lo que pasó.
    """

    cuerpo = b'{"id": "evt_1", "type": "checkout.session.completed"}'

    for cabeceras in ({}, {"content-length": str(len(cuerpo))}):
        peticion = PeticionDePrueba([cuerpo[:10], cuerpo[10:]], cabeceras)
        assert await leer_cuerpo_acotado(peticion, TOPE) == cuerpo


@pytest.mark.asyncio
async def test_acepta_un_cuerpo_exactamente_en_el_limite() -> None:
    """El límite es un tope, no un exclusive.

    Un cuerpo de justo `TOPE` bytes es legítimo y tiene que pasar. El margen interno de un
    chunk es lo que evita que esto dependa de cómo el servidor partió la red, que es la
    razón de que exista: sin él, un cuerpo válido se rechazaría o no según la suerte del
    troceado.
    """

    cuerpo = b"x" * TOPE
    peticion = PeticionDePrueba([cuerpo[:10], cuerpo[10:]], {"content-length": str(TOPE)})

    assert len(await leer_cuerpo_acotado(peticion, TOPE)) == TOPE


# --------------------------------------------------------------------------- #
# La configuración del límite
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("tope", [0, -1])
async def test_falla_antes_de_leer_si_el_limite_no_es_positivo(tope: int) -> None:
    """Un tope no positivo es un error de configuración, y tiene que decirlo.

    Sin este caso, `tope=0` haría que **todo** fuera `413` —con el cuerpo entero en memoria—
    y el diagnóstico sería «el webhook es demasiado grande», que apunta al sitio equivocado.
    """

    peticion = PeticionDePrueba([b"x"])

    with pytest.raises(ValueError, match="positivo"):
        await leer_cuerpo_acotado(peticion, tope)


# --------------------------------------------------------------------------- #
# La cabecera, por separado
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cabecera", "esperado"),
    [
        ("262144", 262_144),
        ("0", 0),
        (" 42 ", 42),
        ("banana", None),
        ("-5", None),
        ("12.5", None),
        ("", None),
    ],
)
def test_content_length_declarado_solo_acepta_enteros_no_negativos(
    cabecera: str, esperado: int | None
) -> None:
    """La cabecera se interpreta o se descarta; nunca se inventa un valor.

    El `0` sí es un valor legítimo y se conserva: un cuerpo vacío de verdad tiene que poder
    llegar a la ruta y que sea ella la que diga que no hay firma. Lo que no puede pasar es
    que una cabecera ilegible se convierta en `0`, porque eso confunde «vacío» con «no sé».
    """

    peticion = PeticionDePrueba([], {"content-length": cabecera})
    assert content_length_declarado(peticion) == esperado


def test_content_length_declarado_es_none_sin_cabecera() -> None:
    """La ausencia de la cabecera no es un error: es el caso normal de `chunked`."""

    assert content_length_declarado(PeticionDePrueba([])) is None
