"""Comprueba el motor y Docker antes de iniciar el consumidor Celery de producción."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import docker

from backend.core.config import settings


def verify_production_runner() -> None:
    if settings.environment != "production":
        return
    if settings.strix_execution_mode != "host":
        raise RuntimeError("Producción requiere STRIX_EXECUTION_MODE=host")

    cli_path = Path(settings.strix_cli_path)
    if not cli_path.is_absolute() or not cli_path.is_file() or not os.access(cli_path, os.X_OK):
        raise RuntimeError("STRIX_CLI_PATH no apunta a un ejecutable existente")
    try:
        result = subprocess.run(  # noqa: S603 - ruta validada y argumentos constantes
            [str(cli_path), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("strix --version falló en el worker") from error
    if (result.stdout + result.stderr).strip() != "strix 1.7.0":
        raise RuntimeError("strix --version debe devolver strix 1.7.0")

    socket_path = Path("/var/run/docker.sock")
    if not socket_path.is_socket():
        raise RuntimeError("El worker no tiene /var/run/docker.sock")
    client = docker.from_env()
    try:
        if not client.ping():
            raise RuntimeError("El daemon Docker no responde al worker")
    finally:
        client.close()

    workspace = Path(settings.strix_workspace_root)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise RuntimeError("STRIX_WORKSPACE_ROOT debe ser un directorio absoluto montado")
    if not os.access(workspace, os.W_OK | os.X_OK):
        raise RuntimeError("El worker no puede escribir en el workspace compartido")


if __name__ == "__main__":
    verify_production_runner()
