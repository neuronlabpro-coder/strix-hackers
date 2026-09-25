import pytest

from backend.apps.repositories.inventory import (
    RemoteRepositoryError,
    normalize_remote_repository,
    webhook_callback_url,
    webhook_subscription_events,
)
from backend.apps.repositories.models import GitProviderEnum
from backend.apps.repositories.validation import (
    GitReferenceError,
    validate_git_branch,
    validate_git_clone_url,
    validate_git_commit_sha,
)

_GITHUB_PAYLOAD = {
    "id": 4242,
    "name": "payments",
    "full_name": "acme/payments",
    "clone_url": "https://github.com/acme/payments.git",
    "default_branch": "trunk",
    "private": True,
}

_GITLAB_PAYLOAD = {
    "id": 77,
    "name": "payments",
    "path_with_namespace": "acme/payments",
    "http_url_to_repo": "https://gitlab.com/acme/payments.git",
    "default_branch": "main",
    "visibility": "internal",
}


def test_normalizes_github_and_gitlab_inventory() -> None:
    github_repository = normalize_remote_repository(GitProviderEnum.GITHUB, _GITHUB_PAYLOAD)
    gitlab_repository = normalize_remote_repository(GitProviderEnum.GITLAB, _GITLAB_PAYLOAD)

    assert github_repository.remote_repo_id == "4242"
    assert github_repository.full_name == "acme/payments"
    assert github_repository.default_branch == "trunk"
    assert github_repository.is_private is True
    assert gitlab_repository.remote_repo_id == "77"
    assert gitlab_repository.default_branch == "main"
    assert gitlab_repository.is_private is True


def test_defaults_missing_branch_to_main() -> None:
    repository = normalize_remote_repository(
        GitProviderEnum.GITHUB,
        {key: value for key, value in _GITHUB_PAYLOAD.items() if key != "default_branch"},
    )

    assert repository.default_branch == "main"


@pytest.mark.parametrize(
    "payload",
    [
        {**_GITHUB_PAYLOAD, "clone_url": "http://github.com/acme/payments.git"},
        {**_GITHUB_PAYLOAD, "clone_url": "https://user:pass@github.com/acme/payments.git"},
        {**_GITHUB_PAYLOAD, "clone_url": "https://evil.example.com/acme/payments.git"},
        {**_GITHUB_PAYLOAD, "id": "not-a-number"},
        {**_GITHUB_PAYLOAD, "full_name": ""},
        {**_GITHUB_PAYLOAD, "full_name": "acme/pay\x00ments"},
        {**_GITHUB_PAYLOAD, "name": "  "},
        {**_GITHUB_PAYLOAD, "default_branch": "main;rm -rf /"},
    ],
)
def test_rejects_unsafe_or_incomplete_provider_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(RemoteRepositoryError):
        normalize_remote_repository(GitProviderEnum.GITHUB, payload)


def test_rejects_providers_without_adapter() -> None:
    with pytest.raises(RemoteRepositoryError):
        normalize_remote_repository(GitProviderEnum.BITBUCKET, _GITHUB_PAYLOAD)


def test_webhook_helpers_use_configured_base_url_and_events() -> None:
    assert webhook_callback_url(GitProviderEnum.GITHUB).endswith("/api/v1/webhooks/git/github")
    assert webhook_callback_url(GitProviderEnum.GITLAB).endswith("/api/v1/webhooks/git/gitlab")
    assert "pull_request" in webhook_subscription_events()
    assert "issue_comment" in webhook_subscription_events()


@pytest.mark.parametrize(
    "branch",
    ["main", "feature/FIX-12", "release-1.2"],
)
def test_accepts_safe_branches(branch: str) -> None:
    assert validate_git_branch(branch) == branch


@pytest.mark.parametrize(
    "branch",
    ["-main", "../etc", "main..dev", "main lock", "main~1", "main^", "a//b", "x.lock", ""],
)
def test_rejects_unsafe_branches(branch: str) -> None:
    with pytest.raises(GitReferenceError):
        validate_git_branch(branch)


def test_commit_sha_must_be_hex_and_normalized() -> None:
    assert validate_git_commit_sha("A" * 40) == "a" * 40
    with pytest.raises(GitReferenceError):
        validate_git_commit_sha("z" * 40)
    with pytest.raises(GitReferenceError):
        validate_git_commit_sha("abc123")


def test_clone_url_requires_https_and_allowlisted_host() -> None:
    assert (
        validate_git_clone_url("https://github.com/acme/payments.git")
        == "https://github.com/acme/payments.git"
    )
    with pytest.raises(GitReferenceError):
        validate_git_clone_url("git@github.com:acme/payments.git")
    with pytest.raises(GitReferenceError):
        validate_git_clone_url("https://unknown-host.example.com/acme/payments.git")
