"""Gates del contrato entre imagen, Compose y arranque del worker."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.workers import tasks
from backend.workers.runner.exceptions import SandboxExecutionError

ROOT = Path(__file__).resolve().parents[2]


def test_image_installs_pinned_cli_and_checks_its_version() -> None:
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    assert "'strix-agent==1.7.0'" in dockerfile
    assert "/opt/strix/bin/strix --version" in dockerfile
    assert "COPY --from=build /opt/strix /opt/strix" in dockerfile
    assert 'PATH="/app/backend/.venv/bin:/opt/strix/bin:$PATH"' in dockerfile


def test_production_worker_uses_host_runner_and_preflight() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    worker = compose.split("  celery_worker:", 1)[1].split("  celery_beat:", 1)[0]
    assert "STRIX_EXECUTION_MODE: host" in compose
    assert "STRIX_CLI_PATH: /opt/strix/bin/strix" in compose
    assert "STRIX_WORKSPACE_ROOT: /tmp/fenix_workspaces" in compose
    assert "/app/backend/scripts/start_worker.sh" in worker
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" not in worker
    assert "unix:///run/fenix-docker/docker.sock" in worker
    assert "STRIX_DOCKER_SANDBOX_NETWORK:" in worker
    assert "/tmp/fenix_workspaces:/tmp/fenix_workspaces" in worker  # noqa: S108
    assert "${STRIX_WORKSPACE_ROOT" not in worker


def test_worker_preflight_runs_before_celery() -> None:
    script = (ROOT / "backend/scripts/start_worker.sh").read_text(encoding="utf-8")
    assert script.index("python -m backend.workers.runner.production_preflight") < script.index(
        "exec celery"
    )


@pytest.mark.asyncio
async def test_production_dispatches_to_host_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tasks,
        "settings",
        SimpleNamespace(
            environment="production", strix_execution_mode="host", strix_replay_source=""
        ),
    )
    calls: list[str] = []

    async def host(*_args: object) -> object:
        calls.append("host")
        return object()

    async def old_container(*_args: object) -> object:
        calls.append("container")
        return object()

    monkeypatch.setattr(tasks, "_run_attempt_en_host", host)
    monkeypatch.setattr(tasks, "_run_attempt_en_contenedor", old_container)
    run_id, organization_id = uuid4(), uuid4()
    await tasks._run_attempt(run_id, organization_id, "example.com", "QUICK", "URL", "glm")
    assert calls == ["host"]

    tasks.settings.strix_execution_mode = "container"
    with pytest.raises(SandboxExecutionError, match="StrixSandboxRunner"):
        await tasks._run_attempt(run_id, organization_id, "example.com", "QUICK", "URL", "glm")
    assert calls == ["host"]
