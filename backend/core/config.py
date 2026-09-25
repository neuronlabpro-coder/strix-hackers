"""Configuración validada del backend desde el archivo raíz `.env`."""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import EmailStr, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Valida y expone la configuración de infraestructura de la aplicación."""

    environment: Literal["development", "test", "staging", "production"]
    debug: bool
    secret_key: SecretStr = Field(min_length=32, repr=False)

    db_host: str = Field(min_length=1)
    db_port: int = Field(ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1, repr=False)
    database_url: str = Field(min_length=1, repr=False)
    db_pool_size: int = Field(ge=1)
    db_max_overflow: int = Field(ge=0)
    db_pool_recycle_seconds: int = Field(gt=0)
    db_connect_timeout_seconds: float = Field(gt=0)

    redis_host: str = Field(min_length=1)
    redis_port: int = Field(ge=1, le=65535)
    redis_password: SecretStr = Field(min_length=1, repr=False)
    redis_url: str = Field(min_length=1, repr=False)
    redis_db: int = Field(ge=0)
    redis_socket_timeout_seconds: float = Field(gt=0)

    git_encryption_key: SecretStr = Field(min_length=32, repr=False)

    jwt_algorithm: Literal["HS256", "HS384", "HS512"]
    access_token_expire_minutes: int = Field(gt=0)
    invitation_expire_days: int = Field(gt=0)
    password_min_length: int = Field(ge=8)
    password_max_length: int = Field(ge=32)
    bcrypt_rounds: int = Field(ge=4, le=16)
    auth_login_rate_limit: int = Field(ge=1, le=100)
    auth_login_rate_window_seconds: int = Field(ge=1, le=3600)
    auth_register_rate_limit: int = Field(ge=1, le=100)
    auth_register_rate_window_seconds: int = Field(ge=1, le=3600)
    organization_create_rate_limit: int = Field(ge=1, le=100)
    organization_create_rate_window_seconds: int = Field(ge=1, le=3600)
    invitation_rate_limit: int = Field(ge=1, le=100)
    invitation_rate_window_seconds: int = Field(ge=1, le=3600)
    email_verification_ttl_minutes: int = Field(gt=0, le=10080)
    email_verification_delivery_mode: Literal["development", "smtp"]
    email_verification_from: EmailStr
    frontend_base_url: str = Field(min_length=1)
    smtp_host: str = ""
    smtp_port: int = Field(ge=1, le=65535)
    smtp_username: SecretStr | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    smtp_timeout_seconds: float = Field(gt=0)

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    @model_validator(mode="after")
    def validate_runtime_and_endpoints(self) -> Self:
        """Impide combinaciones inseguras o endpoints con componentes divergentes."""

        if self.environment == "production" and self.debug:
            raise ValueError("DEBUG debe ser false en el entorno production")

        if not self._database_url_matches_components():
            raise ValueError("DATABASE_URL no coincide con las variables DB_* configuradas")

        if not self._redis_url_matches_components():
            raise ValueError("REDIS_URL no coincide con las variables REDIS_* configuradas")

        if self.email_verification_delivery_mode == "smtp" and not self.smtp_host:
            raise ValueError("SMTP_HOST es obligatorio cuando el modo de email es smtp")

        return self

    def _database_url_matches_components(self) -> bool:
        try:
            parsed_url = urlsplit(self.database_url)
            return (
                parsed_url.scheme == "postgresql+asyncpg"
                and parsed_url.hostname == self.db_host
                and parsed_url.port == self.db_port
                and parsed_url.username == self.db_user
                and unquote(parsed_url.password or "") == self.db_password.get_secret_value()
                and parsed_url.path.lstrip("/") == self.db_name
            )
        except ValueError:
            return False

    def _redis_url_matches_components(self) -> bool:
        try:
            parsed_url = urlsplit(self.redis_url)
            database_number = int(parsed_url.path.lstrip("/") or "0")
            return (
                parsed_url.scheme == "redis"
                and parsed_url.hostname == self.redis_host
                and parsed_url.port == self.redis_port
                and unquote(parsed_url.password or "") == self.redis_password.get_secret_value()
                and database_number == self.redis_db
            )
        except ValueError:
            return False


settings = Settings()  # pyright: ignore[reportCallIssue]
