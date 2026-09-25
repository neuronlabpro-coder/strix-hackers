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
        "redis_socket_timeout_seconds": 10,
        "git_encryption_key": "clave-de-cifrado-de-prueba-para-aes-256-gcm",
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


def test_settings_repr_hides_sensitive_values() -> None:
    settings = Settings(_env_file=None, **build_environment_values())  # pyright: ignore[reportCallIssue]

    rendered = repr(settings)

    assert "clave-de-prueba-suficientemente-larga" not in rendered
    assert "clave-postgresql-de-prueba" not in rendered
    assert "clave-redis-de-prueba" not in rendered
    assert "clave-de-cifrado-de-prueba" not in rendered
