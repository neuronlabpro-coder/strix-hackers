"""El worker no consume trabajos con un motor o Docker inoperantes."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.workers.runner import production_preflight as preflight


@pytest.fixture
def production_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Mock]:
    cli = tmp_path / "strix"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setattr(
        preflight,
        "settings",
        SimpleNamespace(
            environment="production",
            strix_execution_mode="host",
            strix_cli_path=str(cli),
            strix_workspace_root=str(tmp_path),
        ),
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="strix 1.7.0", stderr=""),
    )
    monkeypatch.setattr(Path, "is_socket", lambda self: self.as_posix() == "/var/run/docker.sock")
    client = Mock()
    client.ping.return_value = True
    monkeypatch.setattr(preflight.docker, "from_env", lambda: client)
    return cli, client


def test_validated_worker_can_start(production_runner: tuple[Path, Mock]) -> None:
    _, client = production_runner
    preflight.verify_production_runner()
    client.ping.assert_called_once()
    client.close.assert_called_once()


def test_worker_rejects_missing_cli(production_runner: tuple[Path, Mock]) -> None:
    cli, _ = production_runner
    cli.unlink()
    with pytest.raises(RuntimeError, match="STRIX_CLI_PATH"):
        preflight.verify_production_runner()


def test_worker_rejects_failed_version_command(
    production_runner: tuple[Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    del production_runner
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        Mock(side_effect=preflight.subprocess.CalledProcessError(1, "strix --version")),
    )
    with pytest.raises(RuntimeError, match="strix --version"):
        preflight.verify_production_runner()


def test_worker_rejects_wrong_cli_version(
    production_runner: tuple[Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    del production_runner
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="strix 1.6.2", stderr=""),
    )
    with pytest.raises(RuntimeError, match=r"1\.7\.0"):
        preflight.verify_production_runner()


def test_worker_rejects_missing_docker_socket(
    production_runner: tuple[Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    del production_runner
    monkeypatch.setattr(Path, "is_socket", lambda _self: False)
    with pytest.raises(RuntimeError, match=r"docker\.sock"):
        preflight.verify_production_runner()
