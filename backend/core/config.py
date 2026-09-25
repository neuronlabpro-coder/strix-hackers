"""Configuración validada del backend desde el archivo raíz `.env`."""

import base64
import binascii
from pathlib import Path
from typing import Literal, Self
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
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
    git_webhook_max_body_bytes: int = Field(default=2_000_000, gt=0, le=10_000_000)
    git_webhook_subscription_events: str = "pull_request,issue_comment"
    git_webhook_rate_limit: int = Field(default=120, ge=1, le=1000)
    git_webhook_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    git_webhook_replay_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    git_api_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    git_command_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    git_max_diff_files: int = Field(default=5000, ge=1, le=100_000)
    git_max_diff_path_chars: int = Field(default=512, ge=32, le=4096)
    git_allowed_clone_hosts_csv: str = Field(
        default="github.com,gitlab.com,bitbucket.org,gitea.com",
        min_length=1,
    )
    chatops_review_commands: str = Field(
        default="@fenix-team review,@strix review",
        min_length=1,
    )
    autofix_branch_prefix: str = Field(default="fenix/fix-", min_length=1, max_length=64)
    autofix_max_files: int = Field(default=50, ge=1, le=500)
    autofix_create_rate_limit: int = Field(default=5, ge=1, le=100)
    autofix_create_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    pr_scan_hard_timeout_seconds: int = Field(default=900, gt=0, le=7200)
    pr_scan_soft_timeout_seconds: int = Field(default=840, gt=0, le=7200)
    pr_review_stale_after_seconds: int = Field(default=300, ge=30, le=86400)
    api_public_base_url: str = Field(default="http://localhost:8000", min_length=1)
    oauth_state_ttl_seconds: int = Field(default=600, ge=300, le=1800)
    repository_management_rate_limit: int = Field(default=60, ge=1, le=500)
    repository_management_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    oauth_callback_rate_limit: int = Field(default=30, ge=1, le=200)
    oauth_callback_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    github_oauth_client_id: str = ""
    github_oauth_client_secret: SecretStr | None = None
    github_oauth_authorize_url: str = "https://github.com/login/oauth/authorize"
    github_oauth_token_url: str = "https://github.com/login/oauth/access_token"  # noqa: S105
    github_oauth_scopes: str = "repo read:user admin:repo_hook"
    gitlab_oauth_client_id: str = ""
    gitlab_oauth_client_secret: SecretStr | None = None
    gitlab_oauth_authorize_url: str = "https://gitlab.com/oauth/authorize"
    gitlab_oauth_token_url: str = "https://gitlab.com/oauth/token"  # noqa: S105
    gitlab_oauth_scopes: str = "api read_user read_repository"

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

    @staticmethod
    def _decode_git_encryption_key(value: str) -> bytes:
        raw_value = value.encode("utf-8")
        if len(raw_value) == 32:
            return raw_value
        try:
            padding = "=" * (-len(value) % 4)
            decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("GIT_ENCRYPTION_KEY no contiene 32 bytes válidos") from error
        if len(decoded) != 32:
            raise ValueError("GIT_ENCRYPTION_KEY debe contener exactamente 32 bytes")
        return decoded

    @field_validator("git_encryption_key")
    @classmethod
    def validate_git_encryption_key(cls, value: SecretStr) -> SecretStr:
        """Exige exactamente 32 bytes de material para AES-256."""

        cls._decode_git_encryption_key(value.get_secret_value())
        return value

    @property
    def git_encryption_key_bytes(self) -> bytes:
        """Entrega la clave maestra solo como bytes para el cifrador AES."""

        return self._decode_git_encryption_key(self.git_encryption_key.get_secret_value())

    @property
    def git_allowed_clone_hosts(self) -> frozenset[str]:
        """Hosts autorizados para materializar repositorios en producción."""

        return frozenset(
            host.strip().lower()
            for host in self.git_allowed_clone_hosts_csv.split(",")
            if host.strip()
        )

    @property
    def chatops_review_command_aliases(self) -> tuple[str, ...]:
        """Comandos ChatOps permitidos, normalizados sin depender del locale."""

        return tuple(
            command.strip()
            for command in self.chatops_review_commands.split(",")
            if command.strip()
        )

    @model_validator(mode="after")
    def validate_runtime_and_endpoints(self) -> Self:
        """Impide combinaciones inseguras o endpoints con componentes divergentes."""

        if self.environment == "production" and self.debug:
            raise ValueError("DEBUG debe ser false en el entorno production")
        if self.environment in {"staging", "production"} and self._decode_git_encryption_key(
            self.git_encryption_key.get_secret_value()
        ) == b"0123456789abcdef0123456789abcdef":
            raise ValueError("GIT_ENCRYPTION_KEY de ejemplo no se permite fuera de desarrollo")

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
        if self.pr_scan_soft_timeout_seconds >= self.pr_scan_hard_timeout_seconds:
            raise ValueError("El timeout suave del scan PR debe ser menor que el duro")
        if not self.git_allowed_clone_hosts:
            raise ValueError("GIT_ALLOWED_CLONE_HOSTS debe contener al menos un host")
        subscription_events = [
            event.strip()
            for event in self.git_webhook_subscription_events.split(",")
            if event.strip()
        ]
        if not subscription_events or any(
            not event.replace("_", "").isalnum() for event in subscription_events
        ):
            raise ValueError(
                "GIT_WEBHOOK_SUBSCRIPTION_EVENTS solo admite identificadores de evento"
            )
        if not self.chatops_review_command_aliases:
            raise ValueError("CHATOPS_REVIEW_COMMANDS debe contener un comando")
        if any(char.isspace() for char in self.autofix_branch_prefix):
            raise ValueError("AUTOFIX_BRANCH_PREFIX no puede contener espacios")
        github_secret_configured = (
            self.github_oauth_client_secret is not None
            and bool(self.github_oauth_client_secret.get_secret_value())
        )
        gitlab_secret_configured = (
            self.gitlab_oauth_client_secret is not None
            and bool(self.gitlab_oauth_client_secret.get_secret_value())
        )
        if bool(self.github_oauth_client_id) != github_secret_configured:
            raise ValueError("GitHub OAuth requiere client ID y client secret juntos")
        if bool(self.gitlab_oauth_client_id) != gitlab_secret_configured:
            raise ValueError("GitLab OAuth requiere client ID y client secret juntos")

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
            try:
                api_scheme = urlsplit(self.api_public_base_url).scheme.lower()
            except ValueError as error:
                raise ValueError("API_PUBLIC_BASE_URL no es una URL válida") from error
            if api_scheme != "https":
                raise ValueError("API_PUBLIC_BASE_URL debe usar HTTPS en producción y staging")
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
