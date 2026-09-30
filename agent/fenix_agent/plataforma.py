"""Cliente de la plataforma:|latido, pedir trabajo, entregarlo.

## Por qué el agente no reintenta a lo bruto y sí reintenta con espera

Porque un `401` de este token **no se arregla reintentando**: o el token está mal copiado, o el
agente se dio de baja, y en los dos casos lo que se necesita es que alguien lo mire. Reintentar
un `401` veinte veces solo produce veinte lineas de log idénticas y una entrada de auditoría que
dice que el agente estuvo vivo cuando en realidad no podía hacer nada.

Un `5xx` o un error de conexión sí se reintenta, con espera creciente, porque esos sí son
transitorios: el proceso se queda esperando y vuelve a intentarlo cuando la plataforma vuelva.

## Por qué el token va en la cabecera y no en la URL

Porque una URL con un token dentro acaba en los logs de acceso del servidor, en el historial del
proxy y en el `Referer` si algún día hay una redirección. En la cabecera `Authorization` no sale
en ninguno de esos sitios, y ese es el motivo por el que el estandar existe.

## Por qué eso no era suficiente, y por qué ahora hay un filtro de destino

Porque el razonamiento de arriba hablaba de la `Referer` de una redirección y se olvidaba de algo
peor: **`urllib` reenvía la cabecera `Authorization` al seguir una redirección**. En CPython,
`HTTPRedirectHandler.redirect_request` reconstruye la petición copiando todas las cabeceras
salvo `content-length` y `content-type`. Con `urlopen` global —que trae el
`HTTPRedirectHandler` activo por defecto— cualquier `3xx` en la ruta del panel entregaba el token
del agente a quien lo enviara: un open redirect, un takeover del subdominio de la API, un proxy
mal configurado.

La cabecera estaba en el sitio correcto por un motivo que **no era** el del riesgo, y eso es lo
que lo hacía invisible en la revisión.

La respuesta no es mover el token ni quitar la cabecera: es que **no se sigue ninguna
redirección**. Va en `red.abrir`, y el mismo filtro cubre el caso de que la plataforma no
resuelva el destino a una red interna. El token sigue en la cabecera porque es donde debe
estar, y ahora la petición solo sale hacia el host que este agente ha comprobado.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from types import TracebackType
from typing import Any

from fenix_agent import red
from fenix_agent.config import AgentConfig

logger = logging.getLogger(__name__)

#: Tamaño máximo de una respuesta. La plataforma **acota** los resultados que devuelve, así que
#: un cuerpo mayor que esto significa que alguien cambió ese recorte o que el destino no es la
#: plataforma. Ante la duda se corta: leer un cuerpo enorme en memoria en la red del cliente es
#: exactamente lo que este agente no debe poder hacer.
MAX_CUERPO: int = 32 * 1024 * 1024


class ErrorDePlataforma(RuntimeError):
    """La plataforma respondió algo que no se puede usar."""


class ErrorDeAutenticacion(ErrorDePlataforma):
    """El token no sirve. Reintentar no lo arregla."""


class ErrorTransitorio(ErrorDePlataforma):
    """Un fallo que probablemente se resuelva solo en el siguiente intento."""


class ClientePlataforma:
    """Lo único que el agente sabe hacer con la plataforma."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._url = config.base_url

    # ----------------------------------------------------------------- #
    # Transporte
    # ----------------------------------------------------------------- #

    def _peticion(
        self, metodo: str, ruta: str, cuerpo: dict[str, Any] | None = None
    ) -> Any:
        """Una llamada, con el token en la cabecera y el cuerpo acotado.

        El tiempo de espera de lectura es más alto que el de conexión a propósito: un
        `claim` tiene que llegar a la base y puede tardar si hay otros agentes esperando, y
        cortar esa llamada por poco se parecería a un fallo de red.
        """

        destino = f"{self._url}{ruta}"
        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        peticion = urllib.request.Request(destino, data=datos, method=metodo)
        peticion.add_header("Authorization", f"Bearer {self._config.token}")
        peticion.add_header("Accept", "application/json")
        if datos is not None:
            peticion.add_header("Content-Type", "application/json")

        try:
            with red.abrir(
                destino,
                {clave: valor for clave, valor in peticion.header_items()},
                self._config.read_timeout,
            ) as respuesta:
                cuerpo_leido = red.leer_acotado(respuesta)
                if len(cuerpo_leido) > MAX_CUERPO:
                    raise ErrorDePlataforma(
                        f"la respuesta de {ruta} supera el maximo de {MAX_CUERPO} bytes. "
                        "La plataforma acota los resultados de un escaneo, asi que esto apunta "
                        "a que el destino no es la plataforma."
                    )
        except urllib.error.HTTPError as error:
            # Se lee el cuerpo del error antes de decidir, y **no** se incluye en el mensaje:
            # un `detail` de FastAPI puede contener algo del interior. Al log va el código y
            # el motivo de la plataforma, que es texto que la plataforma eligió para ser leido.
            detalle = ""
            try:
                crudo = error.read(8_192).decode("utf-8", "replace")
                analysed = json.loads(crudo)
                if isinstance(analysed, dict):
                    detalle = str(analysed.get("detail", ""))[:300]
            except (json.JSONDecodeError, OSError, ValueError):
                detalle = ""
            if error.code in (401, 403):
                causa = detalle or "el token no es valido o el agente esta dado de baja"
                raise ErrorDeAutenticacion(
                    f"HTTP {error.code}: {causa}. Reintentar no lo arregla: revisa el token en "
                    "la seccion [plataforma] del fichero de configuracion."
                ) from error
            raise ErrorTransitorio(f"HTTP {error.code}: {detalle}") from error
        except urllib.error.URLError as error:
            raise ErrorTransitorio(
                f"no se pudo contactar con {destino}: {error.reason}. Esto si es transitorio: "
                "se reintenta en el siguiente ciclo."
            ) from error
        except red.DestinoNoPermitido as error:
            # No es transitorio y **no** se reintenta: un destino que este agente no puede abrir
            # no se va a abrir en el siguiente ciclo, y reintentarlo convierte un fallo de
            # configuracion en un bucle que consume la cola.
            raise ErrorDePlataforma(
                f"este agente no abre {destino}: {error}. Si la plataforma esta en una red "
                "privada, hay que declararla en [red] del fichero de configuracion."
            ) from error
        except TimeoutError as error:
            raise ErrorTransitorio(
                f"la peticion a {destino} paso de {self._config.read_timeout} s"
            ) from error

        if not cuerpo_leido.strip():
            return None
        try:
            return json.loads(cuerpo_leido.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ErrorDePlataforma(
                f"la respuesta de {ruta} no es JSON. Si la url apunta a un portal en vez de a "
                "la API, esto es lo que se ve."
            ) from error

    # ----------------------------------------------------------------- #
    # Operaciones
    # ----------------------------------------------------------------- #

    def latido(self, version: str, plataforma: str) -> None:
        """Dice que sigue vivo. Los fallos se ignoran a proposito.

        Un latido que falla no es un problema del agente: puede que la plataforma esté caída y el
        resto de llamadas fallaran igual. Propagar el fallo haria que el bucle principal tratara
        un problema informativo como si fuera operativo, y que un corte de red pareciera un
        fallo del escaneo.
        """

        try:
            self._peticion(
                "POST",
                "/api/v1/agents/heartbeat",
                {"agent_version": version, "platform_hint": plataforma},
            )
        except ErrorDeAutenticacion:
            # Este si se propaga: un token invalido deja al agente sin hacer nada, y seguir
            # sondeando es gastar peticiones para no conseguir trabajo.
            raise
        except ErrorDePlataforma as error:
            logger.debug("latido no entregado: %s", error)

    def pedir_trabajo(self) -> dict[str, Any] | None:
        """Pide el siguiente trabajo, o `None` si no hay ninguno.

        `None` **no** es un fallo: es la respuesta normal la mayor parte del tiempo, y por eso el
        cliente devuelve un cuerpo vacío que aqui se traduce a `None` en vez de a una excepción.
        """

        respuesta = self._peticion("POST", "/api/v1/agents/jobs/claim")
        if respuesta is None:
            return None
        if not isinstance(respuesta, dict):
            raise ErrorDePlataforma("la respuesta de `claim` no es un objeto")
        return respuesta

    def empezar_trabajo(self, job_id: str) -> None:
        """Marca que el trabajo empieza de verdad, no que se lo llevaron."""

        self._peticion("POST", f"/api/v1/agents/jobs/{job_id}/start", {})

    def entregar_trabajo(
        self, job_id: str, resultado: dict[str, Any] | None, error: str | None
    ) -> None:
        """Cierra el trabajo. Un fallo al entregar **no** se pierde: se reintenta.

        ## Por qué este reintento tiene su propio límite y no el del bucle

        Porque un `5xx` al entregar significa que el resultado **está a punto de perderse**, y lo
        que se está a punto de perder es lo que el cliente pagó. Un reintento con espera
        creciente y un tope es la diferencia entre "se recuperó" y "se perdió el escaneo entero".

        Y si aun así no se entrega, se deja constancia en el log con el identificador del
        trabajo, que es lo único que permite a alguien recuperar el resultado si el agente tiene
        el resultado en disco.
        """

        cuerpo: dict[str, Any] = {"failed": error is not None}
        if error is not None:
            cuerpo["error_message"] = error
        if resultado is not None:
            cuerpo["result"] = resultado

        espera = 2.0
        for intento in range(1, 7):
            try:
                self._peticion("POST", f"/api/v1/agents/jobs/{job_id}/report", cuerpo)
                return
            except ErrorDeAutenticacion:
                raise
            except ErrorDePlataforma as error:
                if intento == 6:
                    logger.error(
                        "no se pudo entregar el trabajo %s tras %d intentos: %s. El resultado "
                        "se ha perdido; si el agente lo guardo en disco, se puede entregar a "
                        "mano con `fenix-agent` --reenviar.",
                        job_id,
                        intento,
                        error,
                    )
                    return
                logger.warning(
                    "entrega del trabajo %s fallida (intento %d/%d): %s",
                    job_id,
                    intento,
                    6,
                    error,
                )
                time.sleep(espera)
                espera *= 2


def esperar_segundos(segundos: float) -> None:
    """Dormir, pero despertar de verdad ante un `Ctrl+C`.

    No es un detalle: sin esto, un `time.sleep` de quince segundos hace que el proceso tarde
    quince segundos en responder a la señal de parada, y en un despliegue de un contenedor eso
    acaba en un `SIGKILL` del orquestador y en un log que no dice por qué.
    """

    try:
        time.sleep(segundos)
    except KeyboardInterrupt:
        raise


#: Reexportado para que el `__init__` no tenga que importar de dos sitios.
_ = (TracebackType, logger)
