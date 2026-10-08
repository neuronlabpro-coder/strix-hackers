from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.core.config import ENV_FILE, Settings

ValorDeConfiguracion = str | int | float | bool | None | list[str]


def build_environment_values() -> dict[str, ValorDeConfiguracion]:
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
        "api_public_base_url": "http://localhost:8000",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_username": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "smtp_timeout_seconds": 10,
        # Stripe se deja sin credenciales en desarrollo a propósito: la plataforma
        # debe arrancar sin cobro. Las pruebas de producción lo vuelven a
        # configurar, igual que hacen con SMTP.
        "stripe_secret_key": None,
        "stripe_publishable_key": "",
        "stripe_webhook_secret": None,
        "credits_per_usd": "1",
        "scan_credit_cost": "10",
        "quick_scan_credit_multiplier": "0.5",
    }


def production_values() -> dict[str, ValorDeConfiguracion]:
    """Valores base de un despliegue de producción con SMTP y Stripe completos."""

    values = build_environment_values()
    values["environment"] = "production"
    values["debug"] = False
    values["strix_execution_mode"] = "host"
    values["strix_cli_path"] = "/opt/strix/bin/strix"
    values["email_verification_delivery_mode"] = "smtp"
    values["smtp_host"] = "smtp.example.com"
    values["smtp_username"] = "smtp-user"
    values["smtp_password"] = "smtp-password"
    values["frontend_base_url"] = "https://app.example.com"
    values["api_public_base_url"] = "https://api.example.com"
    values["stripe_secret_key"] = "sk_test_placeholder_no_es_una_clave_real"
    values["stripe_publishable_key"] = "pk_test_placeholder"
    values["stripe_webhook_secret"] = "whsec_placeholder"
    return values


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
    values = production_values()
    values["email_verification_delivery_mode"] = "development"

    with pytest.raises(ValidationError, match="smtp"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_insecure_production_smtp_transport() -> None:
    values = production_values()
    values = production_values()
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
    values = production_values()

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert settings.environment == "production"
    assert settings.smtp_use_tls is True


def test_production_rejects_old_container_runner() -> None:
    values = production_values()
    values["strix_execution_mode"] = "container"

    with pytest.raises(ValidationError, match="STRIX_EXECUTION_MODE=host"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_production_rejects_missing_cli_path() -> None:
    values = production_values()
    values["strix_cli_path"] = ""

    with pytest.raises(ValidationError, match="STRIX_CLI_PATH"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_insecure_public_api_base_url_in_production() -> None:
    values = production_values()
    values["api_public_base_url"] = "http://api.example.com"

    with pytest.raises(ValidationError, match="API_PUBLIC_BASE_URL"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]



def test_el_origen_del_frontend_se_annade_a_cors_solo() -> None:
    """Un frontend nuevo no obliga a editar `CORS_ORIGINS`.

    El olvido de esa sincronización produce el peor síntoma posible: el panel carga entero,
    todas sus peticiones fallan, y el error del navegador no menciona CORS. Por eso el
    origen se **añade** en vez de exigirse, y ese estado no es representable.
    """

    values = production_values()
    values["frontend_base_url"] = "https://panel.nuevo.example.com"
    values["cors_origins"] = ["https://mindguardredteam.com"]

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert "https://panel.nuevo.example.com" in settings.cors_origins
    # Y no se duplica si ya estaba.
    assert settings.cors_origins.count("https://panel.nuevo.example.com") == 1
    # El origen declarado se conserva: la lista admite origenes adicionales.
    assert "https://mindguardredteam.com" in settings.cors_origins


def test_en_produccion_se_descartan_los_origenes_de_otro_esquema() -> None:
    """Un origen `http` en producción se descarta, y el proceso **arranca**.

    La lista por defecto mezcla a propósito los orígenes locales con los de producción. En
    producción, un navegador del panel nunca enviará `Origin: http://localhost:5173`, así que
    ese origen está muerto pero es inocuo.

    Descartarlo en vez de fallar al arrancar es la diferencia entre un despliegue que no
    rompe y uno que no arranca. La alternativa —exigir que la lista no mezcle esquemas—
    obligaría a mantener una lista distinta por entorno, que es la sincronización que el
    validador existe para evitar.
    """

    values = production_values()
    values["cors_origins"] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://panel.mindguardredteam.com",
    ]

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert "http://localhost:5173" not in settings.cors_origins
    assert "http://127.0.0.1:5173" not in settings.cors_origins
    assert "https://panel.mindguardredteam.com" in settings.cors_origins


def test_el_origen_del_frontend_sobrevive_al_filtrado() -> None:
    """El origen del panel se añade **después** de filtrar, nunca antes.

    Es el orden que hace correcta la función. Si se añadiera antes, un frontend declarado en
    `http` en producción se descartaría a sí mismo al filtrar y la lista acabaría vacía; si
    se añadiera después, sobrevive. Comprobado con un frontend que no está en la lista
    declarada: el resultado tiene que contenerlo.
    """

    values = production_values()
    values["frontend_base_url"] = "https://panel.nuevo.example.com"
    values["cors_origins"] = ["http://localhost:5173"]

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert settings.cors_origins == ["https://panel.nuevo.example.com"]


def test_un_origen_con_ruta_no_se_considera_un_origen() -> None:
    """`https://x.example.com/app` no es un origen, y se descarta.

    Un origen no tiene ruta: `https://x.example.com/app` y `https://x.example.com/` son el
    mismo origen. Declarar uno con ruta no autoriza nada —el navegador envía el origen sin
    ruta en la cabecera— y dejarlo pasar daría la falsa impresión de que ese origen está
    permitido.
    """

    values = production_values()
    values["cors_origins"] = [
        "https://panel.mindguardredteam.com",
        "https://con-ruta.example.com/app",
    ]

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert "https://con-ruta.example.com/app" not in settings.cors_origins
    assert "https://panel.mindguardredteam.com" in settings.cors_origins


def test_en_desarrollo_el_origen_del_frontend_no_se_annade() -> None:
    """En local la lista significa "lo que el navegador vería sin proxy".

    El proxy de Vite sirve `/api` desde el mismo origen, así que la lista no se usa. Dejar
    que el validador la modificara haría que la lectura de "que origenes validos hay" mentía
    justo en el entorno donde se lee para entender el comportamiento.
    """

    values = build_environment_values()
    values["frontend_base_url"] = "http://localhost:4321"
    values["cors_origins"] = ["http://localhost:5173"]

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert settings.cors_origins == ["http://localhost:5173"]


def test_cors_declarado_por_defecto_cubre_local_y_produccion() -> None:
    """La lista por defecto trae los cuatro origenes que hacen falta.

    Local por si alguien abre la API desde la pagina, y los dos dominios reales de
    produccion. Es una lista de cuatro entradas y conviene que sea explicita: es la que se
    lee cuando alguien pregunta "de donde se puede llamar a la API".

    Se leen los valores por defecto **de la clase**, sin construir un `Settings`: la
    composicion por entorno ocurre en el validador, y aqui lo que se comprueba es la
    declaracion, no el resultado de la composicion.
    """

    from backend.core.config import Settings as ConfigSettings

    declarados = ConfigSettings.model_fields["cors_origins"].get_default(call_default_factory=True)

    for origen in (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://panel.mindguardredteam.com",
        "https://mindguardredteam.com",
    ):
        assert origen in declarados, f"falta el origen {origen}"
    assert len(declarados) == 4, "la lista por defecto deberia tener exactamente cuatro"

def test_settings_rejects_partial_git_provider_oauth_credentials() -> None:
    values = build_environment_values()
    values["github_oauth_client_id"] = "github-client"

    with pytest.raises(ValidationError, match="GitHub OAuth"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_accepts_paired_git_provider_oauth_credentials() -> None:
    values = build_environment_values()
    values["github_oauth_client_id"] = "github-client"
    values["github_oauth_client_secret"] = "github-secret"
    values["gitlab_oauth_client_id"] = "gitlab-client"
    values["gitlab_oauth_client_secret"] = "gitlab-secret"

    settings = Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

    assert settings.github_oauth_client_secret is not None
    assert settings.github_oauth_client_secret.get_secret_value() == "github-secret"
    assert "github-secret" not in repr(settings)


def test_settings_rejects_invalid_webhook_subscription_events() -> None:
    values = build_environment_values()
    values["git_webhook_subscription_events"] = "pull request;drop table"

    with pytest.raises(ValidationError, match="GIT_WEBHOOK_SUBSCRIPTION_EVENTS"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]

def test_settings_requires_stripe_in_production() -> None:
    """Sin secreto de Stripe, produccion no arranca: el webhookReload quede abierto."""

    values = production_values()
    values["stripe_secret_key"] = None

    with pytest.raises(ValidationError, match="STRIPE_SECRET_KEY"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_requires_stripe_webhook_secret_in_production() -> None:
    values = production_values()
    values["stripe_webhook_secret"] = None

    with pytest.raises(ValidationError, match="STRIPE_WEBHOOK_SECRET"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_rejects_publishable_key_with_wrong_prefix() -> None:
    values = production_values()
    values["stripe_publishable_key"] = "sk_esto_no_es_publicable"

    with pytest.raises(ValidationError, match="STRIPE_PUBLISHABLE_KEY"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_allows_missing_stripe_in_development() -> None:
    """En local la plataforma arranca sin cobro: solo falla al intentar cobrar."""

    settings = Settings(_env_file=None, **build_environment_values())  # pyright: ignore[reportCallIssue]

    assert settings.stripe_secret_key is None
    assert settings.stripe_webhook_secret is None
    assert settings.scan_credit_cost > 0
    assert settings.credits_per_usd > 0


def test_settings_rejects_quick_scan_multiplier_above_one() -> None:
    """Un escaneo rápido no puede costar más que uno estándar."""

    values = build_environment_values()
    values["quick_scan_credit_multiplier"] = "1.5"

    with pytest.raises(ValidationError, match="quick_scan_credit_multiplier"):
        Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_settings_repr_hides_sensitive_values() -> None:
    settings = Settings(_env_file=None, **build_environment_values())  # pyright: ignore[reportCallIssue]

    rendered = repr(settings)

    assert "clave-de-prueba-suficientemente-larga" not in rendered
    assert "clave-postgresql-de-prueba" not in rendered
    assert "clave-redis-de-prueba" not in rendered
    assert "clave-de-cifrado-de-prueba" not in rendered


# --------------------------------------------------------------------------- #
# R6: Redis solo se alcanza por Tailscale en produccion
# --------------------------------------------------------------------------- #


def test_produccion_acepta_el_alias_de_redis_de_la_red_interna() -> None:
    """En Dokploy, `REDIS_HOST` es `fenix-redis`: el alias del servicio en la red interna.

    ## Por qué esta prueba existe

    Porque se falló justo aquí. Se escribió una comprobación que exigía que `REDIS_HOST` fuera
    una IP del rango de Tailscale en producción, y broke Dokploy: el backend es un contenedor
    del mismo despliegue y habla con Redis por la red interna, no por el mesh. Los síntomas
    eran errores de Celery sin conexión con la cola, que no señalaban la causa.

    Confundir "Tailscale" con "R6" fue el error. R6 pide que los puertos de datos **no se
    alcancen desde Internet**, y el alias de la red interna cumple eso por construcción. Un
    nombre que solo resuelve dentro de la red de Docker no necesita ser una IP para estar
    protegido.
    """

    valores = production_values()
    valores["redis_host"] = "fenix-redis"
    valores["redis_url"] = "redis://:clave-redis-de-prueba@fenix-redis:6380/0"
    settings = Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]
    assert settings.redis_host == "fenix-redis"


def test_produccion_rechaza_una_redis_publica() -> None:
    """Lo que R6 prohíbe de verdad es que la dirección sea de Internet.

    Y no se comprueba solo que la IP sea inválida o que no cuadre con `REDIS_URL`: se comprueba
    que **no salga a Internet**, que es la propiedad. Da igual si ese Redis existe o si el
    despliegue arranca: en cuanto la dirección es pública, la credencial de la plataforma está
    en manos de quien administre ese servicio, y un `docker logs` puede acabar en un sitio que
    nadie ha revisado.
    """

    valores = production_values()
    valores["redis_host"] = "8.8.8.8"
    valores["redis_url"] = "redis://:clave-redis-de-prueba@8.8.8.8:6380/0"
    with pytest.raises(ValidationError, match="direcci"):
        Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]

    # Y el mensaje tiene que decir cuál es la forma buena, porque el que se topa con esto no
    # sabe todavía que hay una red interna detrás.
    valores = production_values()
    valores["redis_host"] = "8.8.8.8"
    valores["redis_url"] = "redis://:clave-redis-de-prueba@8.8.8.8:6380/0"
    with pytest.raises(ValidationError, match="fenix-redis"):
        Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]


def test_produccion_acepta_una_ip_privada_para_redis() -> None:
    """Una IP privada no es pública, y hay despliegues legítimos que la usan.

    La comprobación es "no pública" y no "es un alias", porque la categoría es lo único cierto
    en todos los despliegues. Si se exigiera el alias, un despliegue con la IP privada del
    servicio en su misma máquina —que es igual de interno— quedaría fuera sin motivo.
    """

    valores = production_values()
    valores["redis_host"] = "10.0.0.5"
    valores["redis_url"] = "redis://:clave-redis-de-prueba@10.0.0.5:6380/0"
    settings = Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]
    assert settings.redis_host == "10.0.0.5"


def test_desarrollo_no_rechaza_una_redis_publica() -> None:
    """La comprobación es **solo** de producción, y por eso hay que comprobar que no es de más.

    En desarrollo se puede apuntar a un Redis de pruebas en cualquier parte, y bloquearlo estorba
    sin ganar nada. Una guarda que se aplica donde no toca entrena a ignorar el mensaje cuando
    sí importa, que es el peor resultado posible de una comprobación de configuración.
    """

    valores = build_environment_values()
    valores["redis_host"] = "8.8.8.8"
    valores["redis_url"] = "redis://:clave-redis-de-prueba@8.8.8.8:6380/0"
    settings = Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]
    assert settings.redis_host == "8.8.8.8"
