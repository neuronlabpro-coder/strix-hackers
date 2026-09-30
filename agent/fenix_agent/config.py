"""Configuración del agente de escaneo.

## Por qué `configparser` y no variables de entorno ni un fichero JSON

Porque el formato `.ini` es el que tiene un gestor de secretos en todas las distribuciones, con
interfaz de línea de comandos, y este fichero va a contener un token. Un `.env` se copia por
casualidad en un ticket; un `/etc/fenix-agent/fenix-agent.ini` con `600` y `root` no.

Y no se admiten variables de entorno **por defecto** a propósito. Una variable de entorno la
hereda todo el árbol de procesos —incluido cualquier subproceso que alguien lance sin querer—, y
un token de agente no debería estar en la memoria de procesos que no son el agente.

## Por qué el token se valida al arrancar y no solo al usarlo

Porque el fallo que importa es «el agente no hace nada y no dice por qué», y la causa más
probable de ese fallo es un token mal copiado. Validarlo al arrancar lo convierte en un mensaje
inmediato en el log, en vez de un `401` cada quince segundos que alguien lee como ruido de red.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: El prefijo que emite la plataforma. Un token que no lo tiene no es de un agente, y la
#: plataforma lo rechaza con un `401` genérico; avisar aquí evita gastar la primera petición en
#: descubrirlo.
PREFIJO_TOKEN: Final[str] = "mgf_agent_"

#: Intervalo mínimo entre sondeos. Por debajo de cinco segundos un agente solo genera
#: peticiones: el servidor no encuentra trabajo antes de ese tiempo, así que el agente no está
#: trabajando, está haciendo ruido en la base de datos.
INTERVALO_MINIMO: Final[int] = 5
INTERVALO_MAXIMO: Final[int] = 3_600

_URL = re.compile(r"^https://[^\s/]+(?:/[^\s]*)?$")


class ConfigError(ValueError):
    """La configuración no sirve para arrancar, y el motivo dice cuál es el campo."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Todo lo que el agente necesita saber antes de hacer nada."""

    url: str
    token: str
    intervalo_sondeo: int
    connect_timeout: float
    read_timeout: float

    @property
    def base_url(self) -> str:
        """La URL sin barra final, que es lo que evitan las rutas dobles."""

        return self.url.rstrip("/")

    @property
    def host_de_plataforma(self) -> str:
        """El host de la plataforma, que es lo unico que se puede declarar alcanzable.

        ## Por que esto es una propiedad y no un campo mas de la configuracion

        Porque **no se configura**: se deduce de la URL que ya es obligatoria, y pedir al operador
        que escriba el host dos veces es pedirle que los escriba de forma distinta una vez. La
        propiedad es el unico sitio donde se decide que ese host es de fiar, y asi no puede
        haber un segundo sitio donde se escriba.

        Lo consume `red.declarar_entidades_permitidas` al arrancar, y solo con esto.
        """

        from urllib.parse import urlparse

        return urlparse(self.base_url).hostname or ""


def _exigir(seccion: configparser.SectionProxy, clave: str) -> str:
    valor = seccion.get(clave, "").strip()
    if not valor:
        raise ConfigError(
            f"falta `{clave}` en la seccion [plataforma] del fichero de configuracion. "
            "El token se copia de la respuesta al registrar el agente en el panel, y esa "
            "respuesta **solo se muestra una vez**: si se perdio, registra uno nuevo y da de "
            "baja el anterior."
        )
    return valor


def cargar_configuracion(ruta: str | Path) -> AgentConfig:
    """Lee el fichero de configuración y lo valida entero antes de devolver nada.

    ## Por qué se valida **todo** antes de arrancar

    Porque un fallo de configuración a medio camino deja el agente en un estado en el que hace
    cosas: se registra, se autentica, saca un trabajo y lo pierde al entregarlo. Validar la URL
    y el token antes de la primera petición hace que un error de tecleo se vea con el fichero
    delante y no con un `401` en el log tres horas después.
    """

    ruta = Path(ruta)
    if not ruta.is_file():
        raise ConfigError(
            f"no existe el fichero de configuracion {ruta}. En el README esta el bloque de "
            "INI minimo que hay que crear."
        )

    # `interpolation=None` desactiva el `%` de configparser, que si no interpretaria un `%` de
    # la URL como una referencia a otra clave y fallaria con un error que no menciona el
    # fichero.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ruta, encoding="utf-8")
    except configparser.Error as error:
        raise ConfigError(f"el fichero {ruta} no es un INI valido: {error}") from error

    if not parser.has_section("plataforma"):
        raise ConfigError(f"el fichero {ruta} no tiene la seccion [plataforma]")

    seccion = parser["plataforma"]

    url = _exigir(seccion, "url")
    if not _URL.match(url):
        raise ConfigError(
            f"`url` debe ser una URL https de la plataforma, como "
            "https://api.example.com. El valor "
            f"«{url}» no vale. **Sin** http: el token viaja en la cabecera de autorizacion y "
            "en claro por el cable."
        )

    token = _exigir(seccion, "token")
    if not token.startswith(PREFIJO_TOKEN):
        raise ConfigError(
            f"`token` no empieza por {PREFIJO_TOKEN!r}. Los tokens de la API de panel empiezan "
            f"por otra cosa, y un token de panel aqui no puede ni registrarse ni escanear: el "
            "agente necesita el suyo."
        )

    intervalo = seccion.getint("intervalo_sondeo", fallback=15)
    if not INTERVALO_MINIMO <= intervalo <= INTERVALO_MAXIMO:
        raise ConfigError(
            f"`intervalo_sondeo` esta en {intervalo} y tiene que estar entre "
            f"{INTERVALO_MINIMO} y {INTERVALO_MAXIMO} segundos. Por debajo del minimo solo se "
            "generan peticiones sin trabajo que hacer; por encima del maximo, un escaneo "
            "puede tardar en arrancar lo que el cliente espera."
        )

    return AgentConfig(
        url=url,
        token=token,
        intervalo_sondeo=intervalo,
        connect_timeout=seccion.getfloat("connect_timeout", fallback=10.0),
        read_timeout=seccion.getfloat("read_timeout", fallback=120.0),
    )
