import json
import time
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from docker.client import DockerClient
from docker.errors import APIError, NotFound
from requests.exceptions import ReadTimeout

from backend.core.config import settings
from backend.workers.runner.exceptions import ContainerExecutionError
from backend.workers.runner.sandbox import (
    SandboxCleanupError,
    SandboxExecutionError,
    SandboxOutputError,
    SandboxTimeoutError,
    StrixSandboxManager,
)


def make_manager(tmp_path: Path, client: MagicMock) -> StrixSandboxManager:
    return StrixSandboxManager(
        run_id="11111111-1111-4111-8111-111111111111",
        target="example.test",
        scan_mode="STANDARD",
        client=cast(DockerClient, client),
        workspace_root=tmp_path,
        image="example/strix:test",
    )


def configure_completed_container(
    manager: StrixSandboxManager,
    client: MagicMock,
) -> MagicMock:
    network = MagicMock()
    client.networks.create.return_value = network
    container = MagicMock()
    container.id = "container-123"
    client.containers.create.return_value = container
    client.containers.get.side_effect = NotFound("gone")

    def wait(**_kwargs: object) -> dict[str, int]:
        assert manager.temp_dir is not None
        output_path = Path(manager.temp_dir) / "output" / "results.json"
        output_path.write_text(
            json.dumps({"scan_id": "scan-123", "status": "completed", "findings": []}),
            encoding="utf-8",
        )
        return {"StatusCode": 0}

    container.wait.side_effect = wait
    return container


def test_sandbox_uses_bridge_network_cgroups_and_no_docker_socket(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)

    result = manager.run(timeout_seconds=5)

    assert result.exit_code == 0
    assert result.container_id == "container-123"
    client.networks.create.assert_called_once()
    network_call = client.networks.create.call_args.kwargs
    assert network_call["driver"] == "bridge"
    assert "11111111-1111-4111-8111-111111111111" in network_call["name"]

    run_call = client.containers.create.call_args.kwargs
    assert run_call["mem_limit"] == "4g"
    assert run_call["memswap_limit"] == "4g"
    assert run_call["nano_cpus"] == 2_000_000_000
    assert run_call["pids_limit"] == 256
    container.start.assert_called_once_with()
    assert run_call["privileged"] is False
    assert run_call["network"] == network_call["name"]
    assert run_call["command"][:2] == ["strix", "-n"]
    assert run_call["command"][2:4] == ["--target", "example.test"]
    assert "--run-name" in run_call["command"]
    assert "STRIX_NON_INTERACTIVE" in run_call["environment"]
    assert "LLM_API_KEY" in run_call["environment"]
    assert all("/var/run/docker.sock" not in str(volume) for volume in run_call["volumes"])
    container.remove.assert_called_once_with(force=True)
    client.networks.create.return_value.remove.assert_called_once_with()


def test_sandbox_can_run_with_a_prepared_workspace_and_incremental_scope(
    tmp_path: Path,
) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    manager.setup_workspace()
    manager.included_files = ["z.py", "a.py"]
    configure_completed_container(manager, client)

    result = manager.run(timeout_seconds=5, workspace_prepared=True)

    assert result.exit_code == 0
    environment = client.containers.create.call_args.kwargs["environment"]
    assert environment["STRIX_INCREMENTAL_FILES"] == '["a.py","z.py"]'
    assert manager.temp_dir is None


def test_sandbox_cleans_workspace_and_container_when_execution_raises(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    container.wait.side_effect = RuntimeError("boom")

    with pytest.raises(SandboxExecutionError):
        manager.run(timeout_seconds=5)

    assert manager.temp_dir is None
    assert not (tmp_path / manager.run_id).exists()
    container.remove.assert_called_once_with(force=True)
    client.networks.create.return_value.remove.assert_called_once_with()


def test_partial_create_start_deny_removes_container_before_network(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    container.start.side_effect = APIError("start DENY")
    events: list[str] = []
    container.remove.side_effect = lambda **_kwargs: events.append("container")
    manager_network = client.networks.create.return_value
    manager_network.remove.side_effect = lambda: events.append("network")

    with pytest.raises(ContainerExecutionError):
        manager.run(timeout_seconds=5)

    assert events == ["container", "network"]
    assert manager.container is None
    assert manager.network is None
    assert manager.cleanup_pending is False


def test_create_api_fails_after_daemon_created_container(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    container.labels = {"fenix.run_id": manager.run_id}
    client.containers.create.side_effect = APIError("GET/parsing after create failed")
    removed = False

    def remove(**_kwargs: object) -> None:
        nonlocal removed
        removed = True

    def get(_name: str) -> MagicMock:
        if removed:
            raise NotFound("gone")
        return container

    container.remove.side_effect = remove
    client.containers.get.side_effect = get

    with pytest.raises(ContainerExecutionError):
        manager.run(timeout_seconds=5)

    assert removed is True
    client.networks.create.return_value.remove.assert_called_once_with()
    assert manager.cleanup_pending is False


def test_post_start_failure_cleans_in_order(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    container.wait.side_effect = RuntimeError("test posterior FAIL")
    events: list[str] = []
    container.remove.side_effect = lambda **_kwargs: events.append("container")
    client.networks.create.return_value.remove.side_effect = lambda: events.append("network")

    with pytest.raises(SandboxExecutionError):
        manager.run(timeout_seconds=5)

    container.start.assert_called_once_with()
    assert events == ["container", "network"]
    assert manager.cleanup_pending is False


def test_transient_container_cleanup_retries_and_verifies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    attempts = 0
    removed = False

    def remove(**_kwargs: object) -> None:
        nonlocal attempts, removed
        attempts += 1
        if attempts == 1:
            raise OSError("Docker busy")
        removed = True

    def get(_name: str) -> MagicMock:
        if removed:
            raise NotFound("gone")
        return container

    container.remove.side_effect = remove
    client.containers.get.side_effect = get
    manager.run(timeout_seconds=5)

    assert attempts == 2
    assert manager.cleanup_pending is False
    client.networks.create.return_value.remove.assert_called_once_with()
    assert "Docker busy" in caplog.text


def test_sandbox_kills_container_and_raises_typed_timeout(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    container.wait.side_effect = ReadTimeout("timeout")

    with pytest.raises(SandboxTimeoutError):
        manager.run(timeout_seconds=1)

    container.kill.assert_called_once_with()
    container.remove.assert_called_once_with(force=True)
    assert manager.temp_dir is None


def test_sandbox_marks_cleanup_pending_when_resource_removal_fails(
    tmp_path: Path,
) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    manager.setup_workspace()
    manager.network = MagicMock()
    manager.network.remove.side_effect = OSError("network busy")

    with pytest.raises(SandboxCleanupError):
        manager.cleanup()

    assert manager.cleanup_pending is True


def test_sandbox_rejects_symlink_output_file(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    outside_file = tmp_path / "outside.json"
    outside_file.write_text(
        json.dumps({"scan_id": "scan-symlink", "status": "completed", "findings": []}),
        encoding="utf-8",
    )

    def create_symlink(**_kwargs: object) -> dict[str, int]:
        assert manager.temp_dir is not None
        output_path = Path(manager.temp_dir) / "output" / "results.json"
        try:
            output_path.symlink_to(outside_file)
        except OSError as error:
            pytest.skip(f"symlink not available: {error}")
        return {"StatusCode": 0}

    container.wait.side_effect = create_symlink
    with pytest.raises(SandboxOutputError):
        manager.run(timeout_seconds=5)

    container.remove.assert_called_once_with(force=True)
    assert manager.temp_dir is None


def test_kill_container_uses_docker_kill_for_id() -> None:
    client = MagicMock()
    container = MagicMock()
    client.containers.get.return_value = container

    StrixSandboxManager.kill_container(
        "container-reference",
        client=cast(DockerClient, client),
    )

    client.containers.get.assert_called_once_with("container-reference")
    container.kill.assert_called_once_with()
    container.remove.assert_called_once_with(force=True)

    StrixSandboxManager.remove_network_for_run(
        "11111111-1111-4111-8111-111111111111",
        client=cast(DockerClient, client),
    )
    # El prefijo de la red es `STRIX_NETWORK_PREFIX`, y en el `.env` de este proyecto de desarrollo
    # vale `strix_local_net`. Escribir `strix_net` aquí fijaba unaquinterna igual que el número de
    # Redis: el test caía según lo que hubiera en el fichero de quien lo ejecutaba. Se afirma lo
    # que el código hace —el nombre sale del prefijo configurado y el del run va detrás— y no un
    # literal que es de otro despliegue.
    client.networks.get.assert_called_once_with(
        f"{settings.strix_network_prefix}_11111111-1111-4111-8111-111111111111"
    )
    client.networks.get.return_value.remove.assert_called_once_with()


def test_el_nombre_de_la_red_usa_el_prefijo_configurado(tmp_path: Path) -> None:
    """El prefijo sale de configuración y no de un literal (R1), y se lee de un solo sitio.

    ## Por qué esta prueba y no basta con la anterior

    Porque la anterior compara contra `settings.strix_network_prefix`, que **también** se leería
    si el código escribiera un literal y la configuración valiera `strix_net` por casualidad. Esta
    cambia el prefijo y comprueba que el nombre le sigue: con el prefijo movido, un literal en el
    código daría un nombre que no empieza por el configurado y la prueba caería.
    """

    cliente = MagicMock()
    # `Settings` está congelado a propósito, así que la prueba construye una copia con el prefijo
    # movido en vez de mutar el global. Es la forma que no depende del `.env` de quien ejecuta.
    otro = settings.model_copy(update={"strix_network_prefix": "otro_prefijo_de_prueba"})

    with patch("backend.workers.runner.sandbox.settings", otro):
        gestor = StrixSandboxManager(
            "11111111-1111-4111-8111-111111111111",
            "app.example.com",
            "QUICK",
            client=cast(DockerClient, cliente),
            workspace_root=tmp_path,
        )

    assert gestor.network_name == "otro_prefijo_de_prueba_11111111-1111-4111-8111-111111111111"


def test_sandbox_soft_timeout_kills_container_and_raises_typed_timeout(tmp_path: Path) -> None:
    client = MagicMock()
    manager = make_manager(tmp_path, client)
    container = configure_completed_container(manager, client)
    container.wait.side_effect = lambda **_kwargs: (time.sleep(0.03), {"StatusCode": 137})[1]

    with pytest.raises(SandboxTimeoutError):
        manager.run(timeout_seconds=1, soft_timeout_seconds=0.001)

    container.kill.assert_called()
    container.remove.assert_called_once_with(force=True)
    assert manager.temp_dir is None
