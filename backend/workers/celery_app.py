"""Instancia Celery compartida por los workers de Fenix."""

from datetime import timedelta

from celery import Celery

from backend.core.config import settings

celery_app = Celery(
    "mind_guard_fenix",
    broker=settings.celery_redis_url,
    backend=settings.celery_redis_url,
    include=[
        "backend.workers.tasks",
        "backend.apps.repositories.tasks",
        "backend.apps.webhooks.tasks",
        "backend.apps.assets.tasks",
    ],
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
        "sync-cve-catalog": {
            "task": "cve.sync_catalog",
            "schedule": timedelta(hours=settings.cve_sync_interval_hours),
        },
        # Renueva las credenciales OAuth de Git antes de que caduquen, en un proceso sin trabajo de
        # usuario alrededor. El intervalo tiene que ser menor que la ventana, y la configuración
        # lo rechaza si no lo es; se deja la comprobación en un solo sitio porque dos comparaciones
        # en dos ficheros divergen sin que ninguna prueba se entere.
        "refresh-expiring-git-credentials": {
            "task": "repositories.refresh_expiring_git_credentials",
            "schedule": timedelta(
                seconds=settings.git_token_proactive_refresh_interval_seconds
            ),
        },
    },
    # La tarea de CVE se registra en este módulo para que Celery pueda descubrirla
    # por nombre; sin el import, `cve.sync_catalog` no existiría en el worker.
    imports=("backend.workers.tasks", "backend.apps.cve_database.feeds"),
)
