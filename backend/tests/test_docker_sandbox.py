import json
import time
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from docker.client import DockerClient
from requests.exceptions import ReadTimeout

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
    client.containers.run.return_value = container

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

    run_call = client.containers.run.call_args.kwargs
    assert run_call["mem_limit"] == "4g"
    assert run_call["memswap_limit"] == "4g"
    assert run_call["nano_cpus"] == 2_000_000_000
    assert run_call["pids_limit"] == 256
    assert run_call["detach"] is True
    assert run_call["remove"] is False
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
    environment = client.containers.run.call_args.kwargs["environment"]
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
    client.networks.get.assert_called_once_with(
        "strix_net_11111111-1111-4111-8111-111111111111"
    )
    client.networks.get.return_value.remove.assert_called_once_with()


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
