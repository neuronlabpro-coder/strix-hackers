import os
import subprocess
import uuid
from pathlib import Path

import pytest

from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.workspace import (
    WorkspaceMaterializationError,
    materialize_pr_workspace,
)
from backend.core.crypto import encrypt_secret


def _repository_and_review() -> tuple[Repository, PullRequestReview, GitCredential]:
    organization_id = uuid.uuid4()
    repository = Repository(
        id=uuid.uuid4(),
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="42",
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    review = PullRequestReview(
        id=uuid.uuid4(),
        organization_id=organization_id,
        repository_id=repository.id,
        pr_number=7,
        pr_title="Safe change",
        pr_author="alice",
        source_branch="feature/safe-change",
        target_branch="main",
        commit_sha="a" * 40,
        base_sha="f" * 40,
        head_clone_url="https://github.com/acme/app.git",
    )
    credential = GitCredential(
        id=uuid.uuid4(),
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        encrypted_access_token=encrypt_secret(
            "github-token",
            organization_id=str(organization_id),
            provider=GitProviderEnum.GITHUB.value,
        ),
    )
    return repository, review, credential


@pytest.mark.asyncio
async def test_materializer_uses_shallow_single_branch_clone_without_token_in_argv(
    tmp_path: Path,
) -> None:
    repository, review, credential = _repository_and_review()
    target_dir = tmp_path / "run" / "target"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_git(
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        input_data: bytes | None,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, input_data
        calls.append((args, env))
        if args[0] == "clone":
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "src").mkdir()
            (target_dir / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        if args[:2] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, review.commit_sha.encode(), b"")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, b"src/app.py\0", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    modified = await materialize_pr_workspace(
        repository,
        review,
        target_dir,
        credential=credential,
        command_runner=fake_git,
    )

    assert modified == ["src/app.py"]
    clone_args, clone_env = calls[0]
    assert clone_args[:7] == [
        "clone",
        "--depth=1",
        "--single-branch",
        "--no-tags",
        "--branch",
        "feature/safe-change",
        "--",
    ]
    assert "github-token" not in " ".join(clone_args)
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "github-token" in clone_env["GIT_CONFIG_VALUE_0"]
    assert not (target_dir / ".git" / "config").exists()


@pytest.mark.asyncio
async def test_materializer_recovers_exact_commit_when_branch_moved(
    tmp_path: Path,
) -> None:
    repository, review, credential = _repository_and_review()
    review.head_clone_url = "https://github.com/alice/app.git"
    target_dir = tmp_path / "run" / "target"
    calls: list[list[str]] = []
    rev_call_count = 0

    def fake_git(
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        input_data: bytes | None,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal rev_call_count
        del cwd, env, input_data
        calls.append(args)
        if args[0] == "clone":
            target_dir.mkdir(parents=True, exist_ok=True)
        if args[:2] == ["rev-parse", "HEAD"]:
            rev_call_count += 1
            sha = "9" * 40 if rev_call_count == 1 else review.commit_sha
            return subprocess.CompletedProcess(args, 0, sha.encode(), b"")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, b"src/app.py\0", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    modified = await materialize_pr_workspace(
        repository,
        review,
        target_dir,
        credential=credential,
        command_runner=fake_git,
    )

    assert modified == ["src/app.py"]
    assert calls[0][-2] == "https://github.com/alice/app.git"
    assert ["fetch", "--depth=1", "--no-tags", "origin", review.commit_sha] in calls
    assert ["checkout", "--detach", review.commit_sha] in calls
    diff_call = next(call for call in calls if call[0] == "diff")
    assert "..." not in diff_call


@pytest.mark.asyncio
async def test_materializer_rejects_diff_path_traversal_and_removes_workspace(
    tmp_path: Path,
) -> None:
    repository, review, credential = _repository_and_review()
    target_dir = tmp_path / "run" / "target"

    def fake_git(
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        input_data: bytes | None,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, env, input_data
        if args[0] == "clone":
            target_dir.mkdir(parents=True, exist_ok=True)
        if args[:2] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, review.commit_sha.encode(), b"")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, b"../outside.txt\0", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with pytest.raises(WorkspaceMaterializationError):
        await materialize_pr_workspace(
            repository,
            review,
            target_dir,
            credential=credential,
            command_runner=fake_git,
        )

    assert not target_dir.exists()


@pytest.mark.asyncio
async def test_materializer_requires_immutable_base_sha(tmp_path: Path) -> None:
    repository, review, credential = _repository_and_review()
    review.base_sha = None

    with pytest.raises(WorkspaceMaterializationError):
        await materialize_pr_workspace(
            repository,
            review,
            tmp_path / "target",
            credential=credential,
            command_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, b"", b""),
        )


@pytest.mark.asyncio
async def test_materializer_rejects_branch_option_injection_before_git(
    tmp_path: Path,
) -> None:
    repository, review, credential = _repository_and_review()
    review.source_branch = "--upload-pack=evil"

    with pytest.raises(WorkspaceMaterializationError):
        await materialize_pr_workspace(
            repository,
            review,
            tmp_path / "target",
            credential=credential,
            command_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, b"", b""),
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink permissions vary on Windows")
@pytest.mark.asyncio
async def test_materializer_rejects_symlink_escaping_workspace(tmp_path: Path) -> None:
    repository, review, credential = _repository_and_review()
    target_dir = tmp_path / "run" / "target"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    def fake_git(
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        input_data: bytes | None,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, env, input_data
        if args[0] == "clone":
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "escape").symlink_to(outside)
        if args[:2] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, review.commit_sha.encode(), b"")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, b"escape\0", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with pytest.raises(WorkspaceMaterializationError):
        await materialize_pr_workspace(
            repository,
            review,
            target_dir,
            credential=credential,
            command_runner=fake_git,
        )
