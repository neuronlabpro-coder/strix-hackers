import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.workers.runner.exceptions import SandboxTimeoutError
from backend.workers.tasks import (
    execute_pentest_run,
    mark_run_timed_out,
    reconcile_orphaned_runs,
    schedule_startup_watchdog,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_watchdog_marks_stale_running_runs_failed(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Watchdog {uuid.uuid4().hex}",
        slug=f"watchdog-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="watchdog.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.RUNNING,
        started_at=datetime.now(UTC) - timedelta(hours=2),
        container_id=None,
    )
    integration_session.add(run)
    await integration_session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    integration_session.add(repository)
    await integration_session.flush()
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        run_id=run.id,
        pr_number=1,
        pr_title="Orphaned PR",
        pr_author="alice",
        source_branch="feature/orphan",
        target_branch="main",
        commit_sha="a" * 40,
        status=PRReviewStatusEnum.SCANNING,
    )
    integration_session.add(review)
    await integration_session.flush()
    workspace = tmp_path / str(run.id)
    workspace.mkdir()
    (workspace / "source.py").write_text("secret-source", encoding="utf-8")

    killer = MagicMock()
    with patch("backend.workers.tasks.StrixSandboxManager.remove_network_for_run"):
        marked = await reconcile_orphaned_runs(
            integration_session,
            now=datetime.now(UTC),
            kill_container=killer,
            workspace_root=tmp_path,
        )

    assert marked == 1
    killer.assert_called_once_with(f"fenix-strix-{run.id}")
    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.FAILED
    assert run.error_message == "WORKER_WATCHDOG_ORPHANED"
    assert run.cleanup_pending is False
    await integration_session.refresh(review)
    assert review.status == PRReviewStatusEnum.ERROR
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_watchdog_retries_pending_cleanup_for_terminal_run(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Cleanup {uuid.uuid4().hex}",
        slug=f"cleanup-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="cleanup.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.FAILED,
        finished_at=datetime.now(UTC) - timedelta(hours=2),
        cleanup_pending=True,
    )
    integration_session.add(run)
    await integration_session.flush()
    workspace = tmp_path / str(run.id)
    workspace.mkdir()
    (workspace / "source.py").write_text("secret-source", encoding="utf-8")

    killer = MagicMock()
    with patch("backend.workers.tasks.StrixSandboxManager.remove_network_for_run"):
        marked = await reconcile_orphaned_runs(
            integration_session,
            now=datetime.now(UTC),
            kill_container=killer,
            workspace_root=tmp_path,
        )

    assert marked == 1
    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.FAILED
    assert run.cleanup_pending is False
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_timeout_transition_is_persisted_for_active_run(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Timeout {uuid.uuid4().hex}",
        slug=f"timeout-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="timeout.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.RUNNING,
        started_at=datetime.now(UTC),
    )
    integration_session.add(run)
    await integration_session.flush()

    changed = await mark_run_timed_out(integration_session, run.id, organization.id)

    assert changed is True
    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.TIMED_OUT
    assert run.error_message == "STRIX_TIMEOUT"


def test_execute_task_marks_timeout_when_sandbox_raises_timeout() -> None:
    run_id = str(uuid.uuid4())
    mark_timeout = AsyncMock()
    with (
        patch(
            "backend.workers.tasks._get_run_organization_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "backend.workers.tasks._execute_pentest_run",
            new=AsyncMock(side_effect=SandboxTimeoutError("timeout")),
        ),
        patch("backend.workers.tasks._mark_timed_out", new=mark_timeout),
    ):
        with pytest.raises(SandboxTimeoutError):
            execute_pentest_run.run(run_id)  # pyright: ignore[reportFunctionMemberAccess]

    mark_timeout.assert_awaited_once_with(None, uuid.UUID(run_id))


@pytest.mark.asyncio
async def test_watchdog_reenqueues_stale_queued_pr_reviews(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Queued {uuid.uuid4().hex}",
        slug=f"queued-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    integration_session.add(repository)
    await integration_session.flush()
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=8,
        pr_title="Queued PR",
        pr_author="alice",
        source_branch="feature/queued",
        target_branch="main",
        commit_sha="b" * 40,
        status=PRReviewStatusEnum.QUEUED,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    integration_session.add(review)
    await integration_session.commit()

    with patch("backend.apps.repositories.tasks.run_pr_security_pipeline.delay") as delay:
        marked = await reconcile_orphaned_runs(
            integration_session,
            now=datetime.now(UTC),
            workspace_root=tmp_path,
        )

    assert marked == 0
    delay.assert_called_once_with(str(review.id))


def test_startup_signal_enqueues_watchdog() -> None:
    with patch("backend.workers.tasks.watchdog_orphaned_runs.delay") as delay:
        schedule_startup_watchdog()

    delay.assert_called_once_with()
