"""Instancia Celery compartida por los workers de Fenix."""

from datetime import timedelta

from celery import Celery

from backend.core.config import settings

celery_app = Celery(
    "mind_guard_fenix",
    broker=settings.celery_redis_url,
    backend=settings.celery_redis_url,
    include=["backend.workers.tasks", "backend.apps.repositories.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    result_expires=3600,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.strix_worker_concurrency,
    task_time_limit=settings.celery_task_time_limit_seconds,
    task_soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "watchdog-orphaned-runs": {
            "task": "pentests.watchdog_orphaned_runs",
            "schedule": timedelta(seconds=settings.strix_watchdog_interval_seconds),
        },
    },
)
