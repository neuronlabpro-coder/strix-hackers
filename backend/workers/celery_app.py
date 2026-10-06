"""Instancia Celery compartida por los workers de Fenix."""

import asyncio
import logging
from datetime import timedelta

from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.config import settings
from backend.core.database import create_database_engine

logger = logging.getLogger(__name__)

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
        # El vigilante de revisiones va **en el mismo periodo** y no en uno propio, y no es
        # estética: las dos ramas que cierran revisiones dependen de que el watchdog de runs
        # haya pasado ya. Con intervalos distintos, la revisión huérfana podría cerrarse antes
        # de que su run se marcara, y el usuario vería dos motivos para un solo fallo.
        #
        # Y va declarado aquí, y no en `backend/apps/repositories/tasks.py`, porque
        # `celery_app.py` es el módulo que **no** importa nada de los workers: si la función
        # de la tarea viviera en este fichero, importar `celery_app` desde cualquier worker
        # arrastraría la definición de la tarea y el grafo de imports dejaría de ser un
        # sentido. La tarea vive con su dominio y aquí solo se nombra.
        "watchdog-orphaned-reviews": {
            "task": "repositories.watchdog_orphaned_reviews",
            # El mismo periodo que el watchdog de runs, y por el mismo motivo: las dos ramas
            # terminan en revisiones y un periodo distinto haría que una se cerrara mientras la
            # otra todavía no ha pasado. No se cuelgan entre sí porque **no comparten filas** —
            # esta solo mira revisiones sin `run_id`, que son las únicas que el otro no ve—, así
            # que el orden entre las dos pasadas no importa: cada una tiene su conjunto.
            #
            # Y no se añade un knob propio para el periodo, porque la pregunta es la misma —«¿cada
            # cuánto se mira si algo se quedó esperando?»— y dos parámetros para lo mismo divergen
            # sin que nada se entere.
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


@celery_app.task(name="repositories.watchdog_orphaned_reviews")
def watchdog_orphaned_reviews() -> int:
    """Cierra revisiones de PR que se quedaron esperando a un worker que no existe.

    ## Por qué la tarea se declara aquí y no en su módulo de dominio

    Porque `celery_app.py` es el único módulo que **no** importa ningún worker, y esta función
    abre su propio motor de base de datos. La lógica vive en
    `backend/apps/repositories/reviews.py`, que no importa Celery y por eso se puede probar sin
    broker; el `import` de la función está **dentro** del cuerpo de la tarea, no arriba del
    módulo, por lo mismo: arriba ataría el arranque de la aplicación al registro de tareas.

    ## Por qué devuelve un recuento y propaga el fallo en vez de tragárselo

    Porque un vigilante que se traga su fallo es indistinguible de uno que no se ejecuta, y el
    log es lo único que permite saber cuál de los dos fue. La siguiente pasada lo intentará
    igual —cerrar revisiones no mejora con reintentos—, así que lo que se pierde sin log es la
    visibilidad, y eso sí es evitable. El recuento es lo que dice si la pasada hizo algo.

    ## Por qué abre su propio motor

    Porque es una tarea programada, no una petición: no hay sesión que reutilizar y su ciclo de
    vida es el suyo. Es el mismo patrón que `_run_watchdog` en `backend/workers/tasks.py`, y
    por el mismo motivo: una tarea de Beat no puede depender de que haya un motor vivo en el
    proceso de la API.
    """

    from backend.apps.repositories.reviews import reconciliar_revisiones_huerfanas

    motor = create_database_engine(settings)
    fabrica = async_sessionmaker[AsyncSession](
        bind=motor,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def _cerrar() -> int:
        try:
            async with fabrica() as sesion:
                return await reconciliar_revisiones_huerfanas(sesion)
        finally:
            await motor.dispose()

    try:
        return asyncio.run(_cerrar())
    except Exception:
        # Se registra **y** se propaga. Un vigilante que traga su fallo es indistinguible de uno
        # que no se ejecuta, y el registro es lo que permite saber cuál de los dos fue. La
        # siguiente pasada lo intentará igual; lo que se pierde es la visibilidad, y eso sí es
        # evitable.
        logger.exception("El vigilante de revisiones de PR no pudo completar su pasada")
        raise
