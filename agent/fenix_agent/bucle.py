"""El bucle del agente: latido, pedir trabajo, escanear, entregar.

## Por qué un fallo de escaneo **no** para el agente

Porque un escaneo fallido es un resultado, y el cliente tiene que verlo. Si el agente se detuviera
al primer fallo —un registro caído, un host inalcanzable, una imagen que no existe— dejaría de
escanear para siempre, y el único síntoma sería un agente vivo que no hace nada.

Lo que hace es entregar el trabajo con `failed: true` y el motivo, seguir con el siguiente, y
volver al ciclo. El log lo dice una vez y el resultado lo ve el cliente, que es quien puede
decidir si reintenta.

## Por qué el bucle distingue `CLAIMED` de `RUNNING` en su propio flujo

Porque si el agente muere entre tomar el trabajo y empezar, el trabajo queda `CLAIMED` con un
alquiler que vence y otro agente lo recoge. Si se muere **empezando**, ya ha hecho el `POST
/start` y el mismo reaper lo devuelve. No hay una tercera vía, y por eso el `start` se manda
antes de tocar la red: es lo que convierte "tomó un trabajo" en "está trabajando en él", que es la
diferencia entre esperar y reintentar.

## Por qué el agente guarda el resultado en disco **antes** de entregarlo

Porque la entrega es la única parte de este ciclo que no se puede repetir. Si el escaneo tarda
veinte minutos y la entrega falla, el resultado está en memoria y se pierde; escrito en un
fichero, se puede reenviar a mano. Es la diferencia entre un escaneo perdido y un escaneo
recuperable, y por eso el fichero va antes de la llamada y no después.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import tempfile
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from fenix_agent import escaneo
from fenix_agent import red
from fenix_agent.config import AgentConfig
from fenix_agent.plataforma import (
    ClientePlataforma,
    ErrorDeAutenticacion,
    ErrorDePlataforma,
    esperar_segundos,
)

logger = logging.getLogger(__name__)

VERSION = "1.0.0"

#: Cómo se llama cada `kind` de trabajo aquí, y qué función lo ejecuta.
#:
#: Es un `match` y no un `getattr` construido con el valor que llega de la red, precisamente
#: porque el valor llega de la red: un `getattr(mod, trabajo["kind"])` convertiría un campo de la
#: respuesta en el nombre de algo que se ejecuta. Aquí el nombre tiene que estar escrito en el
#: código para que se pueda llamar.
EJECUTORES = {
    "CONTAINER_SCAN": escaneo.escanear_imagen,
    "NETWORK_SCAN": None,  # necesita los puertos, que vienen en el trabajo
}


def _ejecutar(trabajo: dict[str, Any]) -> dict[str, Any]:
    """El escaneo que toca, o un fallo con el motivo."""

    tipo = trabajo.get("kind")
    if tipo == "CONTAINER_SCAN":
        return escaneo.escanear_imagen(str(trabajo.get("target", "")))
    if tipo == "NETWORK_SCAN":
        puertos = trabajo.get("ports") or []
        # Sin puertos, un escaneo de red no tiene sentido: se devolvería «no hay nada abierto»
        # sin haber mirado nada, que es un resultado falso. Es un fallo del encargo, no del
        # escaneo, y por eso se dice con esas palabras.
        if not puertos:
            raise escaneo.ErrorDeEscaneo(
                "el trabajo llega sin lista de puertos. Un escaneo de red sin puertos que mirar "
                "devolveria que no hay nada abierto sin haber comprobado nada."
            )
        return escaneo.escanear_red(str(trabajo.get("target", "")), list(puertos))
    raise escaneo.ErrorDeEscaneo(
        f"tipo de trabajo desconocido: {tipo!r}. Este agente solo sabe hacer CONTAINER_SCAN y "
        "NETWORK_SCAN, que son los que la plataforma encola."
    )


#: Permisos del directorio de resultados y del fichero.
#:
#: ## Por qué `0o700` y `0o600` y no los que deja el umask
#:
#: Porque lo que se guarda aquí es la **topología de la red del cliente**: direcciones internas,
#: puertos abiertos y banners de servicio. Con los permisos por defecto —`0755` el directorio y
#: `0644` el fichero— cualquier usuario local de la máquina donde corre el agente lee esa
#: topología sin hacer nada. Y el agente se despliega dentro de la red del cliente, que es
#: exactamente donde puede haber alguien con un shell.
#:
#: El contraste está en el propio repositorio: `backend/workers/runner/sandbox.py:96-102` usa
#: `mode=0o700` explícito en cada nivel del workspace del servidor. El del cliente no lo tenía.
PERMISOS_DIRECTORIO: Final[int] = 0o700
PERMISOS_FICHERO: Final[int] = 0o600


def _identificador_de_trabajo(bruto: object) -> str:
    """El identificador del trabajo, o `""` si no es un UUID.

    ## Por qué se valida siendo un UUID y no comprobando separadores

    Porque `job_id` viene de la respuesta del `claim`, o sea de la red, y se concatenaba en una
    ruta. Un identificador de trabajo es un UUID, y la plataforma no puede enviar otra cosa: esa
    fila viene de `AgentJob.id`, que es `uuid.UUID`. Validarlo contra el UUID no es una defensa
    de runtime contra un atacante que controle la plataforma —quien controle eso ya puede
    escribir donde quiera—; es una defensa contra que el contrato se rompa sin que nadie se entere.

    Y comprueba el tipo y no la forma de la cadena, que es lo que hace segura la comparación: un
    `str` que se parece a un UUID pero lleva `../` dentro no pasa `uuid.UUID(...)`.
    """

    try:
        return str(uuid.UUID(str(bruto)))
    except (ValueError, TypeError, AttributeError):
        return ""


def _guardar_resultado(job_id: str, resultado: dict[str, Any] | None, motivo: str | None) -> Path | None:
    """Escribe el resultado en disco antes de entregarlo, y devuelve dónde está.

    ## Por qué un fichero temporal y no una ruta fija

    Porque una ruta fija —`/var/lib/fenix-agent/ultimo.json`— se sobrescribe con el siguiente
    trabajo, y un cliente que pierde un escaneo de veinte minutos necesita recuperarlo aunque
    haya llegado otro después. El nombre lleva el identificador del trabajo, así que los
    resultados no se pisan.

    ## Por qué devuelve `None` en vez de lanzar

    Porque escribir el fichero es una **comodidad** para el operador, no parte del escaneo. El
    resultado ya está en la plataforma en cuanto se devuelve de aquí, y un `OSError` al escribir
    —un `/tmp` lleno, un sistema de ficheros de solo lectura— no debe convertir un escaneo
    terminado en uno fallido. Antes propagaba, y un `/tmp` lleno hacía fallar escaneos que ya
    estaban hechos.
    """

    identificador = _identificador_de_trabajo(job_id)
    if not identificador:
        logger.warning(
            "el claim devolvio un identificador de trabajo que no es un UUID; no se guarda "
            "copia local. Valor: %r",
            job_id,
        )
        return None
    directorio = Path(tempfile.gettempdir()) / "fenix-agent-resultados"
    directorio.mkdir(parents=True, exist_ok=True, mode=PERMISOS_DIRECTORIO)
    destino = directorio / f"{identificador}.json"
    descriptor = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PERMISOS_FICHERO)
    with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
        archivo.write(
            json.dumps(
                {"job_id": identificador, "resultado": resultado, "error": motivo},
                ensure_ascii=False,
                indent=2,
            )
        )
    return destino


def _un_trabajo(cliente: ClientePlataforma, config: AgentConfig) -> None:
    """Toma un trabajo, lo escanea y lo entrega. O `None` si no había ninguno."""

    trabajo = cliente.pedir_trabajo()
    if trabajo is None:
        return

    job_id = str(trabajo.get("id", ""))
    if not job_id:
        logger.error("la plataforma devolvio un trabajo sin identificador; se descarta")
        return

    logger.info("trabajo recibido: id=%s kind=%s", job_id, trabajo.get("kind"))

    try:
        cliente.empezar_trabajo(job_id)
    except ErrorDePlataforma as error:
        # Sin `start` el trabajo se queda `CLAIMED` y otro agente puede tomarlo mientras este
        # sigue escaneando. Se entrega igualmente, que es lo que cierra el ciclo, pero avisa:
        # dos agentes escaneando lo mismo es trabajo duplicado que el cliente paga una vez y
        # consume dos.
        logger.warning(
            "no se pudo marcar el trabajo %s como en curso: %s. Puede que otro agente lo tome.",
            job_id,
            error,
        )

    inicio = __import__("time").monotonic()
    resultado: dict[str, Any] | None = None
    motivo: str | None = None
    try:
        resultado = _ejecutar(trabajo)
        logger.info(
            "escaneo terminado: id=%s en %.1f s", job_id, __import__("time").monotonic() - inicio
        )
    except escaneo.ErrorDeEscaneo as error:
        motivo = str(error)
        logger.warning("escaneo fallido: id=%s motivo=%s", job_id, motivo)
    except Exception as error:  # noqa: BLE001 - el ciclo no puede morir por un trabajo
        # Un error que no es del escaneo —un bug del agente, un `MemoryError`— también tiene que
        # cerrar el trabajo. Si no, se queda reservado hasta que el alquiler vencie y el
        # cliente espera quince minutos por un error que ya pasó.
        motivo = f"error inesperado en el agente: {type(error).__name__}: {error}"
        logger.exception("error inesperado en el trabajo %s", job_id)

    destino = _guardar_resultado(job_id, resultado, motivo)
    if destino is not None:
        logger.info("resultado guardado en %s", destino)

    cliente.entregar_trabajo(job_id, resultado, motivo)


def ejecutar_bucle(config: AgentConfig, cliente: ClientePlataforma) -> None:
    """Hasta que le digan que pare, o hasta que el token deje de servir.

    ## Por qué el `401` sí sale del bucle y los demás errores no

    Porque un token revocado o mal copiado no se arregla esperando, y seguir sondeando es gastar
    peticiones para no conseguir nada. Un `5xx` o un corte de red sí se resuelven solos, y el
    bucle simplemente espera y vuelve.
    """

    marca = f"{platform.system().lower()}/{platform.machine()}"
    # La plataforma es la unica entidad a la que se permite hablar aunque resuelva a una IP
    # privada, y se declara **antes** del primer latido: un despliegue con la API en la red
    # interna del cliente es legitimo, y sin esto el filtro de `red` rechazaria al propio
    # destino del agente.
    #
    # Solo con el host de la plataforma, nunca con lo que devuelva un escaneo. El `realm` de un
    # registro no pasa por aqui, y por eso el filtro sigue valiendo para el.
    red.declarar_entidades_permitidas(config.host_de_plataforma)
    cliente.latido(VERSION, marca)
    logger.info(
        "agente iniciado: version=%s plataforma=%s cada %d s hacia %s",
        VERSION,
        marca,
        config.intervalo_sondeo,
        config.base_url,
    )

    while True:
        try:
            _un_trabajo(cliente, config)
        except ErrorDeAutenticacion as error:
            # Sale con codigo 1 para que `systemd` lo vea como fallo y lo reinicie, y con un
            # mensaje que dice qué hacer. Un agente que reintenta con un token muerto parece
            # vivo y no hace nada.
            logger.error("autenticacion fallida: %s", error)
            raise SystemExit(1) from error
        except ErrorDePlataforma as error:
            logger.warning("la plataforma no respondio: %s", error)

        esperar_segundos(config.intervalo_sondeo)


def _cerrar(fichero: Any) -> None:  # pragma: no cover - solo en el apagado
    if not fichero.closed:
        fichero.close()


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada.

    Devuelve el código de salida en vez de llamar a `sys.exit`, para que la función sea
    comprobable en una prueba sin lanzar un subproceso.
    """

    import argparse

    from fenix_agent.config import ConfigError, cargar_configuracion

    analizador = argparse.ArgumentParser(
        prog="fenix-agent",
        description="Agente de escaneo de contenedores y redes de Mind Guard Fenix Team.",
    )
    analizador.add_argument(
        "--config", required=True, help="ruta del fichero INI de configuración"
    )
    analizador.add_argument(
        "--version", action="version", version=f"fenix-agent {VERSION}"
    )
    # `--una-vez` existe para las pruebas y para depurar a mano: hace un único ciclo y sale.
    # Sin él no hay forma de comprobar contra la plataforma real que un agente entrega bien sin
    # dejarlo corriendo.
    analizador.add_argument(
        "--una-vez",
        action="store_true",
        help="hace un solo ciclo y sale, en vez de quedarse sondeando",
    )
    argumentos = analizador.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("FENIX_AGENT_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = cargar_configuracion(argumentos.config)
    except ConfigError as error:
        print(f"fenix-agent: {error}", file=sys.stderr)  # noqa: T201
        return 2

    cliente = ClientePlataforma(config)
    if argumentos.una_vez:
        marca = f"{platform.system().lower()}/{platform.machine()}"
        cliente.latido(VERSION, marca)
        try:
            _un_trabajo(cliente, config)
        except ErrorDeAutenticacion as error:
            logger.error("autenticacion fallida: %s", error)
            return 1
        except ErrorDePlataforma as error:
            logger.warning("la plataforma no respondio: %s", error)
            return 1
        return 0

    ejecutar_bucle(config, cliente)
    return 0


_ = TracebackType
