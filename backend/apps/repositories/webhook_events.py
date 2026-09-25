"""Procesamiento de eventos Git después de validar la firma del webhook."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.chatops import (
    NormalizedPullRequestEvent,
    normalize_pull_request_details,
    normalize_pull_request_event,
    parse_review_command,
)
from backend.apps.repositories.clients.base import BaseGitClient, GitClientError
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.pipeline import _default_session_provider
from backend.apps.repositories.services import build_client_for_repository

logger = logging.getLogger(__name__)


class WebhookEventError(RuntimeError):
    """Error de normalización o persistencia de un evento Git."""


class SessionProvider(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


ClientBuilder = Callable[[AsyncSession, Repository], Awaitable[BaseGitClient]]
EnqueueReview = Callable[[str], None]


def _uuid(value: str | None, field_name: str) -> UUID:
    if not value:
        raise WebhookEventError(f"Falta {field_name} en el evento")
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise WebhookEventError(f"{field_name} no es un UUID válido") from error


def _review_values(
    event: NormalizedPullRequestEvent,
) -> tuple[str, str, str, str, str, str | None, str | None]:
    if (
        not event.pr_number
        or not event.title
        or not event.author
        or not event.source_branch
        or not event.target_branch
        or not event.commit_sha
    ):
        raise WebhookEventError("El evento PR no contiene los datos requeridos")
    return (
        event.title,
        event.author,
        event.source_branch,
        event.target_branch,
        event.commit_sha.lower(),
        event.head_clone_url,
        event.base_sha.lower() if event.base_sha else None,
    )


async def _find_repository(
    session: AsyncSession,
    repository_id: UUID,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> Repository | None:
    result = await session.execute(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.organization_id == organization_id,
            Repository.provider == provider,
        )
    )
    return result.scalar_one_or_none()


async def _upsert_review(
    session: AsyncSession,
    repository: Repository,
    event: NormalizedPullRequestEvent,
    *,
    force: bool = False,
) -> tuple[PullRequestReview, bool]:
    (
        title,
        author,
        source_branch,
        target_branch,
        commit_sha,
        head_clone_url,
        base_sha,
    ) = _review_values(event)
    result = await session.execute(
        select(PullRequestReview)
        .where(
            PullRequestReview.organization_id == repository.organization_id,
            PullRequestReview.repository_id == repository.id,
            PullRequestReview.pr_number == event.pr_number,
            PullRequestReview.commit_sha == commit_sha,
            PullRequestReview.base_sha == base_sha,
        )
        .with_for_update()
    )
    review = result.scalar_one_or_none()
    if review is not None and review.status == PRReviewStatusEnum.SCANNING:
        return review, False
    if (
        review is not None
        and not force
        and review.status != PRReviewStatusEnum.QUEUED
        and not (
            review.status == PRReviewStatusEnum.ERROR
            and review.run_id is None
        )
    ):
        return review, False
    if review is None:
        previous_result = await session.execute(
            select(PullRequestReview)
            .where(
                PullRequestReview.organization_id == repository.organization_id,
                PullRequestReview.repository_id == repository.id,
                PullRequestReview.pr_number == event.pr_number,
                PullRequestReview.comment_id.is_not(None),
            )
            .order_by(PullRequestReview.created_at.desc())
            .limit(1)
        )
        previous = previous_result.scalar_one_or_none()
        review = PullRequestReview(
            organization_id=repository.organization_id,
            repository_id=repository.id,
            pr_number=event.pr_number,
            pr_title=title,
            pr_author=author,
            source_branch=source_branch,
            target_branch=target_branch,
            commit_sha=commit_sha,
            head_clone_url=head_clone_url,
            base_sha=base_sha,
            comment_id=previous.comment_id if previous is not None else None,
        )
        session.add(review)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            result = await session.execute(
                select(PullRequestReview).where(
                    PullRequestReview.organization_id == repository.organization_id,
                    PullRequestReview.repository_id == repository.id,
                    PullRequestReview.pr_number == event.pr_number,
                    PullRequestReview.commit_sha == commit_sha,
                    PullRequestReview.base_sha == base_sha,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                raise WebhookEventError("No se pudo idempotentar la revisión PR") from error
            review = existing
    review.pr_title = title
    review.pr_author = author
    review.source_branch = source_branch
    review.target_branch = target_branch
    review.head_clone_url = head_clone_url
    review.base_sha = base_sha
    review.status = PRReviewStatusEnum.QUEUED
    review.run_id = None
    review.issues_caught_critical = 0
    review.issues_caught_high = 0
    review.merge_blocked = False
    review.finished_at = None
    await session.commit()
    return review, True


async def _process_git_webhook_event(
    provider: str,
    event_type: str,
    payload: dict[str, object],
    repository_id: str | None = None,
    organization_id: str | None = None,
    delivery_id: str | None = None,
    *,
    session_provider: SessionProvider = _default_session_provider,
    client_builder: ClientBuilder = build_client_for_repository,
    enqueue_review: EnqueueReview,
) -> dict[str, object]:
    del delivery_id
    try:
        selected_provider = GitProviderEnum(provider.upper())
    except ValueError as error:
        raise WebhookEventError("Proveedor de webhook no soportado") from error
    event = normalize_pull_request_event(selected_provider, event_type, payload)
    if event is None:
        return {"accepted": True, "processed": False}
    repository_uuid = _uuid(repository_id, "repository_id")
    organization_uuid = _uuid(organization_id, "organization_id")
    async with session_provider() as session:
        repository = await _find_repository(
            session,
            repository_uuid,
            organization_uuid,
            selected_provider,
        )
        if repository is None or not repository.is_active or not repository.pr_reviews_enabled:
            return {"accepted": True, "processed": False}
        if event.is_comment:
            comment_body = event.comment_body
            comment_author = event.comment_author
            if not comment_body or not comment_author:
                return {"accepted": True, "processed": False}
            if not parse_review_command(comment_body):
                return {"accepted": True, "processed": False}
            client = await client_builder(session, repository)
            try:
                allowed = await asyncio.to_thread(
                    client.has_write_access,
                    repository.full_name,
                    comment_author,
                )
                if not allowed:
                    return {"accepted": True, "processed": False}
                details = await asyncio.to_thread(
                    client.get_pull_request,
                    repository.full_name,
                    event.pr_number,
                )
            except GitClientError as error:
                if error.status_code in {401, 403, 404}:
                    logger.warning(
                        "No se pudo validar permisos ChatOps para %s",
                        repository.id,
                    )
                    return {"accepted": True, "processed": False}
                raise WebhookEventError("Git no pudo validar el comando ChatOps") from error
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    await asyncio.to_thread(close)
            current = normalize_pull_request_details(selected_provider, details)
            if current is None:
                return {"accepted": True, "processed": False}
            event = NormalizedPullRequestEvent(
                provider=current.provider,
                pr_number=current.pr_number,
                title=current.title,
                author=current.author,
                source_branch=current.source_branch,
                target_branch=current.target_branch,
                commit_sha=current.commit_sha,
                is_comment=True,
                head_clone_url=current.head_clone_url,
                base_sha=current.base_sha,
                comment_body=comment_body,
                comment_author=comment_author,
                action=event.action,
            )
            review, should_enqueue = await _upsert_review(
                session,
                repository,
                event,
                force=True,
            )
        else:
            if event.base_sha is None or (
                selected_provider == GitProviderEnum.GITLAB
                and event.head_clone_url is None
            ):
                enrichment_client = await client_builder(session, repository)
                try:
                    details = await asyncio.to_thread(
                        enrichment_client.get_pull_request,
                        repository.full_name,
                        event.pr_number,
                    )
                except GitClientError as error:
                    raise WebhookEventError(
                        "No se pudo confirmar el head actual del PR"
                    ) from error
                finally:
                    close = getattr(enrichment_client, "close", None)
                    if callable(close):
                        await asyncio.to_thread(close)
                current = normalize_pull_request_details(selected_provider, details)
                if current is not None:
                    event = current
            review, should_enqueue = await _upsert_review(session, repository, event)
        if should_enqueue:
            try:
                enqueue_review(str(review.id))
            except Exception as error:
                review.status = PRReviewStatusEnum.ERROR
                review.finished_at = datetime.now(UTC)
                await session.commit()
                raise WebhookEventError("No se pudo encolar la revisión PR") from error
        return {
            "accepted": True,
            "processed": True,
            "review_id": str(review.id),
            "enqueued": should_enqueue,
        }
