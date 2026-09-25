import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.pipeline import PRPipelineError
from backend.apps.repositories.tasks import _run_pr_security_pipeline
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.core.crypto import encrypt_secret


async def _create_pipeline_review(
    session: AsyncSession,
) -> tuple[Repository, PullRequestReview, GitCredential]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"PR pipeline {suffix}",
        slug=f"pr-pipeline-{suffix}",
    )
    session.add(organization)
    await session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    session.add(repository)
    await session.flush()
    credential = GitCredential(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        encrypted_access_token=encrypt_secret(
            "github-token",
            organization_id=str(organization.id),
            provider=GitProviderEnum.GITHUB.value,
        ),
    )
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=17,
        pr_title="Change",
        pr_author="alice",
        source_branch="feature/change",
        target_branch="main",
        commit_sha="b" * 40,
        base_sha="f" * 40,
        head_clone_url="https://github.com/acme/app.git",
    )
    session.add_all([credential, review])
    await session.commit()
    return repository, review, credential


class _FakeGitClient:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, str]] = []
        self.comments: list[str] = []

    def set_commit_status(
        self,
        repo_full_name: str,
        sha: str,
        state: str,
        description: str,
        target_url: str,
    ) -> None:
        del description, target_url
        self.statuses.append((repo_full_name, sha, state))

    def post_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> str:
        del repo_full_name, pr_number
        self.comments.append(body)
        return "comment-123"


class _FakeManager:
    def __init__(self, run_id: str, target: str, workspace_root: Path) -> None:
        self.run_id = run_id
        self.target = target
        self.workspace_root = workspace_root
        self.temp_dir: Path | None = None
        self.cleanup_pending = False
        self.output_json = ""

    def setup_workspace(self) -> Path:
        run_dir = self.workspace_root / self.run_id
        (run_dir / "workspace").mkdir(parents=True)
        (run_dir / "output").mkdir()
        self.temp_dir = run_dir
        return run_dir

    def run(self, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["workspace_prepared"] is True
        return SimpleNamespace(
            exit_code=0,
            output_json=self.output_json,
            container_id="container-test",
        )

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("severity", "expected_status", "expected_check", "expected_comment"),
    [
        (SeverityEnum.MEDIUM, PRReviewStatusEnum.PASSED, "success", False),
        (SeverityEnum.CRITICAL, PRReviewStatusEnum.FAILED, "failure", True),
    ],
)
async def test_pr_pipeline_transitions_and_publishes_feedback(
    integration_session: AsyncSession,
    tmp_path: Path,
    severity: SeverityEnum,
    expected_status: PRReviewStatusEnum,
    expected_check: str,
    expected_comment: bool,
) -> None:
    assert integration_session is not None
    repository, review, _credential = await _create_pipeline_review(integration_session)
    review_id = review.id
    client = _FakeGitClient()
    manager_holder: dict[str, _FakeManager] = {}

    def manager_factory(
        run_id: str,
        target: str,
        scan_mode: str,
        target_type: str,
        workspace_root: Path,
    ) -> _FakeManager:
        assert scan_mode == "quick"
        assert target_type == "REPOSITORY"
        manager = _FakeManager(run_id, target, workspace_root)
        manager_holder["manager"] = manager
        return manager

    async def client_builder(
        session: AsyncSession,
        loaded_repository: Repository,
    ) -> _FakeGitClient:
        del session
        assert loaded_repository.id == repository.id
        return client

    async def fake_materializer(
        loaded_repository: Repository,
        loaded_review: PullRequestReview,
        target_dir: Path,
        *,
        credential: GitCredential,
    ) -> list[str]:
        del loaded_repository, credential
        current = await integration_session.get(PullRequestReview, loaded_review.id)
        assert current is not None
        assert current.status == PRReviewStatusEnum.SCANNING
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "changed.py").write_text("print('changed')\n", encoding="utf-8")
        return ["changed.py"]

    # The manager output is installed after the pipeline creates its run, through
    # a small factory wrapper that observes the generated run identifier.
    original_factory = manager_factory

    def factory_with_output(
        run_id: str,
        target: str,
        scan_mode: str,
        target_type: str,
        workspace_root: Path,
    ) -> _FakeManager:
        manager = original_factory(run_id, target, scan_mode, target_type, workspace_root)
        manager.output_json = json.dumps(
            {
                "status": "completed",
                "scan_id": "scan-pr-1",
                "findings": [
                    {
                        "id": "finding-pr-1",
                        "title": "Finding in PR",
                        "description": "A reproducible finding",
                        "severity": severity.value,
                        "cvss_score": 9.8 if severity == SeverityEnum.CRITICAL else 5.0,
                        "affected_target": "changed.py:1",
                        "poc_reproduction_raw": "pytest -q test_security.py",
                        "autofix_patch_diff": "--- a/changed.py\n+++ b/changed.py",
                    }
                ],
            }
        )
        return manager

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    result = await _run_pr_security_pipeline(
        str(review.id),
        session_provider=session_provider,
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
        manager_factory=factory_with_output,  # pyright: ignore[reportArgumentType]
        materializer=fake_materializer,
        workspace_root=tmp_path,
    )

    assert result == expected_status.value
    refreshed_review = await integration_session.get(PullRequestReview, review_id)
    assert refreshed_review is not None
    assert refreshed_review.status == expected_status
    assert refreshed_review.merge_blocked is (expected_status == PRReviewStatusEnum.FAILED)
    assert refreshed_review.comment_id == ("comment-123" if expected_comment else None)
    assert client.statuses[-1][2] == expected_check
    assert client.statuses[0][2] == "pending"
    assert bool(client.comments) is expected_comment
    if expected_comment:
        assert "CRITICAL" in client.comments[0]
        assert "pytest -q test_security.py" in client.comments[0]
        assert "@fenix-team review" not in client.comments[0]

    run_result = await integration_session.execute(
        select(PentestRun).where(PentestRun.id == refreshed_review.run_id)
    )
    run = run_result.scalar_one()
    assert run.status == ScanStatusEnum.COMPLETED
    assert run.scan_mode == ScanModeEnum.QUICK
    findings = list(
        (
            await integration_session.execute(
                select(Vulnerability).where(Vulnerability.run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(findings) == 1
    assert manager_holder["manager"].temp_dir is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pr_pipeline_marks_cleanup_pending_after_materializer_failure(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    _repository, review, _credential = await _create_pipeline_review(integration_session)
    review_id = review.id
    client = _FakeGitClient()

    class FailingManager(_FakeManager):
        def cleanup(self) -> None:
            super().cleanup()
            self.cleanup_pending = True

    def manager_factory(
        run_id: str,
        target: str,
        scan_mode: str,
        target_type: str,
        workspace_root: Path,
    ) -> _FakeManager:
        del scan_mode, target_type
        return FailingManager(run_id, target, workspace_root)

    async def client_builder(
        session: AsyncSession,
        loaded_repository: Repository,
    ) -> _FakeGitClient:
        del session, loaded_repository
        return client

    async def failing_materializer(
        loaded_repository: Repository,
        loaded_review: PullRequestReview,
        target_dir: Path,
        *,
        credential: GitCredential,
    ) -> list[str]:
        del loaded_repository, loaded_review, target_dir, credential
        raise RuntimeError("materialization failed")

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    with pytest.raises(PRPipelineError):
        await _run_pr_security_pipeline(
            str(review_id),
            session_provider=session_provider,
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=failing_materializer,
            workspace_root=tmp_path,
        )

    refreshed_review = await integration_session.get(PullRequestReview, review_id)
    assert refreshed_review is not None
    assert refreshed_review.status == PRReviewStatusEnum.ERROR
    assert refreshed_review.run_id is not None
    run = await integration_session.get(PentestRun, refreshed_review.run_id)
    assert run is not None
    assert run.cleanup_pending is True
