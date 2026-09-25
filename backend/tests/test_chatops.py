import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.repositories.chatops import (
    normalize_pull_request_event,
    parse_review_command,
)
from backend.apps.repositories.models import GitProviderEnum, PullRequestReview, Repository
from backend.apps.repositories.webhook_events import (
    WebhookEventError,
    _process_git_webhook_event,
)


def test_parse_review_command_accepts_configured_alias_and_ignores_unrelated_text() -> None:
    assert parse_review_command("Please run @Fenix-Team   review now")
    assert parse_review_command("@strix review", commands=("@strix review",))
    assert not parse_review_command("the reviewer asked for a review")
    assert not parse_review_command("@fenix-team deploy")


def test_normalize_github_pull_request_event_extracts_review_context() -> None:
    event = normalize_pull_request_event(
        GitProviderEnum.GITHUB,
        "pull_request",
        {
            "action": "synchronize",
            "pull_request": {
                "number": 12,
                "title": "Fix auth",
                "user": {"login": "alice"},
                "head": {"ref": "feature/auth", "sha": "d" * 40},
                "base": {"ref": "main"},
            },
        },
    )

    assert event is not None
    assert event.pr_number == 12
    assert event.source_branch == "feature/auth"
    assert event.target_branch == "main"
    assert event.commit_sha == "d" * 40
    assert event.base_sha is None
    assert event.head_clone_url is None
    assert event.author == "alice"
    assert event.is_comment is False


def test_normalize_github_comment_event_extracts_command_context() -> None:
    event = normalize_pull_request_event(
        GitProviderEnum.GITHUB,
        "issue_comment",
        {
            "action": "created",
            "issue": {
                "number": 12,
                "title": "Fix auth",
                "pull_request": {"url": "https://api.github.com/pulls/12"},
                "user": {"login": "alice"},
            },
            "comment": {
                "body": "@fenix-team review",
                "user": {"login": "reviewer"},
            },
            "repository": {},
        },
    )

    assert event is not None
    assert event.is_comment is True
    assert event.author == "reviewer"
    assert event.comment_body == "@fenix-team review"
    assert event.pr_number == 12


async def _create_chatops_repository(session: AsyncSession) -> Repository:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"ChatOps {suffix}", slug=f"chatops-{suffix}")
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
    await session.commit()
    return repository


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_pull_request_event_creates_queued_review(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await _create_chatops_repository(integration_session)
    enqueued: list[str] = []

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    async def client_builder(session: AsyncSession, loaded_repository: Repository) -> object:
        del session, loaded_repository
        return object()

    result = await _process_git_webhook_event(
        "GITHUB",
        "pull_request",
        {
            "action": "opened",
            "pull_request": {
                "number": 22,
                "title": "New change",
                "user": {"login": "alice"},
                "head": {
                    "ref": "feature/new",
                    "sha": "e" * 40,
                    "repo": {"clone_url": "https://github.com/acme/app.git"},
                },
                "base": {"ref": "main", "sha": "f" * 40},
            },
        },
        repository_id=str(repository.id),
        organization_id=str(repository.organization_id),
        session_provider=session_provider,
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
        enqueue_review=enqueued.append,
    )

    assert result["enqueued"] is True
    assert len(enqueued) == 1
    review = await integration_session.get(PullRequestReview, uuid.UUID(enqueued[0]))
    assert review is not None
    assert review.status.value == "QUEUED"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_event_retry_recovers_enqueue_failure(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await _create_chatops_repository(integration_session)
    attempts = 0
    enqueued: list[str] = []

    def enqueue(review_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("broker unavailable")
        enqueued.append(review_id)

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    arguments = {
        "provider": "GITHUB",
        "event_type": "pull_request",
        "payload": {
            "action": "opened",
            "pull_request": {
                "number": 44,
                "title": "Retry me",
                "user": {"login": "alice"},
                "head": {
                    "ref": "feature/retry",
                    "sha": "3" * 40,
                    "repo": {"clone_url": "https://github.com/acme/app.git"},
                },
                "base": {"ref": "main", "sha": "4" * 40},
            },
        },
        "repository_id": str(repository.id),
        "organization_id": str(repository.organization_id),
        "session_provider": session_provider,
        "enqueue_review": enqueue,
    }

    with pytest.raises(WebhookEventError):
        await _process_git_webhook_event(**arguments)  # pyright: ignore[reportArgumentType]

    result = await _process_git_webhook_event(**arguments)  # pyright: ignore[reportArgumentType]

    assert result["enqueued"] is True
    assert len(enqueued) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chatops_comment_requires_write_permission_before_enqueue(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await _create_chatops_repository(integration_session)
    review = PullRequestReview(
        organization_id=repository.organization_id,
        repository_id=repository.id,
        pr_number=22,
        pr_title="Existing change",
        pr_author="alice",
        source_branch="feature/new",
        target_branch="main",
        commit_sha="e" * 40,
    )
    integration_session.add(review)
    await integration_session.commit()
    enqueued: list[str] = []

    class Client:
        def has_write_access(self, repo_full_name: str, username: str) -> bool:
            del repo_full_name, username
            return False

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    async def client_builder(session: AsyncSession, loaded_repository: Repository) -> Client:
        del session, loaded_repository
        return Client()

    result = await _process_git_webhook_event(
        "GITHUB",
        "issue_comment",
        {
            "action": "created",
            "issue": {
                "number": 22,
                "title": "Existing change",
                "pull_request": {"url": "https://api.github.com/pulls/22"},
            },
            "comment": {
                "body": "@fenix-team review",
                "user": {"login": "reader"},
            },
        },
        repository_id=str(repository.id),
        organization_id=str(repository.organization_id),
        session_provider=session_provider,
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
        enqueue_review=enqueued.append,
    )

    assert result["processed"] is False
    assert enqueued == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chatops_comment_uses_current_provider_head(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    repository = await _create_chatops_repository(integration_session)
    enqueued: list[str] = []

    class Client:
        def has_write_access(self, repo_full_name: str, username: str) -> bool:
            del repo_full_name, username
            return True

        def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, object]:
            del repo_full_name
            assert pr_number == 31
            return {
                "number": 31,
                "title": "Current head",
                "user": {"login": "alice"},
                "head": {
                    "ref": "feature/current",
                    "sha": "1" * 40,
                    "repo": {"clone_url": "https://github.com/alice/app.git"},
                },
                "base": {"ref": "main", "sha": "2" * 40},
            }

    @asynccontextmanager
    async def session_provider():
        yield integration_session

    async def client_builder(session: AsyncSession, loaded_repository: Repository) -> Client:
        del session, loaded_repository
        return Client()

    result = await _process_git_webhook_event(
        "GITHUB",
        "issue_comment",
        {
            "action": "created",
            "issue": {
                "number": 31,
                "pull_request": {"url": "https://api.github.com/pulls/31"},
            },
            "comment": {
                "body": "@fenix-team review",
                "user": {"login": "reviewer"},
            },
        },
        repository_id=str(repository.id),
        organization_id=str(repository.organization_id),
        session_provider=session_provider,
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
        enqueue_review=enqueued.append,
    )

    assert result["processed"] is True
    review = await integration_session.get(PullRequestReview, uuid.UUID(enqueued[0]))
    assert review is not None
    assert review.commit_sha == "1" * 40
    assert review.head_clone_url == "https://github.com/alice/app.git"
    assert review.base_sha == "2" * 40
