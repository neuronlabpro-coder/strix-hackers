"""Fábrica del cliente Docker del worker."""

import docker
from docker.client import DockerClient


def create_docker_client() -> DockerClient:
    """Conecta el worker al daemon Docker local sin compartir el socket con containers."""

    return docker.from_env()
