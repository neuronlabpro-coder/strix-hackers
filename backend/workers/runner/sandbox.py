"""Ciclo de vida efímero del contenedor Strix."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import threading
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

from backend.core.config import settings
from backend.workers.runner.docker_client import create_docker_client
from backend.workers.runner.exceptions import (
    ContainerExecutionError,
    SandboxCleanupError,
    SandboxError,
    SandboxExecutionError,
    SandboxOutputError,
    SandboxTimeoutError,
)

logger = logging.getLogger(__name__)


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
        client: DockerClient | None = None,
        workspace_root: Path | str | None = None,
        image: str | None = None,
    ) -> None:
        self.run_id = str(UUID(str(run_id)))
        self.target = target
        self.target_argument = (
            "/workspace/target" if target_type and target_type.upper() == "REPOSITORY" else target
        )
        self.scan_mode = scan_mode.lower()
        self.client = client if client is not None else create_docker_client()
        self.workspace_root = Path(workspace_root or settings.strix_workspace_root)
        self.image = image or settings.strix_sandbox_image
        self.temp_dir: Path | None = None
        self.cleanup_pending = False
        self.container: Container | None = None
        self.network: Network | None = None
        self.network_name = f"{settings.strix_network_prefix}_{self.run_id}"
        self.container_name = f"fenix-strix-{self.run_id}"
        self.run_name = f"fenix_{self.run_id.replace('-', '')}"

    def setup_workspace(self) -> Path:
        """Crea un directorio nuevo y privado; nunca reutiliza uno de otro run."""

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root.chmod(0o700)
        run_dir = self.workspace_root / self.run_id
        run_dir.mkdir(mode=0o700, exist_ok=False)
        self.temp_dir = run_dir
        try:
            (run_dir / "workspace").mkdir(mode=0o700)
            (run_dir / "output").mkdir(mode=0o700)
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
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

    def _container_environment(self) -> dict[str, str]:
        return {
            "STRIX_LLM": settings.default_strix_llm,
            "LLM_API_KEY": settings.llm_api_key.get_secret_value(),
            "LLM_API_BASE": settings.llm_api_base,
            "STRIX_NON_INTERACTIVE": "1",
            "STRIX_HEADLESS": "1",
        }

    @staticmethod
    def _read_output_file(path: Path) -> str:
        """Lee un archivo regular sin seguir symlinks y con límite estricto."""

        try:
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode):
                raise SandboxOutputError("results.json no es un archivo regular")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as output_file:
                opened_stat = os.fstat(output_file.fileno())
                if not stat.S_ISREG(opened_stat.st_mode):
                    raise SandboxOutputError("results.json cambió a un tipo no regular")
                if (path_stat.st_dev, path_stat.st_ino) != (
                    opened_stat.st_dev,
                    opened_stat.st_ino,
                ):
                    raise SandboxOutputError("results.json cambió durante la lectura")
                data = output_file.read(settings.strix_max_output_bytes + 1)
        except SandboxOutputError:
            raise
        except OSError as error:
            raise SandboxOutputError("No se pudo leer results.json") from error
        if len(data) > settings.strix_max_output_bytes:
            raise SandboxOutputError("results.json supera el tamaño máximo permitido")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SandboxOutputError("results.json no está codificado en UTF-8") from error

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
            workspace_dir = self.setup_workspace()
            # Un bridge dedicado por run evita compartir el namespace del bridge por defecto.
            self.network = self.client.networks.create(
                name=self.network_name,
                driver="bridge",
                labels={"fenix.run_id": self.run_id},
            )
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
            self.container = self.client.containers.run(
                image=self.image,
                command=command,
                environment=self._container_environment(),
                volumes=volumes,
                mem_limit=settings.strix_memory_limit,
                memswap_limit=settings.strix_memory_limit,
                nano_cpus=int(settings.strix_cpu_limit * 1_000_000_000),
                pids_limit=settings.strix_pids_limit,
                network=self.network_name,
                name=self.container_name,
                labels={"fenix.run_id": self.run_id},
                detach=True,
                remove=False,
                privileged=False,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                working_dir="/workspace",
            )
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
        if self.container is not None:
            try:
                self.container.remove(force=True)
            except NotFound:
                pass
            except (APIError, OSError, RequestException) as error:
                cleanup_errors.append(f"container: {error}")
            finally:
                self.container = None

        if self.network is not None:
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
