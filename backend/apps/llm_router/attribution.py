"""Atribución pública de la aplicación ante el proveedor de LLM.

## Qué es esto y por qué existe

OpenRouter muestra en su catálogo público qué aplicaciones Envían tráfico a cada modelo, y
para hacerlo exige dos cabeceras en **todas** las peticiones: `HTTP-Referer` y `X-Title`. Sin
ellas la petición se procesa pero no aparece en el ranking, y con el ranking aparece la
mitad del valor de estar en OpenRouter.

La declaración vive aquí, en un solo sitio, y es **inmutable**: son valores que describen la
identidad del producto, no configuración. No se pueden cambiar por entorno ni por token de
API, porque en cuanto son configurables dejan de ser la atribución correcta y se convierten
en el sitio donde alguien pone su propia URL por error.

## Por qué no basta con poner las cabeceras en el cliente HTTP

Porque no es la plataforma la única que habla con el proveedor. El motor de escaneo corre en
un contenedor efímero y hace sus propias llamadas a través de su cliente LLM, y esas no
pasan por el backend. Por eso la atribución viaja además en el **entorno del contenedor**,
ver `backend/workers/runner/sandbox.py`: las dos rutas de tráfico llevan la misma identidad.

## Por qué las claves van en un `MappingProxyType`

Para que el diccionario sea de solo lectura. Un `dict` normal que se devuelve desde una
función se puede mutar desde fuera, y una mutación sería la forma más discreta de quitar la
atribución: nadie la vería en el código, que sigue diciendo la verdad, y las peticiones
dejarían de llevar la cabecera. `MappingProxyType` convierte ese fallo en un `TypeError` en
el primer uso.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

#: Cabecera que OpenRouter usa para identificar el sitio de la aplicación. Es una URL, y
#: tiene que ser pública: es lo que el usuario ve al hacer clic en el ranking.
HTTP_REFERER: Final[str] = "https://mindguard.tech"

#: Nombre de la aplicación tal como se muestra en el ranking público.
APP_TITLE: Final[str] = "Mind Guard Fenix"

#: Cabecera de la URL de la aplicación.
HEADER_HTTP_REFERER: Final[str] = "HTTP-Referer"

#: Cabecera del nombre de la aplicación.
HEADER_APP_TITLE: Final[str] = "X-Title"

#: Las cabeceras de atribución, listas para inyectar en cualquier petición al proveedor.
#:
#: Es `MappingProxyType` y no `dict` por la razón de la cabecera del módulo: un diccionario
#: mutado desde fuera haría desaparecer la atribución sin que nadie lo notara.
ATTRIBUTION_HEADERS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        HEADER_HTTP_REFERER: HTTP_REFERER,
        HEADER_APP_TITLE: APP_TITLE,
    }
)

#: Variables de entorno con las que la atribución viaja al contenedor del motor.
#:
#: El motor usa `litellm` y lee su configuración del entorno. Estas dos variables son la
#: forma de que la identidad llegue también a las peticiones que el contenedor hace por su
#: cuenta, que son la mayoría del tráfico.
ENV_HTTP_REFERER: Final[str] = "OPENROUTER_HTTP_REFERER"

ENV_APP_TITLE: Final[str] = "OPENROUTER_X_TITLE"


def attribution_headers() -> dict[str, str]:
    """Las cabeceras de atribución, como diccionario nuevo y mutable.

    Devuelve una **copia** a propósito. Quien la recibe la usa para construir su petición y
    casi siempre necesita añadir más cabeceras —`Authorization`, `Content-Type`—, y un
    `MappingProxyType` compartido se lo impediría con un `TypeError` en el camino caliente.
    La inmutabilidad que importa es la de la declaración: que nadie pueda alterarla, no que
    sea imposible copiarla.
    """

    return dict(ATTRIBUTION_HEADERS)


def attribution_environment() -> dict[str, str]:
    """Las variables de entorno que llevan la atribución al contenedor del motor.

    Se construye a partir de `ATTRIBUTION_HEADERS` y no de constantes sueltas, para que no
    pueda pasar que el entorno diga una cosa y las cabeceras otra. Si se olvidara una al
    añadirla, la atribución funcionaría en el backend y no en el motor, que es el peor de
    los dos fallos: parece que funciona.
    """

    return {
        ENV_HTTP_REFERER: ATTRIBUTION_HEADERS[HEADER_HTTP_REFERER],
        ENV_APP_TITLE: ATTRIBUTION_HEADERS[HEADER_APP_TITLE],
    }


__all__ = [
    "APP_TITLE",
    "ATTRIBUTION_HEADERS",
    "ENV_APP_TITLE",
    "ENV_HTTP_REFERER",
    "HEADER_APP_TITLE",
    "HEADER_HTTP_REFERER",
    "HTTP_REFERER",
    "attribution_environment",
    "attribution_headers",
]
