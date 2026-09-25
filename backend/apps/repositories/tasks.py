"""Tareas asíncronas de integración Git."""

from __future__ import annotations

import asyncio
import logging

from backend.apps.repositories.clients.base import GitRateLimitError, GitServerError
from backend.apps.repositories.pipeline import _run_pr_security_pipeline
from backend.apps.repositories.webhook_events import (
    WebhookEventError,
    _process_git_webhook_event,
)
from backend.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="repositories.run_pr_security_pipeline",
    autoretry_for=(GitRateLimitError, GitServerError),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def run_pr_security_pipeline(self, review_id: str) -> str:
    """Ejecuta el scan rápido de un PR y publica su veredicto."""

    return asyncio.run(
        _run_pr_security_pipeline(
            review_id,
            retry_failed=self.request.retries > 0,
            celery_task_id=self.request.id,
        )
    )


def _enqueue_pr_review(review_id: str) -> None:
    run_pr_security_pipeline.delay(review_id)  # pyright: ignore[reportFunctionMemberAccess]


@celery_app.task(
    name="repositories.process_git_webhook_event",
    autoretry_for=(WebhookEventError,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=5,
)
def process_git_webhook_event(
    provider: str,
    event_type: str,
    payload: dict[str, object],
    repository_id: str | None = None,
    organization_id: str | None = None,
    delivery_id: str | None = None,
) -> dict[str, object]:
    """Normaliza el evento y encola el pipeline cuando corresponde."""

    logger.info(
        "Webhook Git procesado: provider=%s event=%s repository=%s organization=%s delivery=%s",
        provider,
        event_type,
        repository_id,
        organization_id,
        delivery_id,
    )
    return asyncio.run(
        _process_git_webhook_event(
            provider,
            event_type,
            payload,
            repository_id,
            organization_id,
            delivery_id,
            enqueue_review=_enqueue_pr_review,
        )
    )
