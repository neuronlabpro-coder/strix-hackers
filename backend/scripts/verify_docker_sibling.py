"""Prueba local sin LLM del socket y del bind host/worker/sibling."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import docker


def main(image: str) -> None:
    root = Path(os.environ["STRIX_WORKSPACE_ROOT"])
    probe = root / f"strix-dood-probe-{uuid4()}"
    client = docker.from_env()
    container = None
    try:
        if not client.ping():
            raise RuntimeError("El socket Docker no responde")
        print("DOCKER SOCKET = PASS", flush=True)
        probe.mkdir(mode=0o700)
        container = client.containers.create(
            image=image,
            command=["sh", "-c", "printf sibling-ok > /workspace/proof.txt"],
            volumes={str(probe): {"bind": "/workspace", "mode": "rw"}},
            network_disabled=True,
            user="10001:10001",
            labels={"fenix.probe": "strix-dood"},
        )
        container.start()
        result = container.wait(timeout=30)
        if result["StatusCode"] != 0:
            raise RuntimeError(f"El sibling falló: {result['StatusCode']}")
        print("DOCKER SIBLING = PASS", flush=True)
        if (probe / "proof.txt").read_text(encoding="utf-8") != "sibling-ok":
            raise RuntimeError("El fichero del sibling no es visible en el worker")
        print("WORKSPACE BIND = PASS", flush=True)
    finally:
        if container is not None:
            container.remove(force=True)
        shutil.rmtree(probe, ignore_errors=True)
        client.close()


if __name__ == "__main__":
    main(sys.argv[1])
