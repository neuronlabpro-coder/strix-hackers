import backend.workers.tasks  # noqa: F401
from backend.workers.celery_app import celery_app


def test_celery_uses_isolated_redis_database_and_strict_json() -> None:
    configuration = celery_app.conf

    assert str(celery_app.conf.broker_url).endswith("/1")
    assert str(celery_app.conf.result_backend).endswith("/1")
    assert configuration.task_serializer == "json"
    assert configuration.accept_content == ["json"]
    assert configuration.worker_prefetch_multiplier == 1
    assert configuration.worker_concurrency == 1
    assert configuration.task_acks_late is True
    assert configuration.task_reject_on_worker_lost is True
    assert configuration.worker_cancel_long_running_tasks_on_connection_loss is True
    assert configuration.result_expires == 3600
    assert configuration.task_time_limit == 2400
    assert configuration.task_soft_time_limit == 2100
    assert "pentests.ingest_strix_output" in celery_app.tasks
    assert "pentests.execute" in celery_app.tasks
    assert "pentests.watchdog_orphaned_runs" in celery_app.tasks
    assert "repositories.process_git_webhook_event" in celery_app.tasks
    assert "repositories.run_pr_security_pipeline" in celery_app.tasks
    assert celery_app.tasks["repositories.run_pr_security_pipeline"].max_retries == 3
    assert configuration.beat_schedule["watchdog-orphaned-runs"]["task"] == (
        "pentests.watchdog_orphaned_runs"
    )
