"""Ciclo de vida efímero del contenedor Strix."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded
from docker.client import DockerClient
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.models.networks import Network
from requests.exceptions import ReadTimeout, RequestException

from backend.apps.llm_router.attribution import attribution_environment
from backend.core.config import settings
from backend.workers.runner.docker_client import create_docker_client
from backend.workers.runner.exceptions import (
    ContainerExecutionError,
    SandboxCleanupError,
    SandboxError,
    SandboxExecutionError,
    SandboxOutputError,
    SandboxTimeoutError,
    SandboxWorkspaceError,
)

from .egress_fence import exigir_cerco_de_salida, subred_de_la_red
from .llm_key_exposure import exigir_reconocimiento_de_exposicion
from .strix_artefactos import leer_texto_protegido

logger = logging.getLogger(__name__)
REMOVE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class SandboxRunResult:
    """Resultado normalizado de una ejecución Strix ya purgada."""

    exit_code: int
    output_json: str
    container_id: str


class StrixSandboxManager:
    """Ejecuta Strix en un bridge y workspace exclusivos por run."""

    def __init__(
        self,
        run_id: str,
        target: str,
        scan_mode: str = "STANDARD",
        *,
        target_type: str | None = None,
        included_files: list[str] | None = None,
        client: DockerClient | None = None,
        workspace_root: Path | str | None = None,
        image: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.run_id = str(UUID(str(run_id)))
        self.target = target
        self.target_argument = (
            "/workspace/target" if target_type and target_type.upper() == "REPOSITORY" else target
        )
        self.included_files = included_files or []
        self.scan_mode = scan_mode.lower()
        if llm_model is not None and not llm_model.strip():
            raise ValueError("El slug del modelo de LLM no puede estar vacío")
        # El modelo llega resuelto por el orquestador. Si no hay ninguno (catálogo
        # vacío o fallo de resolución), se recurre al `DEFAULT_STRIX_LLM`: es
        # preferible un modelo con tarificación desconocida a no ejecutar el
        # escaneo, y el worker deja constancia del slug usado.
        self.llm_model = (llm_model or settings.default_strix_llm).strip()
        self.client = client if client is not None else create_docker_client()
        self.workspace_root = Path(workspace_root or settings.strix_workspace_root)
        self.image = image or settings.strix_sandbox_image
        self.temp_dir: Path | None = None
        self.cleanup_pending = False
        self.container: Container | None = None
        self._container_create_attempted = False
        self.network: Network | None = None
        self.network_name = f"{settings.strix_network_prefix}_{self.run_id}"
        self.container_name = f"fenix-strix-{self.run_id}"
        self.run_name = f"fenix_{self.run_id.replace('-', '')}"

    def setup_workspace(self) -> Path:
        """Crea un directorio nuevo y privado; nunca reutiliza uno de otro run."""

        try:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.workspace_root.chmod(0o700)
            run_dir = self.workspace_root / self.run_id
            run_dir.mkdir(mode=0o700, exist_ok=False)
        except OSError as error:
            # El `OSError` se tipa aqui y no mas abajo a proposito: en `run()` los `OSError`
            # del daemon de Docker se convierten en `ContainerExecutionError`, y un fallo de
            # permisos preparando el workspace es del host, no de Docker. Sin esta distincion
            # los dos fallos salen con el mismo motivo y el operador mira el daemon cuando el
            # problema es un `mkdir` sin permisos.
            raise SandboxWorkspaceError(
                f"El host no permite preparar el workspace en {self.workspace_root}: {error}"
            ) from error
        self.temp_dir = run_dir
        try:
            (run_dir / "workspace").mkdir(mode=0o700)
            (run_dir / "output").mkdir(mode=0o700)
        except Exception as error:
            shutil.rmtree(run_dir, ignore_errors=True)
            if run_dir.exists():
                self.cleanup_pending = True
                raise SandboxCleanupError("No se pudo purgar el workspace incompleto") from error
            self.temp_dir = None
            raise
        return run_dir

    @staticmethod
    def _exit_code(wait_result: object) -> int:
        if not isinstance(wait_result, dict):
            raise SandboxExecutionError("Docker devolvió un resultado de espera inválido")
        raw_exit_code = wait_result.get("StatusCode", wait_result.get("status_code", -1))
        try:
            return int(raw_exit_code)
        except (TypeError, ValueError) as error:
            raise SandboxExecutionError("Docker no devolvió un código de salida válido") from error

    @staticmethod
    def _raise_timeout(container: Container | None) -> NoReturn:
        if container is not None:
            try:
                container.kill()
            except (APIError, OSError, RequestException):
                logger.exception("No se pudo matar el contenedor tras el timeout")
        raise SandboxTimeoutError("Strix superó el timeout de ejecución")

    def container_environment(self) -> dict[str, str]:
        """Variables de entorno con las que arranca el contenedor efímero.

        Es parte del contrato del sandbox, no un detalle interno: el modelo que
        inyecta el orquestador y la clave que consume Strix se deciden aquí, y las
        pruebas necesitan poder comprobarlo sin levantar un contenedor.

        ## Por qué la atribución viaja en el entorno

        El motor habla con el proveedor de LLM por su cuenta, desde dentro del contenedor, y
        ese tráfico **no pasa por el backend**. La atribución pública solo se puede
        garantizar si llega a los dos lados: el backend la añade en su cliente y aquí se le
        entrega al contenedor por variable de entorno.

        Se llama a `attribution_environment()` y no se escriben las dos variables a mano, por
        la razón que hace que esto merezca un comentario: si el entorno dijera una cosa y las
        cabeceras del backend otra, la atribución funcionaría a medias y parecería que
        funciona. Construir las dos desde la misma declaración hace que sea imposible.
        """

        environment = {
            "STRIX_LLM": self.llm_model,
            "LLM_API_KEY": exigir_reconocimiento_de_exposicion(),
            "LLM_API_BASE": settings.llm_api_base,
            "STRIX_NON_INTERACTIVE": "1",
            "STRIX_HEADLESS": "1",
            **attribution_environment(),
        }
        if self.included_files:
            environment["STRIX_INCREMENTAL_FILES"] = json.dumps(
                sorted(set(self.included_files)),
                separators=(",", ":"),
            )
        return environment

    def _verificar_cerco_de_salida(self) -> None:
        """Se niega a continuar si el cerco de salida no cubre la red recien creada.

        Va justo despues de crear la red y **antes** de arrancar el contenedor, y ese orden es
        la parte que importa: crear la red no da salida a nada, arrancarla si. Comprobar
        despues seria comprobar un contenedor que ya ha podido falar con la red interna.

        Se propaga la excepcion, no se avisa: el trabajo se marca como fallido y el operador ve
        el motivo, que es lo unico accionable. Un `logger.warning` aqui dejaria el contenedor
        ejecutandose con salida completa.
        """

        exigir_cerco_de_salida(subred_de_la_red(self.network))

    @staticmethod
    def _read_output_file(path: Path) -> str:
        """Lee la salida del sandbox con las mismas defensas que los artefactos del motor.

        El cuerpo **no** está aquí: delega en `leer_texto_protegido`, que es la función que
        también usan `run.json`, `findings.sarif` y `coverage.json` en modo host. Que sea una
        sola implementación es lo que hace imposible que el modo host se quede sin `O_NOFOLLOW`,
        sin la comparación de `st_dev`/`st_ino` o sin el techo de tamaño el día que alguien
        escriba un cuarto lector.

        El nombre del parámetro histórico era `results.json`, que **no existe** como contrato:
        el motor nunca escribe ese fichero. Aquí se lee lo que el modo contenedor haya dejado en
        su salida, que es lo que ese modo produce; el nombre de la etiqueta lo pone quien llama.
        """

        return leer_texto_protegido(path, "la salida del sandbox")

    @staticmethod
    def _kill_on_soft_timeout(
        container: Container,
        triggered: threading.Event,
    ) -> None:
        try:
            container.kill()
        except (APIError, OSError, RequestException):
            logger.exception("No se pudo matar el contenedor tras el timeout suave")
        finally:
            triggered.set()

    def run(
        self,
        timeout_seconds: int | None = None,
        soft_timeout_seconds: float | None = None,
        on_started: Callable[[str], None] | None = None,
        *,
        workspace_prepared: bool = False,
    ) -> SandboxRunResult:
        """Ejecuta Strix y devuelve el JSON leído antes de purgar el workspace."""

        timeout = (
            settings.strix_hard_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        soft_timeout = (
            settings.strix_soft_timeout_seconds
            if soft_timeout_seconds is None
            else soft_timeout_seconds
        )
        primary_error: BaseException | None = None
        soft_timer: threading.Timer | None = None
        soft_timeout_triggered = threading.Event()
        try:
            if workspace_prepared:
                if self.temp_dir is None or not self.temp_dir.is_dir():
                    raise SandboxError("El workspace preparado no existe")
                workspace_dir = self.temp_dir
            else:
                workspace_dir = self.setup_workspace()
            # Un bridge dedicado por run evita compartir el namespace del bridge por defecto.
            #
            # La subred la elige Docker y solo se conoce **despues** de crearla, que es
            # justamente por lo que no se puede instalar aqui el cerco de salida: una regla de
            # `iptables` del host tiene que existir antes de que la red exista. Lo que se hace
            # es comprobar que el cerco esta puesto antes de lanzar nada, y negarse a ejecutar
            # si no lo esta. Ver `egress_fence.py`.
            self.network = self.client.networks.create(
                name=self.network_name,
                driver="bridge",
                labels={"fenix.run_id": self.run_id},
            )
            self._verificar_cerco_de_salida()
            volumes = {
                str(workspace_dir / "workspace"): {
                    "bind": "/workspace/target",
                    "mode": "ro",
                },
                str(workspace_dir / "output"): {
                    "bind": "/workspace/output",
                    "mode": "rw",
                },
            }
            command = [
                "strix",
                "-n",
                "--target",
                self.target_argument,
                "--scan-mode",
                self.scan_mode,
                "--run-name",
                self.run_name,
                "--output",
                "/workspace/output/results.json",
            ]
            # `containers.run()` agrupa create+start y pierde el objeto si start falla.
            # Registrar el create antes de llamar a start permite purgar Created en finally.
            self._container_create_attempted = True
            self.container = self.client.containers.create(
                image=self.image,
                command=command,
                environment=self.container_environment(),
                volumes=volumes,
                mem_limit=settings.strix_memory_limit,
                memswap_limit=settings.strix_memory_limit,
                nano_cpus=int(settings.strix_cpu_limit * 1_000_000_000),
                pids_limit=settings.strix_pids_limit,
                network=self.network_name,
                name=self.container_name,
                labels={"fenix.run_id": self.run_id},
                privileged=False,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                working_dir="/workspace",
            )
            self.container.start()
            if on_started is not None:
                on_started(str(self.container.id))
            if soft_timeout > 0:
                soft_timer = threading.Timer(
                    soft_timeout,
                    self._kill_on_soft_timeout,
                    args=(self.container, soft_timeout_triggered),
                )
                soft_timer.daemon = True
                soft_timer.start()
            try:
                wait_result = self.container.wait(timeout=timeout)
            except (ReadTimeout, TimeoutError) as error:
                self._raise_timeout(self.container)
                raise AssertionError("unreachable") from error
            finally:
                if soft_timer is not None:
                    soft_timer.cancel()
            if soft_timeout_triggered.is_set():
                raise SandboxTimeoutError("Strix superó el timeout suave")
            exit_code = self._exit_code(wait_result)
            output_path = workspace_dir / "output" / "results.json"
            if not output_path.exists():
                raise SandboxOutputError("Strix no generó results.json")
            output_json = self._read_output_file(output_path)
            return SandboxRunResult(
                exit_code=exit_code,
                output_json=output_json,
                container_id=str(self.container.id),
            )
        except SoftTimeLimitExceeded as error:
            primary_error = error
            raise
        except SandboxError as error:
            primary_error = error
            raise
        except (APIError, OSError, RequestException) as error:
            primary_error = error
            raise ContainerExecutionError("No se pudo ejecutar el sandbox Strix") from error
        except Exception as error:
            primary_error = error
            raise SandboxExecutionError("Falló la ejecución del sandbox Strix") from error
        finally:
            try:
                self.cleanup()
            except Exception:
                self.cleanup_pending = True
                if primary_error is None:
                    raise
                logger.exception("Falló la limpieza del sandbox tras una excepción")

    def cleanup(self) -> None:
        """Purga contenedor, red y workspace aunque alguna parte haya fallado."""

        cleanup_errors: list[str] = []
        if self.container is None and self._container_create_attempted:
            # docker-py crea por API y después hace GET; ese GET puede fallar aunque
            # Docker ya haya creado el recurso. El nombre determinista lo recupera.
            try:
                candidate = self.client.containers.get(self.container_name)
                if (candidate.labels or {}).get("fenix.run_id") != self.run_id:
                    cleanup_errors.append("container: identidad del run no verificable")
                else:
                    self.container = candidate
            except NotFound:
                self._container_create_attempted = False
            except (APIError, OSError, RequestException) as error:
                cleanup_errors.append(f"container: no se pudo recuperar: {error}")
        if self.container is not None:
            for attempt in range(1, REMOVE_ATTEMPTS + 1):
                try:
                    self.container.remove(force=True)
                except NotFound:
                    pass
                except (APIError, OSError, RequestException) as error:
                    logger.warning("Cleanup container intento %s falló: %s", attempt, error)
                try:
                    self.client.containers.get(self.container_name)
                except NotFound:
                    self.container = None
                    self._container_create_attempted = False
                    break
                except (APIError, OSError, RequestException) as error:
                    logger.warning("Verificación cleanup intento %s falló: %s", attempt, error)
                if attempt < REMOVE_ATTEMPTS:
                    time.sleep(0.2)
            if self.container is not None:
                cleanup_errors.append("container: sigue presente o no verificable")

        if self.network is not None and not cleanup_errors:
            try:
                self.network.remove()
            except NotFound:
                pass
            except (APIError, OSError, RequestException) as error:
                cleanup_errors.append(f"network: {error}")
            finally:
                self.network = None

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

    @staticmethod
    def purge_workspace(
        run_id: str,
        workspace_root: Path | str | None = None,
    ) -> None:
        """Purga el workspace de un run huérfano tras un reinicio del worker."""

        root = Path(workspace_root or settings.strix_workspace_root)
        run_dir = root / str(UUID(str(run_id)))
        if run_dir.is_dir():
            shutil.rmtree(run_dir, ignore_errors=True)
        elif run_dir.exists():
            run_dir.unlink(missing_ok=True)
        if run_dir.exists():
            raise SandboxCleanupError(f"No se pudo purgar el workspace {run_dir}")

    @classmethod
    def container_name_for_run(cls, run_id: str) -> str:
        """Devuelve el nombre determinista para abortar un run aún no persistido."""

        del cls
        return f"fenix-strix-{UUID(str(run_id))}"

    @classmethod
    def kill_container(
        cls,
        container_id: str,
        *,
        expected_run_id: str | None = None,
        client: DockerClient | None = None,
    ) -> None:
        """Mata y elimina un contenedor por ID o nombre."""

        del cls
        docker_client = client if client is not None else create_docker_client()
        try:
            container = docker_client.containers.get(container_id)
            if expected_run_id is not None:
                labels = container.labels or {}
                if labels.get("fenix.run_id") != expected_run_id:
                    raise ContainerExecutionError(
                        "El contenedor no pertenece al run solicitado"
                    )
            try:
                container.kill()
            finally:
                try:
                    container.remove(force=True)
                except NotFound:
                    pass
        except NotFound:
            return
        except (APIError, OSError, RequestException) as error:
            raise ContainerExecutionError(
                "No se pudo detener y eliminar el contenedor Strix"
            ) from error

    @classmethod
    def remove_network_for_run(
        cls,
        run_id: str,
        *,
        client: DockerClient | None = None,
    ) -> None:
        """Elimina la red bridge dedicada asociada al run."""

        del cls
        docker_client = client if client is not None else create_docker_client()
        network_name = f"{settings.strix_network_prefix}_{UUID(str(run_id))}"
        try:
            network = docker_client.networks.get(network_name)
            network.remove()
        except NotFound:
            return
        except (APIError, OSError, RequestException) as error:
            raise ContainerExecutionError("No se pudo eliminar la red del sandbox") from error
