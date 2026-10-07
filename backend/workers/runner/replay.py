"""Modo replay: los artefactos de un run real, por el mismo camino que un escaneo de verdad.

## Qué es y qué no es

Es un modo **exclusivo de desarrollo** que mete los cuatro artefactos de un run real del motor
(`run.json`, `findings.sarif`, `coverage.json`, `penetration_test_report.md`) en el workspace de un
run y llama **al mismo** `leer_ejecucion` y **a la misma** `_persistir_artefactos_del_motor` que el
modo host. No es un camino paralelo de ingestión: si lo fuera, no estaría probando nada.

No llama a `docker`, no crea red, no lanza el ejecutable del motor y no llama al proveedor de LLM.
Los créditos no se tocan: ni cobra ni reembolsa.

## Por qué existe

Porque la única forma honesta de saber que el pipeline de ingesta, la persistencia y el panel
muestran lo que el motor dijo es meter un artefacto **real** por ese pipeline. Un SARIF inventado
demostraría que el parser lee un SARIF; este demuestra que el motor de verdad llega entero hasta
la ficha del escaneo.

## Por qué el origen se copia y no se lee en el sitio

Dos razones, y las dos son de aislamiento:

1. **R5.** El workspace es el directorio que el runner purga al terminar. Si el replay leyera los
   artefactos en su sitio y no los copiara, la lectura quedaría fuera del ciclo de vida purgado y
   un fichero de un cliente se leería desde una ruta que nadie controla.
2. **El parser no sabe de dónde vienen los artefactos.** `leer_ejecucion` busca
   `strix_runs/<run>/` bajo un workspace. Copiar es lo que hace que la ruta sea la misma y el
   resto del código no tenga que saber que esto es un replay.

## Por qué la referencia dice `replay:` y no un PID

Porque `pentest_runs.container_id` es la referencia de «la ejecución en curso». En un replay no hay
ninguna ejecución en curso: no hay proceso, no hay contenedor y no hay nada que matar.

Escribir un `host-pid:` inventado sería mentir sobre lo que se puede detener, y el parser de
referencias lo leería como algo que matar. La referencia dice lo que pasó, y el camino de aborto la
reconoce como no-matable.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from uuid import UUID

from backend.core.config import settings
from backend.workers.runner.host import StrixHostRunner
from backend.workers.runner.strix_artefactos import (
    DIRECTORIO_DE_RUNS,
    FICHERO_COBERTURA,
    FICHERO_INFORME,
    FICHERO_RUN,
    FICHERO_SARIF,
    EjecucionStrix,
    leer_ejecucion,
)

logger = logging.getLogger(__name__)

#: Prefijo de la referencia de un replay en `pentest_runs.container_id`.
#:
#: Distinto de `host-pid:` a propósito: un replay no tiene árbol de procesos que matar, y quien
#: lo lea tiene que poder distinguir «no hay nada vivo» de «hay un proceso que se puede parar».
PREFIJO_REFERENCIA_REPLAY = "replay:"

#: Los cuatro artefactos que el motor deja, en el orden en que se comprueban.
#:
#: Los dos primeros son **obligatorios**: sin `run.json` no hay estado y sin `findings.sarif` no hay
#: resultados, y `leer_ejecucion` lanza si faltan. Los otros dos son opcionales porque el motor no
#: los escribe siempre, y un run sin ellos sigue siendo un run.
ARTEFACTOS_OBLIGATORIOS: tuple[str, ...] = (FICHERO_RUN, FICHERO_SARIF)
ARTEFACTOS_OPCIONALES: tuple[str, ...] = (FICHERO_COBERTURA, FICHERO_INFORME)


def referencia_de_replay(origen: str) -> str:
    """La referencia que se guarda en `pentest_runs.container_id` para un replay.

    Lleva el **origen** y no un contador: el valor tiene que poder leerse y decir de dónde
    salieron los artefactos, porque es lo único que distingue dos replays en el historial.
    """

    return f"{PREFIJO_REFERENCIA_REPLAY}{origen}"


def es_replay(referencia: str | None) -> bool:
    """¿Esta referencia es de un replay?"""

    return bool(referencia) and referencia.startswith(PREFIJO_REFERENCIA_REPLAY)


class StrixReplayRunner:
    """Copia los artefactos de un run real al workspace de este run y los lee.

    ## Por qué usa el workspace de `StrixHostRunner` en vez del suyo

    Porque el workspace es la parte que **sí** es la misma: `0o700`, un directorio por run que no
    se reutiliza, y el purgado con la marca `cleanup_pending` para el watchdog. Escribir una
    segunda versión de eso sería tener dos sitios donde un directorio de escaneo puede quedar en
    disco, y R5 no puede garantizarse dos veces. Lo que este runner **no** hereda son las piezas
    que son de un proceso —`command()`, `environment()`, `matar_arbol`— porque aquí no hay proceso.

    ## Por qué no expone un `cleanup()` propio

    Porque `run()` purga en su `finally` siempre, y un segundo método de purgado sería un segundo
    sitio donde se podría intentar. El watchdog lee `cleanup_pending`, que es una propiedad
    delegada al runner de host y por tanto tiene una sola política.
    """

    def __init__(
        self,
        run_id: str,
        *,
        source: str | None = None,
        target: str = "",
        workspace_root: Path | str | None = None,
    ) -> None:
        #: `None` cuando no hay origen configurado, y ese caso **no** se puede lanzar. Se comprueba
        #: en `run()` y no en `__init__` para que construir el runner no tenga efectos.
        origen = (source or settings.strix_replay_source).strip()
        self.source = Path(origen) if origen else None
        #: El runner de host solo se usa por su workspace, y ese workspace se identifica por el
        #: `run_id` —que se normaliza a UUID porque `setup_workspace()` lo valida—. El objetivo se
        #: pasa igualmente para que el objeto interno sea válido si alguien lo inspecciona, aunque
        #: el replay **no** compone ningún comando y por tanto no lo usa.
        self._host = StrixHostRunner(
            str(UUID(str(run_id))),
            target=target or "sin-objetivo",
            workspace_root=workspace_root or settings.strix_workspace_root,
        )

    @property
    def cleanup_pending(self) -> bool:
        return self._host.cleanup_pending

    @cleanup_pending.setter
    def cleanup_pending(self, value: bool) -> None:
        self._host.cleanup_pending = value

    def run(self) -> tuple[EjecucionStrix, str]:
        """Copia, lee y devuelve los artefactos junto con la referencia del replay.

        El workspace se purga **siempre**, en el `finally`, por R5: los artefactos de un run real
        incluyen el informe en markdown, que es una descripción del objetivo del cliente.
        """

        if self.source is None:
            raise ValueError("El replay necesita un directorio de origen configurado")

        workspace = self._host.setup_workspace()
        try:
            destino_run = workspace / DIRECTORIO_DE_RUNS / self.source.name
            destino_run.mkdir(parents=True, exist_ok=True)
            self._copiar(self.source, destino_run)
            ejecucion = leer_ejecucion(workspace)
        finally:
            self._host.cleanup()
        referencia = referencia_de_replay(self.source.name)
        logger.info(
            "Replay del run %s desde %s: %d hallazgo(s), %d registro(s) de cobertura, "
            "coste declarado %s USD",
            ejecucion.run_name,
            self.source,
            len(ejecucion.hallazgos),
            ejecucion.registros_cobertura,
            ejecucion.coste,
        )
        return ejecucion, referencia

    @staticmethod
    def _copiar(origen: Path, destino: Path) -> None:
        """Copia los cuatro artefactos, y falla si faltan los dos obligatorios.

        El fallo es explícito y nombra el origen: un replay que no encuentra `run.json` no es un
        problema del motor —no lo hay— sino de quien escribió la ruta en la configuración, y ese
        mensaje es el único sitio donde se puede ver.
        """

        if not origen.is_dir():
            raise FileNotFoundError(
                f"El origen del replay no existe o no es un directorio: {origen}"
            )

        for nombre in ARTEFACTOS_OBLIGATORIOS + ARTEFACTOS_OPCIONALES:
            fuente = origen / nombre
            if not fuente.is_file():
                if nombre in ARTEFACTOS_OBLIGATORIOS:
                    raise FileNotFoundError(
                        f"El origen del replay no tiene {nombre}: {origen}"
                    )
                logger.info("El origen del replay no trae %s; el run seguirá sin él", nombre)
                continue
            shutil.copyfile(fuente, destino / nombre)


__all__ = (
    "ARTEFACTOS_OBLIGATORIOS",
    "ARTEFACTOS_OPCIONALES",
    "PREFIJO_REFERENCIA_REPLAY",
    "StrixReplayRunner",
    "es_replay",
    "referencia_de_replay",
)
