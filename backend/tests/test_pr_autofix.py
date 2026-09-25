import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.autofix import (
    AutofixError,
    _create_autofix_branch_and_pr,
    validate_autofix_patch,
)
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.core.crypto import encrypt_secret


async def _create_autofix_fixture(
    session: AsyncSession,
) -> tuple[Repository, PullRequestReview, Vulnerability, GitCredential]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Autofix {suffix}", slug=f"autofix-{suffix}")
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
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier="acme/app#PR-3",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.COMPLETED,
    )
    session.add(run)
    await session.flush()
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        run_id=run.id,
        pr_number=3,
        pr_title="Fix me",
        pr_author="alice",
        source_branch="feature/fix-me",
        target_branch="main",
        commit_sha="c" * 40,
    )
    vulnerability = Vulnerability(
        organization_id=organization.id,
        run_id=run.id,
        source_finding_id="finding-fix-1",
        title="Command injection",
        description="Untrusted input reaches a shell",
        severity=SeverityEnum.CRITICAL,
        cvss_score=9.8,
        affected_target="app.py:10",
        poc_reproduction_raw="python exploit.py",
        autofix_patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+safe\n",
    )
    credential = GitCredential(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        encrypted_access_token=encrypt_secret(
            "github-token",
            organization_id=str(organization.id),
            provider=GitProviderEnum.GITHUB.value,
        ),
    )
    session.add_all([review, vulnerability, credential])
    await session.commit()
    return repository, review, vulnerability, credential


@pytest.mark.asyncio
@pytest.mark.integration
async def test_autofix_creates_short_branch_and_uses_validated_patch(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository, review, vulnerability, _credential = await _create_autofix_fixture(
        integration_session
    )
    calls: list[tuple[str, str, str, str, str]] = []

    class Client:
        def create_autofix_branch_and_pr(
            self,
            repo_full_name: str,
            base_branch: str,
            branch_name: str,
            patch_diff: str,
            title: str,
        ) -> str:
            calls.append((repo_full_name, base_branch, branch_name, patch_diff, title))
            return "https://github.com/acme/app/pull/99"

    async def client_builder(session: AsyncSession, loaded_repository: Repository) -> Client:
        del session
        assert loaded_repository.id == repository.id
        return Client()

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    result = await _create_autofix_branch_and_pr(
        str(review.id),
        str(vulnerability.id),
        session_provider=session_provider,
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
    )

    assert result == "https://github.com/acme/app/pull/99"
    assert calls == [
        (
            "acme/app",
            "main",
            f"fenix/fix-{vulnerability.id.hex[:8]}",
            vulnerability.autofix_patch_diff,
            "[Fenix Security Fix] Command injection",
        )
    ]


def test_autofix_patch_validation_rejects_empty_or_traversal_patch() -> None:
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+safe\n"
    assert validate_autofix_patch(patch) == patch
    with pytest.raises(AutofixError):
        validate_autofix_patch("")
    with pytest.raises(AutofixError):
        validate_autofix_patch("--- a/../outside\n+++ b/../outside\n")
    with pytest.raises(AutofixError):
        validate_autofix_patch("--- a/app.py\n+++ b/app.py\n\0")
