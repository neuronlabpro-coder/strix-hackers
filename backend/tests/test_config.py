from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.core.config import ENV_FILE, Settings


def build_environment_values() -> dict[str, str | int | float | bool | None]:
    return {
        "environment": "development",
        "debug": True,
        "secret_key": "clave-de-prueba-suficientemente-larga-1234567890",
        "db_host": "100.89.59.70",
        "db_port": 5433,
        "db_name": "fenix_team_db",
        "db_user": "fenix_admin",
        "db_password": "clave-postgresql-de-prueba",
        "database_url": (
            "postgresql+asyncpg://fenix_admin:clave-postgresql-de-prueba"
            "@100.89.59.70:5433/fenix_team_db"
        ),
        "db_pool_size": 10,
        "db_max_overflow": 20,
        "db_pool_recycle_seconds": 3600,
        "db_connect_timeout_seconds": 10,
        "redis_host": "100.89.59.70",
        "redis_port": 6380,
        "redis_password": "clave-redis-de-prueba",
        "redis_url": "redis://:clave-redis-de-prueba@100.89.59.70:6380/0",
        "redis_db": 0,
        "celery_redis_db": 1,
        "redis_socket_timeout_seconds": 10,
        "git_encryption_key": "dGVzdC1rZXktMzItYnl0ZXMtMDAwMDAwMDAwMDBBQkM",
        "git_webhook_max_body_bytes": 2_000_000,
        "git_webhook_rate_limit": 120,
        "git_webhook_rate_window_seconds": 60,
        "git_webhook_replay_ttl_seconds": 86_400,
        "git_api_timeout_seconds": 10,
        "jwt_algorithm": "HS256",
        "access_token_expire_minutes": 60,
        "invitation_expire_days": 7,
        "password_min_length": 12,
        "password_max_length": 128,
        "bcrypt_rounds": 12,
        "auth_login_rate_limit": 5,
        "auth_login_rate_window_seconds": 60,
        "auth_register_rate_limit": 3,
        "auth_register_rate_window_seconds": 60,
        "organization_create_rate_limit": 10,
        "organization_create_rate_window_seconds": 60,
        "invitation_rate_limit": 10,
        "invitation_rate_window_seconds": 3600,
        "pentest_create_rate_limit": 5,
        "pentest_create_rate_window_seconds": 60,
        "celery_task_time_limit_seconds": 2400,
        "celery_task_soft_time_limit_seconds": 2100,
        "strix_max_output_bytes": 10_000_000,
        "strix_max_findings": 10_000,
        "strix_max_description_chars": 100_000,
        "strix_max_poc_chars": 1_000_000,
        "strix_max_autofix_chars": 2_000_000,
        "strix_sandbox_image": "ghcr.io/usestrix/strix-sandbox:latest",
        "strix_workspace_root": "/tmp/fenix_workspaces",  # noqa: S108
        "strix_network_prefix": "strix_net",
        "strix_memory_limit": "4g",
        "strix_cpu_limit": 2.0,
        "strix_pids_limit": 256,
        "strix_worker_concurrency": 1,
        "strix_hard_timeout_seconds": 1800,
        "strix_soft_timeout_seconds": 1500,
        "strix_watchdog_interval_seconds": 300,
        "strix_watchdog_stale_after_seconds": 1860,
        "default_strix_llm": "openrouter/test-model",
        "llm_api_key": "clave-llm-de-prueba",
        "llm_api_base": "https://api.example.com/v1",
        "email_verification_ttl_minutes": 1440,
        "email_verification_delivery_mode": "development",
        "email_verification_from": "no-reply@example.com",
        "frontend_base_url": "http://localhost:5173",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_username": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "smtp_timeout_seconds": 10,
    }


def test_env_file_is_resolved_from_project_root() -> None:
    assert ENV_FILE == Path(__file__).resolve().parents[2] / ".env"


def test_settings_loads_infrastructure_values_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in build_environment_values().items()),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]

    assert settings.environment == "development"
    assert settings.db_host == "100.89.59.70"
    assert settings.db_port == 5433
    assert settings.redis_host == "100.89.59.70"
    assert settings.redis_port == 6380
    assert settings.celery_redis_url.endswith("/1")
    assert settings.git_encryption_key_bytes == b"test-key-32-bytes-00000000000ABC"


def test_settings_rejects_git_encryption_key_without_exact_aes_length() -> None:
    values = build_environment_values()
    values["git_encryption_key"] = "a" * 33

    with pytest.raises(ValidationError, match="32 bytes"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    "example_key",
    [
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "0123456789abcdef0123456789abcdef",
    ],
)
def test_settings_rejects_public_example_git_key_in_production(example_key: str) -> None:
    values = build_environment_values()
    values["environment"] = "production"
    values["debug"] = False
    values["git_encryption_key"] = example_key

    with pytest.raises(ValidationError, match="ejemplo"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_database_url_that_does_not_match_components() -> None:
    values = build_environment_values()
    values["database_url"] = (
        "postgresql+asyncpg://fenix_admin:clave-postgresql-de-prueba"
        "@100.89.59.70:5999/fenix_team_db"
    )

    with pytest.raises(ValidationError, match="DATABASE_URL no coincide"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_debug_mode_in_production() -> None:
    values = build_environment_values()
    values["environment"] = "production"
    values["debug"] = True

    with pytest.raises(ValidationError, match="DEBUG"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_development_email_delivery_in_production() -> None:
    values = build_environment_values()
    values["environment"] = "production"
    values["debug"] = False
    values["email_verification_delivery_mode"] = "development"

    with pytest.raises(ValidationError, match="smtp"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_insecure_production_smtp_transport() -> None:
    values = build_environment_values()
    values["environment"] = "production"
    values["debug"] = False
    values["email_verification_delivery_mode"] = "smtp"
    values["smtp_host"] = "smtp.example.com"
    values["frontend_base_url"] = "http://app.example.com"

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_incomplete_smtp_credentials() -> None:
    values = build_environment_values()
    values["email_verification_delivery_mode"] = "smtp"
    values["smtp_host"] = "smtp.example.com"
    values["smtp_username"] = "smtp-user"
    values["smtp_password"] = None

    with pytest.raises(ValidationError, match="credenciales SMTP"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_shared_redis_database_for_celery() -> None:
    values = build_environment_values()
    values["celery_redis_db"] = values["redis_db"]

    with pytest.raises(ValidationError, match="CELERY_REDIS_DB"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_accepts_secure_production_smtp_configuration() -> None:
    values = build_environment_values()
    values["environment"] = "production"
    values["debug"] = False
    values["email_verification_delivery_mode"] = "smtp"
    values["smtp_host"] = "smtp.example.com"
    values["smtp_username"] = "smtp-user"
    values["smtp_password"] = "smtp-password"
    values["frontend_base_url"] = "https://app.example.com"

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert settings.environment == "production"
    assert settings.smtp_use_tls is True


def test_settings_repr_hides_sensitive_values() -> None:
    settings = Settings(_env_file=None, **build_environment_values())  # pyright: ignore[reportCallIssue]

    rendered = repr(settings)

    assert "clave-de-prueba-suficientemente-larga" not in rendered
    assert "clave-postgresql-de-prueba" not in rendered
    assert "clave-redis-de-prueba" not in rendered
    assert "clave-de-cifrado-de-prueba" not in rendered
