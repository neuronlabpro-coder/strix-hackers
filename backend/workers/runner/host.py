"""Ejecución del motor como proceso del host, en el workspace del run.

## Por qué existe este modo

Porque la imagen `strix_sandbox_image` **no lleva el motor dentro**. Está verificado: su
`.venv` solo trae `httpx`, `pysemver`, `websockets` y `gql-cli`, y `exec: strix` en el
contenedor responde `not found` con exit 127. El modo contenedor, que es el valor por defecto
y debe seguir siéndolo, no puede ejecutar un escaneo real en esta máquina.

## Qué NO puede hacer este modo, y por qué sigue sin ser el valor por defecto

No es una lista de formalidades; son las dos garantías que R3 pide y que aquí no se pueden dar:

1. **Aislamiento.** El proceso hijo hereda los permisos del usuario del worker. La red de
   Tailscale, PostgreSQL y Redis le quedan al alcance, igual que el sistema de ficheros del
   despliegue. El modo contenedor corre con `cap_drop=ALL`, `no-new-privileges`, límites de
   memoria, CPU y PIDs, y una red bridge con cerco de salida. Aquí no hay forma de fingir que lo
   hay: `subprocess` no construye un namespace de red, y este modo no lo simula.
2. **Purgado garantizado.** El modo contenedor quita el contenedor por identificador aunque el
   proceso muera de forma anormal. Aquí hay que matar el árbol de procesos, y un árbol que se
   queda vivo sigue consumiendo tokens del LLM, que es dinero real.

Por eso el modo se pide **por configuración** (`STRIX_EXECUTION_MODE=host`) y nunca se deduce,
y por eso el contenedor sigue siendo el valor por defecto en el código.

## Las cuatro variables de entorno

El motor las necesita y sin ellas no arranca: `STRIX_LLM`, `LLM_API_KEY`, `LLM_API_BASE` y
`STRIX_NON_INTERACTIVE=1`. Se construyen con el mismo helper que usa el modo contenedor
(`exigir_reconocimiento_de_exposicion()` y `attribution_environment()`), de forma que la clave
sigue pasando por la decisión explícita de exposición y la atribución llega a los dos lados
desde la misma declaración.

## Los dos timeouts, y por qué se matan los dos

`strix_soft_timeout_seconds` mata y marca el run como agotado: el motor seguía trabajando y ya
no va a llegar. `strix_hard_timeout_seconds` es el muro de Celery. Ambos matan **el árbol**
del proceso, no solo el hijo, y por el motivo del punto 2: matar al padre dejando a los nietos
vivos deja un escaneo gastando tokens sin que nadie lo espere.

## Por qué no hay reintentos aquí dentro

Porque este modo ejecuta escaneos de verdad que cobran tokens del proveedor. Un bucle de
reintento alrededor de un proceso que puede llevar veinte minutos y costar un dólar es la forma
más directa de disparar gasto sin tope. La política de reintentos vive en `workers/tasks.py`,
que ya la tiene, y aquí solo se ejecuta una vez.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from backend.apps.llm_router.attribution import attribution_environment
from backend.core.config import settings
from backend.workers.runner.exceptions import (
    SandboxCleanupError,
    SandboxError,
    SandboxExecutionError,
    SandboxTimeoutError,
    SandboxWorkspaceError,
)
from backend.workers.runner.llm_key_exposure import exigir_reconocimiento_de_exposicion
from backend.workers.runner.strix_artefactos import EjecucionStrix, leer_ejecucion

logger = logging.getLogger(__name__)

#: Prefijo de la referencia de un proceso del host, tal como se guarda en
#: `pentest_runs.container_id`. Sin el, un PID sería indistinguible de un ID de contenedor.
PREFIJO_REFERENCIA_HOST = "host-pid:"


class _ProcesoMetable(Protocol):
    """Lo único que `matar_arbol` necesita saber de un proceso.

    Un `Protocol` y no `subprocess.Popen[bytes]` porque el camino de aborto no tiene el `Popen`:
    el proceso se lanzó en el hilo del worker y desde la API solo llega su PID. Aceptar el
    protocolo hace que los dos caminos compartan **la misma** muerte de árbol, que es lo que no
    puede estar duplicado: dos copias divergen, y la que se queda sin matar es la que deja un
    escaneo gastando tokens.

    `pid` es de solo lectura porque `_ReferenciaDeProceso` es un `@dataclass(frozen=True)`, y un
    atributo escribible en el protocolo rechazaría el único objeto que se le pasa por ese camino.
    """

    @property
    def pid(self) -> int:
        """El identificador del sistema operativo."""
        ...

    def poll(self) -> int | None:
        """Código de salida si el proceso terminó, `None` si sigue vivo."""
        ...

    def kill(self) -> None:
        """Mata **solo** este proceso. Es el último recurso, no el primero."""
        ...


@dataclass(frozen=True, slots=True)
class HostRunResult:
    """Lo que devuelve una ejecución en modo host.

    No trae `output_json` porque en este modo **no hay un JSON de salida**: el motor escribe
    cuatro artefactos en `strix_runs/<run_name>/` y eso es lo que se lee. Traer un
    `output_json` aquí obligaría a fabricar un documento intermedio para que el consumidor
    hiciera lo mismo que ya hace con `EjecucionStrix`.
    """

    exit_code: int
    ejecucion: EjecucionStrix
    #: El PID del proceso, para poder pararlo desde otro camino. Se rellena aunque ya haya
    #: terminado, porque es lo que permite auditar qué se lanzó.
    process_id: int | None

    def referencia_de_proceso(self) -> str:
        """La referencia que se guarda en `pentest_runs.container_id`.

        ## Por qué se escribe aquí y con este prefijo

        Porque `container_id` es la única columna por la que el camino de aborto encuentra el
        proceso en curso, y un PID desnudo es indistinguible de un ID de contenedor: quien lo lea
        no puede saber con qué hay que matarlo. El prefijo lo dice, y `host.py` es el único que
        lo reconoce.

        ## Por qué no se inventa una columna nueva

        Porque es la **misma** columna con el mismo uso —«la referencia de la ejecución en
        curso»— y una columna `host_process_id` obligaría a tocar la ruta de aborto, la respuesta
        de la API y el panel para distinguir dos casos que el usuario no distingue. El nombre de
        la columna es un poco más estrecho que su contenido; el contenido es más honesto que
        duplicar la columna.
        """

        return f"host-pid:{self.process_id}" if self.process_id is not None else "host-pid:0"

    @staticmethod
    def parsear_referencia(referencia: str) -> int | None:
        """El PID de una referencia `host-pid:N`, o `None` si no lo es.

        `None` en vez de una excepcion porque quien llama tiene dos referencias posibles —la de un
        contenedor y la de un proceso del host— y decidir cual es no es un error: es la pregunta.
        """

        if not referencia.startswith(PREFIJO_REFERENCIA_HOST):
            return None
        try:
            valor = int(referencia[len(PREFIJO_REFERENCIA_HOST) :])
        except ValueError:
            return None
        return valor if valor > 0 else None


    #: Variables que Windows necesita para lanzar procesos hijos y que `os.environ` no
#: garantiza en un servicio, en un planificador o en un terminal que no las exporta.
#: En POSIX no se usan: el nombre de cada una y su valor salen de Windows.
VARIABLES_DE_WINDOWS_QUE_SE_COMPLETAN = (
    "COMSPEC",
    "HOME",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "PATH",
)


def _directorio_de_windows(environment: dict[str, str]) -> str | None:
    """El directorio de Windows (`SystemRoot`), o `None` si no se puede determinar.

    `SystemRoot` **no** se deduce como `C:/Windows`: la unidad del sistema no tiene por qué
    ser `C:`. Se busca en `SystemDrive`, que es la variable que Windows define para eso, y
    entre las variables con nombre de unidad por si `SystemDrive` tampoco está exportada. El
    directorio se **comprueba** antes de devolverse: apuntar `SYSTEMROOT` a un sitio que no
    existe rompe `cmd.exe` de una forma que el motor no puede explicar.
    """

    candidatas: list[str] = []
    unidad = environment.get("SystemDrive") or environment.get("HOMEDRIVE")
    if unidad:
        candidatas.append(str(Path(unidad) / "Windows"))
    candidatas.extend(
        valor
        for clave, valor in environment.items()
        if len(clave) == 2 and clave.endswith(":") and valor
    )
    candidatas.append(r"C:\Windows")

    for candidata in candidatas:
        ruta = Path(candidata)
        if (ruta / "System32").is_dir():
            return str(ruta)
    return None


def _cmd_executable_del_sistema(system_root: str) -> str | None:
    """La ruta de `cmd.exe` dentro de `SystemRoot`, o `None` si no existe.

    Se **comprueba en disco** a propósito. `COMSPEC` es la variable que Windows usa para
    lanzar `cmd.exe`, y apuntarla a algo que no está convierte un fallo claro del motor en
    un `FileNotFoundError` sin contexto. Comprobarlo cuesta una llamada al sistema de
    ficheros y evita que el valor inventado llegue al hijo.
    """

    if not system_root:
        return None
    ejecutable = Path(system_root) / "System32" / "cmd.exe"
    return str(ejecutable) if ejecutable.is_file() else None


def _completar_entorno_de_windows(environment: dict[str, str]) -> None:
    """Completa en `environment` las variables de Windows que falten.

    Solo rellena lo que falta: si `os.environ` ya trae la variable, **no se toca**. Windows
    es la autoridad sobre esos valores y este modulo no los adivina.
    """

    if "SYSTEMROOT" not in environment:
        system_root = _directorio_de_windows(environment)
        if system_root is not None:
            environment["SYSTEMROOT"] = system_root

    system_root = environment.get("SYSTEMROOT", "")
    if "COMSPEC" not in environment:
        ejecutable = _cmd_executable_del_sistema(system_root)
        if ejecutable is not None:
            environment["COMSPEC"] = ejecutable

    if "HOME" not in environment:
        perfil = environment.get("USERPROFILE", "")
        environment["HOME"] = perfil or str(Path(system_root) / "Users" / "Default")

    for variable in ("TEMP", "TMP"):
        if variable not in environment:
            valor = _directorio_temporal_del_usuario(environment)
            if valor is not None:
                environment[variable] = valor

    if "PATH" not in environment:
        # `PATH` vacío rompe la resolución del intérprete del propio motor. Se compone con
        # los directorios del sistema, que es lo que Windows garantiza, y no con la lista
        # completa del usuario, que no se debe deducir.
        partes = [
            str(Path(system_root) / "System32") if system_root else "",
            str(Path(system_root)) if system_root else "",
            str(Path(system_root) / "System32" / "Wbem") if system_root else "",
        ]
        camino = os.pathsep.join(p for p in partes if p)
        if camino:
            environment["PATH"] = camino


def _directorio_temporal_del_usuario(environment: dict[str, str]) -> str | None:
    """El directorio temporal del usuario, o `None` si no se puede determinar."""

    perfil = environment.get("USERPROFILE", "")
    if not perfil:
        return None
    temporal = Path(perfil) / "AppData" / "Local" / "Temp"
    return str(temporal) if temporal.is_dir() else None


class StrixHostRunner:
    """Lanza el CLI del motor como subproceso en el workspace de un run."""

    def __init__(
        self,
        run_id: str,
        target: str,
        scan_mode: str = "STANDARD",
        *,
        target_type: str | None = None,
        included_files: list[str] | None = None,
        workspace_root: Path | str | None = None,
        llm_model: str | None = None,
        cli_path: str | None = None,
        max_budget_usd: Decimal | None = None,
        max_turns: int | None = None,
    ) -> None:
        self.run_id = str(UUID(str(run_id)))
        self.target = target
        self.target_type = (target_type or "").upper()
        self.included_files = included_files or []
        self.scan_mode = scan_mode.lower()
        if llm_model is not None and not llm_model.strip():
            raise ValueError("El slug del modelo de LLM no puede estar vacío")
        self.llm_model = (llm_model or settings.default_strix_llm).strip()
        # R1: la ruta del ejecutable viene de configuración. Sin ella el modo host ni siquiera
        # arranca —`Settings` lo rechaza al validarse—, así que aquí solo hay un valor por
        # defecto para el caso de que alguien construya el runner a mano en una prueba.
        self.cli_path = cli_path if cli_path is not None else settings.strix_cli_path
        self.max_budget_usd = max_budget_usd
        self.max_turns = max_turns
        self.workspace_root = Path(workspace_root or settings.strix_workspace_root)
        self.temp_dir: Path | None = None
        self.cleanup_pending = False
        self.process: subprocess.Popen[Any] | None = None

    # ----------------------------------------------------------------- #
    # Workspace
    # ----------------------------------------------------------------- #

    def setup_workspace(self) -> Path:
        """Crea un directorio nuevo y privado; nunca reutiliza uno de otro run."""

        try:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.workspace_root.chmod(0o700)
            run_dir = self.workspace_root / self.run_id
            run_dir.mkdir(mode=0o700, exist_ok=False)
        except OSError as error:
            raise SandboxWorkspaceError(
                f"El host no permite preparar el workspace en {self.workspace_root}: {error}"
            ) from error
        self.temp_dir = run_dir
        try:
            (run_dir / "workspace").mkdir(mode=0o700)
        except Exception as error:
            shutil.rmtree(run_dir, ignore_errors=True)
            if run_dir.exists():
                self.cleanup_pending = True
                raise SandboxCleanupError("No se pudo purgar el workspace incompleto") from error
            self.temp_dir = None
            raise
        return run_dir

    def cleanup(self) -> None:
        """Purga el workspace y se asegura de que no queda ningún proceso vivo."""

        cleanup_errors: list[str] = []
        if self.process is not None:
            if self.process.poll() is None:
                self.matar_arbol(self.process)
            self.process = None

        if self.temp_dir is not None:
            temp_dir = self.temp_dir
            shutil.rmtree(temp_dir, ignore_errors=True)
            if temp_dir.exists():
                cleanup_errors.append(f"workspace: {temp_dir}")
            else:
                self.temp_dir = None

        if cleanup_errors:
            self.cleanup_pending = True
            raise SandboxCleanupError("; ".join(cleanup_errors))

    # ----------------------------------------------------------------- #
    # El comando
    # ----------------------------------------------------------------- #

    def target_argument(self) -> str:
        """El valor de `--target`, que el motor exige con esquema para un dominio.

        ## Por qué se compone aquí y no se guarda con esquema en la base

        Porque `PentestCreate` **rechaza** el esquema en `target_identifier` para un `DOMAIN`
        (`schemas.py:65-77`), y ese rechazo es correcto: un dominio es un dominio, sin ruta ni
        puerto, y admitirlos sería aceptar rutas y credenciales en un campo que el panel muestra.
        El motor, en cambio, necesita una URL. Traducir una cosa en la otra es trabajo del
        runner, que es quien conoce las dos formas, y el esquema con el que se compone sale de
        `STRIX_DEFAULT_TARGET_SCHEME` porque es una política del despliegue.

        Un `REPOSITORY` no se toca: el materializador ya dejó el código en `workspace/` y la
        ruta se pasa tal cual.
        """

        if self.target_type == "REPOSITORY":
            return str(self.temp_dir / "workspace") if self.temp_dir else self.target
        if "://" in self.target:
            return self.target
        return f"{settings.strix_default_target_scheme}://{self.target}"

    def command(self) -> list[str]:
        """El comando del motor.

        ## Lo que aquí **no** hay, y por qué

        - **`--run-name`.** No existe. `-r` es `--resume`, y combinarla con `--target` es error.
          El nombre del run lo genera el motor (`mindguard-site_23ee`, `mindguard-site_8023`), y
          por eso el parser busca el directorio **por patrón** y no por nombre.
        - **`--output`.** No existe. Por eso no se espera ningún `results.json`.
        - **`--fail-on`.** No se pasa. Con `--fail-on` el motor sale con **2** cuando hay hallazgos
          por encima del umbral, y ese 2 sería un fallo del escaneo cuando es un escaneo que
          funcionó y encontró algo. Sin ese flag, del resultado se encarga `run.json` y el código
          de salida solo dice si el proceso terminó.
        - **`--run-name` y `--output`** sí los usa el modo contenedor (`sandbox.py`), y ese modo
          está verificado como **no ejecutable** en esta máquina: la imagen no trae el motor
          dentro. Los dos modos no comparten la lista de argumentos, y por eso los topes de gasto
          solo se añaden aquí, en el modo que de verdad lanza el proceso.

        ## Lo que aquí sí lleva: los dos topes de gasto

        `--max-budget` y `--max-turns` son la diferencia entre un escaneo acotado y uno que se
        discovery sola. El QUICK de referencia costó **1,85 USD** sin que nadie declarara un tope, y
        el presupuesto por defecto está puesto para que ese caso entre con margen y un `deep` que
        se descontrola se pare antes de la factura y no después.
        """

        if not self.cli_path.strip():
            raise SandboxExecutionError(
                "El modo host necesita la ruta del ejecutable del motor (STRIX_CLI_PATH)"
            )
        presupuesto = (
            self.max_budget_usd
            if self.max_budget_usd is not None
            else settings.strix_max_budget_usd
        )
        return [
            self.cli_path,
            "-n",
            "--target",
            self.target_argument(),
            "--scan-mode",
            self.scan_mode,
            # Los dos topes de gasto. Sin ellos el único límite es el timeout, que para cuando
            # salta el escaneo ya ha gastado. Los valores salen de `Settings` (R1) y se formatean
            # con `f"{...:.2f}"` porque el motor espera un decimal y `Decimal("3.00")` impreso con
            # `str` sale como `3`, que también es válido pero esconde que hay centavos detrás.
            "--max-budget",
            f"{presupuesto:.2f}",
            "--max-turns",
            str(self.max_turns if self.max_turns is not None else settings.strix_max_turns),
        ]


    def environment(self) -> dict[str, str]:
        r"""El entorno del proceso, con las cuatro variables que el motor necesita.

        Se construye a partir de `os.environ` y no desde cero: el ejecutable instalado con
        `uv tool install` es un launcher que necesita su propio `PATH` y su `HOME` para encontrar
        su runtime. Un entorno vacío arrancaría el motor y fallaría al resolver el intérprete,
        que es un fallo de este modo y no del motor.

        ## Por qué no basta con heredar `os.environ`

        **`os.environ` no está garantizado completo, y en Windows se nota.** El worker puede
        arrancar desde un servicio, un `planificador` o un terminal que no exporta `COMSPEC` ni
        `HOME`. El motor necesita `COMSPEC` para lanzar `cmd.exe` y necesita un `HOME` con el
        perfil de `uv`. Medido en esta máquina: el proceso del worker tenía 60 variables
        heredadas y le faltaban `COMSPEC` y `HOME`; con eso el motor arrancaba, subía 26
        segundos y moría sin dejar `strix_runs/`, es decir, fallaba por su cuenta y el panel
        receiveía `STRIX_OUTPUT_UNUSABLE` sin causa.

        ## Por qué nada de esto es un hardcodeo

        Cada valor sale de Windows o de lo que ya está en `os.environ`, y solo cuando falta:

        - `COMSPEC` sale de `SystemRoot` **más** `cmd.exe`, y se **valida** contra el disco
          antes de usarlo. Si no existe, se deja como está y el fallo lo reporta el motor, que
          es más honesto que apuntar a un intérprete que no está.
        - `HOME` sale de `USERPROFILE` si falta, y de `USERPROFILE` + `\Profiles\Root` si
          tampoco hay `USERPROFILE`.
        - `SYSTEMROOT`, `TEMP`, `TMP` y `PATH` se toman de lo que Windows yaLXIVIO.

        En POSIX este código no hace nada: las variables que completa son las de Windows.
        """

        environment = dict(os.environ)
        _completar_entorno_de_windows(environment)
        environment.update(
            {
                "STRIX_LLM": self.llm_model,
                "LLM_API_KEY": exigir_reconocimiento_de_exposicion(),
                "LLM_API_BASE": settings.llm_api_base,
                "STRIX_NON_INTERACTIVE": "1",
                **attribution_environment(),
            }
        )
        if self.included_files:
            environment["STRIX_INCREMENTAL_FILES"] = json.dumps(
                sorted(set(self.included_files)), separators=(",", ":")
            )
        return environment

    # ----------------------------------------------------------------- #
    # Ejecución
    # ----------------------------------------------------------------- #

    def run(
        self,
        timeout_seconds: int | None = None,
        soft_timeout_seconds: float | None = None,
        on_started: Callable[[str], None] | None = None,
        *,
        workspace_prepared: bool = False,
    ) -> HostRunResult:
        """Ejecuta el motor y devuelve sus artefactos, ya leídos, antes de purgar.

        ## Por qué lee los artefactos **antes** del `finally`

        Porque el workspace se purga al terminar, siempre (R5), y los artefactos viven dentro.
        Leerlos después del purgado es leer un directorio que ya no existe. Es el mismo orden que
        usa el modo contenedor, y por el mismo motivo.
        """

        timeout = (
            settings.strix_hard_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        soft_timeout = (
            settings.strix_soft_timeout_seconds
            if soft_timeout_seconds is None
            else float(soft_timeout_seconds)
        )
        soft_timer: threading.Timer | None = None
        soft_timeout_triggered = threading.Event()
        primary_error: BaseException | None = None
        try:
            if workspace_prepared:
                if self.temp_dir is None or not self.temp_dir.is_dir():
                    raise SandboxError("El workspace preparado no existe")
                workspace_dir = self.temp_dir
            else:
                workspace_dir = self.setup_workspace()

            comando = self.command()
            logger.info(
                "Ejecutando el motor en modo host para el run %s en %s: %s",
                self.run_id,
                workspace_dir,
                " ".join(comando),
            )
            try:
                proceso = subprocess.Popen(  # noqa: S603
                    comando,
                    cwd=workspace_dir,
                    env=self.environment(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    **_flags_de_proceso(),
                )
            except OSError as error:
                raise SandboxExecutionError("No se pudo lanzar el motor en modo host") from error
            # Se asigna a `self.process` **despues** de crearlo, y se usa una variable local para
            # todo lo que viene después. Dos razones, y las dos son de corrección: el `finally` de
            # `cleanup()` lee `self.process` y con la asignación diferida un fallo en `Popen`
            # dejaría el atributo en `None` sin que nadie lo note; y `self.process.pid` tras la
            # asignación sería una lectura que el type checker no puede narrowing, porque entre
            # las dos líneas cabe un `cleanup` de otro hilo.
            self.process = proceso

            process_id = proceso.pid
            if on_started is not None:
                on_started(str(process_id))

            if soft_timeout > 0:
                soft_timer = threading.Timer(
                    soft_timeout,
                    self._kill_on_soft_timeout,
                    args=(soft_timeout_triggered,),
                )
                soft_timer.daemon = True
                soft_timer.start()

            try:
                salida = proceso.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                self._raise_timeout()
                raise AssertionError("unreachable") from error
            finally:
                if soft_timer is not None:
                    soft_timer.cancel()

            if soft_timeout_triggered.is_set():
                raise SandboxTimeoutError("Strix superó el timeout suave")
            exit_code = proceso.returncode if proceso.returncode is not None else -1
            self._registrar_salida(salida, exit_code)

            # `leer_ejecucion` lanza `StrixRunIncompleteError` si `run.json` no dice
            # `completed`. No hay un `if exit_code != 0` antes: un escaneo que encuentra
            # hallazgos puede salir distinto de cero según la plataforma, y lo que decide si el
            # escaneo es un escaneo es el artefacto del motor, no el código del proceso.
            ejecucion = leer_ejecucion(workspace_dir)
            return HostRunResult(exit_code=exit_code, ejecucion=ejecucion, process_id=process_id)
        except SandboxError as error:
            primary_error = error
            raise
        except Exception as error:
            primary_error = error
            raise SandboxExecutionError("Falló la ejecución del motor en modo host") from error
        finally:
            try:
                self.cleanup()
            except Exception:
                self.cleanup_pending = True
                if primary_error is None:
                    raise
                logger.exception("Falló la limpieza del workspace tras una excepción")

    # ----------------------------------------------------------------- #
    # Salida y muerte del proceso
    # ----------------------------------------------------------------- #

    def _registrar_salida(
        self,
        salida: tuple[Any, Any] | None,
        exit_code: int,
    ) -> None:
        """La salida del proceso va al registro del worker, no a la base de datos.

        ## Por qué no a la base

        Porque `pentest_runs.error_message` es un **código** que el panel traduce, y escribirle
        la salida cruda del motor metería en la base lo que el proceso escupió —que puede llevar
        rutas, cabeceras o fragmentos de entorno— y además un idioma. La salida es del operador y
        vive en el registro, que es donde ya se busca cuando el panel no sabe explicar un código.

        Y solo cuando la salida dice algo: un escaneo correcto no necesita que se le copie su
        ruido al log del worker en cada intento.

        El tipo de `salida` es `tuple[Any, Any]` y no `tuple[bytes | None, bytes | None]` porque
        `communicate()` devuelve `str` o `bytes` según si el proceso se abrió en modo texto, y el
        runner no fuerza el modo texto: se deja en binario y se decodifica aquí con
        `errors="replace"`. Fijar un tipo y no el otro haría que el tipochecker eligiera por el
        autor cuál de los dos es el real.
        """

        if not salida:
            return
        contenido, _ = salida
        if not contenido:
            return
        if isinstance(contenido, bytes):
            texto = contenido.decode("utf-8", errors="replace")
        else:
            texto = str(contenido)
        texto = texto.strip()
        if not texto:
            return
        if exit_code == 0:
            logger.debug("Salida del motor del run %s:\n%s", self.run_id, texto)
            return
        logger.warning(
            "El motor del run %s terminó con código %d:\n%s", self.run_id, exit_code, texto
        )

    def _kill_on_soft_timeout(self, triggered: threading.Event) -> None:
        try:
            self.matar_arbol(self.process)
        finally:
            triggered.set()

    def _raise_timeout(self) -> None:
        self.matar_arbol(self.process)
        raise SandboxTimeoutError("Strix superó el timeout de ejecución")

    @classmethod
    def matar_por_pid(cls, process_id: int) -> None:
        """Mata el árbol de un proceso que ya no tenemos como `Popen`.

        Lo necesita el camino de aborto: el panel manda un identificador guardado, no un
        objeto. Un `Popen` solo existe dentro del hilo que lo creó, y el aborto viene del
        proceso de la API, que es otro distinto.

        Se envuelve en un `Popen` **ficticio** solo para reutilizar `matar_arbol`, y no es un
        truco: `matar_arbol` solo usa `pid` y `poll()`, y un objeto con esos dos métodos es todo
        lo que necesita. Duplicar la lógica de matar en dos sitios garantizaría que divergen.
        """

        if process_id <= 0:
            return
        cls.matar_arbol(_ReferenciaDeProceso(process_id))

    @staticmethod
    def matar_arbol(proceso: _ProcesoMetable | None) -> None:
        """Mata el proceso **y a sus hijos**, no solo al padre.

        ## Por qué el árbol y no el proceso

        Porque el motor lanza herramientas —`httpx`, `nmap`, navegadores— y esas herramientas
        son las que siguen vivas si solo se mata al padre. Un escaneo que sigue vivo cobra tokens
        del proveedor y ocupa la máquina del worker, y el run ya está terminal: nadie lo espera y
        nadie lo va a parar.

        En POSIX `start_new_session=True` da un grupo de procesos propio y `killpg` lo mata
        entero. En Windows no hay `killpg`, y `CTRL_BREAK_EVENT` a un proceso que ya no tiene
        consola —la tiene como subproceso de un servicio— no hace nada, así que se recurre a
        `taskkill /T`, que sí va al árbol. Cuando ninguna de las dos puede, se mata al padre y se
        registra: es lo mejor que hay, y dejarlo en silencio sería fingir que el árbol murió.
        """

        if proceso is None or proceso.poll() is not None:
            return
        if os.name == "nt":
            try:
                # `taskkill` sin ruta absoluta: en Windows vive en `%WINDIR%\System32` y esa
                # variable **no** está garantizada en el `PATH` de un servicio. Se suprime S607
                # con la razón escrita aquí, que es la que hace falta: no es una ruta de
                # entrada del usuario, es el binario del sistema con el que se mata un árbol de
                # procesos en Windows, y no hay alternativa.
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(proceso.pid)],  # noqa: S607
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                return
            except (OSError, subprocess.SubprocessError):
                logger.exception("No se pudo matar el árbol con taskkill; se mata solo el padre")
        else:
            try:
                os.killpg(os.getpgid(proceso.pid), signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                logger.exception("No se pudo matar el grupo de procesos; se mata solo el padre")
        try:
            proceso.kill()
        except OSError:
            logger.exception("No se pudo matar el proceso del motor")


@dataclass(frozen=True, slots=True)
class _ReferenciaDeProceso:
    """Lo mínimo que `matar_arbol` necesita de un proceso: su PID y si sigue vivo.

    Existe para el camino de aborto, donde el proceso se lanzó en **otro** hilo —el del worker—
    y su `Popen` no se puede compartir. Duplicar la lógica de matar en dos sitios garantizaría
    que divergen, y `matar_arbol` es justo la lógica que no puede divergir: es lo que evita que
    un escaneo siga cobrando tokens sin que nadie lo espere.
    """

    pid: int

    def poll(self) -> int | None:
        """`None` mientras el PID exista, como hace `Popen.poll()`.

        Se mira el PID y no el proceso: `os.kill(pid, 0)` dice si alguien tiene ese PID. Es la
        unica pregunta que este objeto tiene que responder, y contestarla de mas —devolviendo
        `None` para un PID muerto— solo haria que `matar_arbol` intentara matar algo que ya no
        esta, lo cual es inocuo pero hace ruido en el registro.
        """

        try:
            os.kill(self.pid, 0)
        except (OSError, ProcessLookupError):
            return 0
        return None

    def kill(self) -> None:
        """Mata solo el proceso indicado.

        En POSIX basta `SIGKILL` al PID; en Windows no hay forma de matar por PID desde Python sin
        `ctypes`, y para eso esta el camino de `taskkill` de `matar_arbol`, que va al árbol. Este
        es el último recurso de los dos: se llega aquí solo cuando ni el grupo ni `taskkill`
        pudieron, y es preferible matar el padre a no matar nada.
        """

        if os.name == "nt":  # pragma: no cover - la batería corre en el sistema que la ejecuta
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/PID", str(self.pid)],  # noqa: S607
                check=False,
                capture_output=True,
                timeout=30,
            )
            return
        try:
            os.kill(self.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            logger.exception("No se pudo matar el proceso %s", self.pid)


def _flags_de_proceso() -> dict[str, Any]:
    """Las banderas de creación que permiten matar el árbol entero.

    En POSIX, `start_new_session=True` mete al hijo en su propio grupo de procesos, y sin eso
    `killpg` mataría también al worker. En Windows, `CREATE_NEW_PROCESS_GROUP` es lo que permite
    actuar sobre el grupo sin tocar la consola del worker.

    Se calculan en una función y no como constante de módulo porque `os.name` se decide en
    ejecución, no en importación, y una constante decidiría el comportamiento del otro sistema
    operativo en el primero que arranque.
    """

    if os.name == "nt":  # pragma: no cover - la batería corre en el sistema que la ejecuta
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


__all__ = ("HostRunResult", "StrixHostRunner")
