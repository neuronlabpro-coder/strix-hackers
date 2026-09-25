"""Configuración validada del backend desde el archivo raíz `.env`."""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import unquote, urlsplit, urlunsplit

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
    celery_redis_db: int = Field(default=1, ge=0, le=15)
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
    pentest_create_rate_limit: int = Field(default=5, ge=1, le=100)
    pentest_create_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    celery_task_time_limit_seconds: int = Field(default=2400, gt=0, le=86400)
    celery_task_soft_time_limit_seconds: int = Field(default=2100, gt=0, le=86400)
    strix_max_output_bytes: int = Field(default=10_000_000, gt=0, le=50_000_000)
    strix_max_findings: int = Field(default=10_000, gt=0, le=100_000)
    strix_max_description_chars: int = Field(default=100_000, gt=0, le=1_000_000)
    strix_max_poc_chars: int = Field(default=1_000_000, gt=0, le=5_000_000)
    strix_max_autofix_chars: int = Field(default=2_000_000, gt=0, le=10_000_000)
    strix_sandbox_image: str = Field(
        default="ghcr.io/usestrix/strix-sandbox:latest",
        min_length=1,
    )
    strix_workspace_root: str = Field(default="/tmp/fenix_workspaces", min_length=1)  # noqa: S108
    strix_network_prefix: str = Field(default="strix_net", min_length=1)
    strix_memory_limit: str = Field(default="4g", min_length=1)
    strix_cpu_limit: float = Field(default=2.0, gt=0, le=8)
    strix_pids_limit: int = Field(default=256, gt=0, le=100_000)
    strix_worker_concurrency: int = Field(default=1, gt=0, le=32)
    strix_hard_timeout_seconds: int = Field(default=1800, gt=0, le=86400)
    strix_soft_timeout_seconds: int = Field(default=1500, gt=0, le=86400)
    strix_watchdog_interval_seconds: int = Field(default=300, gt=0, le=86400)
    strix_watchdog_stale_after_seconds: int = Field(default=1860, gt=0, le=172800)
    default_strix_llm: str = Field(min_length=1)
    llm_api_key: SecretStr = Field(min_length=1, repr=False)
    llm_api_base: str = ""
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
        if self.celery_redis_db == self.redis_db:
            raise ValueError("CELERY_REDIS_DB debe ser distinto de REDIS_DB")

        smtp_username_provided = self.smtp_username is not None and bool(
            self.smtp_username.get_secret_value()
        )
        smtp_password_provided = self.smtp_password is not None and bool(
            self.smtp_password.get_secret_value()
        )
        if smtp_username_provided != smtp_password_provided:
            raise ValueError("Las credenciales SMTP deben configurar usuario y contraseña juntos")

        if self.celery_task_soft_time_limit_seconds >= self.celery_task_time_limit_seconds:
            raise ValueError("El límite suave de Celery debe ser menor que el límite duro")
        if self.strix_soft_timeout_seconds >= self.strix_hard_timeout_seconds:
            raise ValueError("El timeout suave de Strix debe ser menor que el duro")
        if self.strix_hard_timeout_seconds >= self.celery_task_soft_time_limit_seconds:
            raise ValueError("Strix hard timeout debe ser menor que el timeout suave de Celery")
        if self.strix_watchdog_stale_after_seconds <= self.strix_hard_timeout_seconds:
            raise ValueError("El watchdog debe esperar más que el timeout duro de Strix")

        if self.email_verification_delivery_mode == "smtp" and not self.smtp_host:
            raise ValueError("SMTP_HOST es obligatorio cuando el modo de email es smtp")

        if self.environment in {"staging", "production"}:
            if self.email_verification_delivery_mode != "smtp":
                raise ValueError(
                    "Producción y staging requieren EMAIL_VERIFICATION_DELIVERY_MODE=smtp"
                )
            try:
                frontend_scheme = urlsplit(self.frontend_base_url).scheme.lower()
            except ValueError as error:
                raise ValueError("FRONTEND_BASE_URL no es una URL válida") from error
            if frontend_scheme != "https":
                raise ValueError("FRONTEND_BASE_URL debe usar HTTPS en producción y staging")
            if not self.smtp_use_tls:
                raise ValueError("SMTP_USE_TLS debe ser true en producción y staging")
            if not smtp_username_provided or not smtp_password_provided:
                raise ValueError(
                    "Producción y staging requieren credenciales SMTP completas"
                )
            try:
                llm_scheme = urlsplit(self.llm_api_base).scheme.lower() if self.llm_api_base else ""
            except ValueError as error:
                raise ValueError("LLM_API_BASE no es una URL válida") from error
            if self.llm_api_base and llm_scheme != "https":
                raise ValueError("LLM_API_BASE debe usar HTTPS en producción y staging")

        return self

    @property
    def celery_redis_url(self) -> str:
        """Deriva la URL de Celery en una base Redis separada sin duplicar secretos."""

        parsed_url = urlsplit(self.redis_url)
        return urlunsplit(parsed_url._replace(path=f"/{self.celery_redis_db}"))

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
