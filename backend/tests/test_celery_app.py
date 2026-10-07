import backend.workers.tasks  # noqa: F401
from backend.core.config import settings
from backend.workers.celery_app import celery_app


def test_celery_uses_isolated_redis_database_and_strict_json() -> None:
    """La cola usa **una base de Redis distinta** de la de la aplicación, y solo JSON.

    ## Por qué el número sale de `settings` y no está escrito aquí

    Porque `CELERY_REDIS_DB` es configuración del despliegue —R1— y el `.env` de este proyecto de
    desarrollo la tiene en `2`. Escribir `/1` en la prueba convertía el gate en unaquinterna: el
    test pasaba o caía según lo que el desarrollador tuviera escrito en su fichero, sin que nada
    del código hubiera cambiado. Un test que lee el entorno de quien lo ejecuta y falla según su
    `.env` no vigila el código: vigila la máquina.

    Lo que sí se afirma, y es lo que el número significaba, es lo que no depende de nadie:
    `celery_redis_db` es distinto de `redis_db`, y el broker y el backend de resultados apuntan a
    la base de Celery y no a la de la aplicación.
    """

    configuration = celery_app.conf

    assert settings.celery_redis_db != settings.redis_db, (
        "la cola de Celery tiene que vivir en su propia base de Redis: compartirla con el cache "
        "de la aplicacion hace que un FLUSH de uno vacie el otro"
    )
    assert str(celery_app.conf.broker_url) == settings.celery_redis_url
    assert str(celery_app.conf.result_backend) == settings.celery_redis_url
    assert str(celery_app.conf.broker_url).endswith(f"/{settings.celery_redis_db}")
    # Los literales que quedan son los que **no** vienen de configuración: los avisos de
    # seguridad y los de forma de cola. Los que sí vienen de `settings` se comparan contra él,
    # por el mismo motivo que el número de la base: un gate que falla según el `.env` de quien
    # lo ejecuta no vigila el código.
    assert configuration.task_serializer == "json"
    assert configuration.result_serializer == "json"
    assert configuration.accept_content == ["json"]
    assert configuration.worker_prefetch_multiplier == 1
    assert configuration.worker_concurrency == settings.strix_worker_concurrency
    assert configuration.task_acks_late is True
    assert configuration.task_reject_on_worker_lost is True
    assert configuration.worker_cancel_long_running_tasks_on_connection_loss is True
    assert configuration.result_expires == 3600
    assert configuration.task_time_limit == settings.celery_task_time_limit_seconds
    assert configuration.task_soft_time_limit == settings.celery_task_soft_time_limit_seconds
    # Y el suelo del muro de Celery tiene que seguir por encima del tope duro del motor: si no,
    # Celery mata la tarea antes de que el runner mate su propio árbol de procesos.
    assert configuration.task_soft_time_limit > settings.strix_hard_timeout_seconds
    assert "pentests.ingest_strix_output" in celery_app.tasks
    assert "pentests.execute" in celery_app.tasks
    assert "pentests.watchdog_orphaned_runs" in celery_app.tasks
    assert "repositories.process_git_webhook_event" in celery_app.tasks
    assert "repositories.run_pr_security_pipeline" in celery_app.tasks
    assert celery_app.tasks["repositories.run_pr_security_pipeline"].max_retries == 3
    assert configuration.beat_schedule["watchdog-orphaned-runs"]["task"] == (
        "pentests.watchdog_orphaned_runs"
    )
