"""Lectura acotada del cuerpo de una petición, con un único límite para todos.

## Por qué este módulo existe

Porque había **dos** formas de leer el cuerpo de un webhook entrante y solo una estaba acotada:

| Ruta | Cómo lo leía | Tope |
| :--- | :--- | :--- |
| `webhooks/git/{provider}` | `async for chunk in request.stream()` | `GIT_WEBHOOK_MAX_BODY_BYTES` |
| `billing/webhooks` | `body = await request.body()` | después, sobre el resultado |

La segunda es la que falla. `await request.body()` **materializa el cuerpo entero en memoria**
antes de que exista la posibilidad de mirarlo: la comprobación de tamaño de la línea siguiente
solo puede decir que el cuerpo era grande cuando ya está entero, y para entonces el trabajo de
asignar gigabytes ya está hecho. Un atacante que no tiene la firma —que es justamente el caso de
esta ruta, porque la firma es lo que se comprueba después— no necesita que la comprobación
pase: le basta con enviar.

## Por qué el límite no se comprueba solo con `Content-Length`

Porque `Content-Length` es una cabecera, y una cabecera la escribe quien envía. Un cliente que
la omita —con `Transfer-Encoding: chunked`—, o que mienta, pasa el filtro de una línea y llega
al `body()` sin tope. El `Content-Length` se mira **además**, no en lugar de: es el atajo que
rechaza sin leer, y el conteo real es el que decide.

## Por qué no un middleware

Porque un middleware tendría que decidir qué rutas tienen cuerpo y cuáles no, y esa lista
crece con cada endpoint nuevo: es el sitio donde un límite se olvida sin que nada falle. Aquí
el límite se pide **explícitamente** al llamar a la función, y una ruta nueva que lea un cuerpo
tiene que decidir su número en la misma línea que la lee.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final, Protocol

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

#: Troceado de la lectura. 64 KiB es el tamaño de chunk que usa `uvicorn` por defecto, así que
#: un cuerpo de 256 KiB se lee en cinco vueltas y uno de 2 MB en treinta y dos: suficiente para
#: que el bucle no domine el coste, y lo bastante corto para que un `413` llegue pronto.
_CHUNK_LECTURA: Final[int] = 64 * 1024

class CuerpoDePeticion(Protocol):
    """Lo único que la lectura necesita de una petición.

    ## Por qué un `Protocol` y no el `Request` de Starlette

    Porque `Request` no es una interfaz, es una clase con veinte atributos de los que aquí se
    usan dos. Declarar el contrato como lo que se usa tiene tres ventajas concretas:

    1. La prueba puede pasar un doble de tres líneas sin un `cast` que apagaría el comprobador
       justo en el punto donde importa.
    2. La firma dice qué necesita la función. Un `Request` completo sugiere que puede leer
       cabeceras que no lee, o ficheros, o la sesión del usuario.
    3. Un `Request` real lo satisface sin cambiar nada: es estructural.

    ## Por qué `headers` es un `Mapping` y no un `dict`

    Porque `Request.headers` es un `Headers` de Starlette, que se comporta como un mapping
    inmutable y no es un `dict`. Declarar `dict` obligaría a convertirlo en cada llamada, y la
    conversión sería el trabajo que esta función existe para no hacer.
    """

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def url(self) -> Any: ...

    def stream(self) -> AsyncIterator[bytes]: ...


def content_length_declarado(request: CuerpoDePeticion) -> int | None:
    """El `Content-Length` declarado, o `None` si falta o no es un entero.

    ## Por qué devuelve `None` en vez de `0` ante una cabecera basura

    Porque `Content-Length: banana` no significa "cuerpo vacío": significa que quien envía no
    sabe o no dice la verdad, y las dos cosas se tratan igual, que es **no fiarse** y leer con
    el tope. Un `0` haría que la ruta creyera que el cuerpo está vacío y devolviera un `400` de
    "falta la firma" en vez de un `413` que dice lo que pasa.
    """

    bruto = request.headers.get("content-length")
    if bruto is None:
        return None
    try:
        declarado = int(bruto)
    except ValueError:
        logger.warning("Content-Length no es un entero: %r", bruto[:32])
        return None
    return declarado if declarado >= 0 else None


async def leer_cuerpo_acotado(
    request: CuerpoDePeticion, maximo: int
) -> bytes:
    """Lee el cuerpo entero **o** falla con `413`, sin llegar a materializarlo de más.

    ## Por qué `413` y no `400`

    Porque no es un cuerpo malformado: es un cuerpo que no cabe. Stripe distingue los dos casos
    y solo reintenta los transitorios, así que la elección importa: un `4xx` cualquiera detiene
    los reintentos, y `413` es el que dice la verdad sin inducir a un reintento.

    ## Por qué `maximo` va en el argumento y no en la configuración

    Porque el límite pertenece a **la ruta**, no a la plataforma. El webhook de Git y el de
    Stripe tienen tolerancias distintas por razones distintas —Git manda payloads con diffs,
    Stripe manda eventos de Checkout—, y un valor global obligaría a ajustar los dos a la vez.
    La configuración decide *qué* número es el de cada ruta; esta función decide *cómo* se
    respeta.
    """

    if maximo <= 0:
        raise ValueError(f"el límite del cuerpo tiene que ser positivo, llegó {maximo}")

    declarado = content_length_declarado(request)
    if declarado is not None and declarado > maximo:
        # El atajo: se rechaza sin leer un solo byte, que es lo que hace esto distinto de
        # `await request.body()` seguido de un `if len(body) > ...`.
        logger.warning(
            "Cuerpo de %d bytes declarado, por encima del límite de %d: %s",
            declarado,
            maximo,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="El cuerpo del webhook supera el tamaño permitido",
        )

    cuerpo = bytearray()
    async for chunk in request.stream():
        # El corte es en el límite exacto, sin margen.
        #
        # ## Por qué no hay margen, y por qué antes lo había
        #
        # Porque un margen de un chunk hacía que la lectura **siguiera** más allá del límite
        # antes de rendirse, que es exactamente lo contrario de lo que este módulo promete. La
        # idea era que un cuerpo válido no se rechazara por la forma en que el servidor partió
        # los trozos; la realidad es que el corte en el límite exacto ya garantiza eso, porque
        # `len(cuerpo) + len(chunk)` es el tamaño que tendría el cuerpo **con** ese trozo, y un
        # cuerpo que cabe no lo cruza nunca.
        if len(cuerpo) + len(chunk) > maximo:
            logger.warning(
                "Cuerpo por encima del límite de %d al leerlo en %s: %d bytes leidos",
                maximo,
                request.url.path,
                len(cuerpo) + len(chunk),
            )
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="El cuerpo del webhook supera el tamaño permitido",
            )
        cuerpo.extend(chunk)

    if len(cuerpo) > maximo:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="El cuerpo del webhook supera el tamaño permitido",
        )
    return bytes(cuerpo)
