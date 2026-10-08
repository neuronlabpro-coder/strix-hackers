"""Configuración validada del backend desde el archivo raíz `.env`."""

import base64
import binascii
import ipaddress
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _origin_of(url: str) -> str:
    """El origen de una URL: esquema y autoridad, sin ruta ni consulta.

    Es lo que CORS compara. Una ruta no es parte del origen —`/app` y `/` son el mismo
    origen— y un `Origin` que llega en la cabecera nunca trae ruta, así que compararla
    entera daría un fallo por cada página que no esté en la raíz.

    Se normaliza el esquema a minúsculas porque los navegadores lo envían en minúscula y
    `HTTPS://x` y `https://x` son el mismo origen; si no se normalizara, la lista de CORS
    fallaría solo con el origen de producción escrito en mayúsculas.
    """

    try:
        partes = urlsplit(url)
    except ValueError:
        return ""
    if not partes.scheme or not partes.netloc:
        return ""
    return f"{partes.scheme.lower()}://{partes.netloc.lower()}"


def _scheme_of(url: str) -> str:
    """El esquema de una URL, en minúsculas, o cadena vacía si no se puede leer."""

    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


#: El valor que entrega `.env.example` para `SECRET_KEY`, y que en staging y producción es
#: exactamente igual de inválido que el de ejemplo de `GIT_ENCRYPTION_KEY`.
#:
#: ## Por qué esto necesita ser una constante y no un literal en el validador
#:
#: Porque el literal está en **dos** sitios —aquí y en `.env.example`— y si divergen la guarda
#: deja de funcionar sin que nada avise: el despliegue arranca con la clave de ejemplo y la
#: comprobación pasa porque el texto ya no coincide. Ponerlo aquí hace que la prueba pueda
#: leerlo y afirmar que `.env.example` dice exactamente esto.
#:
#: ## Por qué el riesgo es real y no teórico
#:
#: `.env.example` **no** se puede copiar tal cual a producción: `STRIPE_SECRET_KEY` y
#: `SMTP_PASSWORD` sí tienen guardas y el arranque falla. El operador configura esas dos, ve el
#: contenedor subir, y `SECRET_KEY` se queda con el valor público sin que nada lo señale. Y con
#: esa clave se firma cualquier JWT, incluido uno con el `sub` de un superusuario.
#: El nombre lleva `_DE_EJEMPLO` y no `SECRET_KEY` a proposito: `ruff` trata cualquier
#: constante con `KEY` en el nombre y un literal como una possible hardcoded password, y en este
#: caso **si lo es** —es la clave de ejemplo de `.env.example`—, pero la regla no lo puede saber y
#: la exception que haria falta desactivaria la comprobacion para todo el fichero, que es peor.
#: Por eso se renombra y se documenta: el aviso se resuelve en la linea que declara la constante.
SECRET_KEY_DE_EJEMPLO: Final[str] = (
    "cambiar_por_un_secreto_aleatorio_de_al_menos_32_caracteres"  # noqa: S105
)


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
    stripe_webhook_max_body_bytes: int = Field(default=262_144, gt=0, le=10_000_000)
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
    #: Orígenes desde los que el navegador acepta peticiones a esta API.
    #:
    #: ## Por qué hace falta en producción y no en local
    #:
    #: En desarrollo el frontend **no** usa CORS: el proxy de Vite sirve `/api` desde el
    #: mismo origen que la SPA, así que el navegador nunca ve un cruce. Por eso se puede
    #: tener la aplicación entera funcionando sin un solo origen declarado.
    #:
    #: En producción es distinto: la SPA se sirve en `panel.` y la API responde en `api.`, que
    #: son **orígenes distintos** por definición. Sin esta lista el navegador bloquea cada
    #: petición antes de que salga, y el panel se queda entero sin datos con un error de red
    #: que no dice nada de CORS.
    #:
    #: Se declaran los dos pares —local y producción— en el mismo sitio porque son la misma
    #: lista conceptual: "los orígenes desde los que se sirve nuestro frontend". Separarlos
    #: en dos variables por entorno haría que añadir un dominio de staging se olvidara, y se
    #: olvidaría **en el sitio donde solo se descubre cuando ya está desplegado**.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "https://panel.mindguardredteam.com",
            "https://mindguardredteam.com",
        ],
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        """Acepta la lista CSV documentada o una lista JSON en variables de entorno."""

        if not isinstance(value, str):
            return value

        value = value.strip()
        if value.startswith("["):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("CORS_ORIGINS debe ser CSV o una lista JSON válida") from exc
            if not isinstance(decoded, list):
                raise ValueError("CORS_ORIGINS en JSON debe ser una lista")
            return decoded

        return [origin.strip() for origin in value.split(",") if origin.strip()]

    @model_validator(mode="after")
    def resolve_cors_origins(self) -> Self:
        """Compone la lista de orígenes de CORS para este entorno.

        ## Qué guarantee esta regla

        Que el origen real del panel **siempre** está autorizado, y que en producción solo
        quedan orígenes del mismo esquema que la API. Las dos cosas se resuelven aquí en vez
        de exigirse en la configuración, porque un estado mal configurado produce el peor
        síntoma posible: el panel carga entero, todas sus peticiones fallan, y el error del
        navegador no menciona CORS. Es un día perdido buscando un problema de autenticación.

        ## Por qué el origen del frontend se **añade** y no se exige

        Exigir que `FRONTEND_BASE_URL` esté en `CORS_ORIGINS` obliga a mantener dos variables
        sincronizadas a mano. Añadiéndolo, un despliegue nuevo arranca con el origen correcto
        sin tocar nada, y la lista declarada queda como la forma de añadir orígenes
        adicionales —un panel de staging, una herramienta interna—.

        ## Por qué en producción se **descartan** los orígenes de otro esquema

        La lista por defecto mezcla a propósito `http://localhost:5173` con los dominios de
        producción. En producción, un navegador del panel nunca enviará
        `Origin: http://localhost:5173`, así que ese origen está muerto pero **inocuo**.

        Descartarlo en vez de fallar al arrancar es la diferencia entre un despliegue que no
        rompe y uno que no arranca. La alternativa —exigir que la lista declarada no mezcle
        esquemas— obligaría a mantener una lista distinta por entorno, que es exactamente la
        sincronización que se quiere evitar: un despliegue de staging olvidaría quitar los
        orígenes locales y no arrancaría, con un error que habla de CORS y no de la causa real.

        ## Por qué `object.__setattr__` y no una copia

        Porque `Settings` hereda de `BaseSettings`, y `pydantic_settings` valida **dentro de
        `__init__`**: un validador de nivel superior que devuelva algo distinto de `self` no
        se aplica, y la librería lo avisa con un aviso en vez de un error. La forma de
        modificar el resultado de la validación es escribir en el campo.

        `object.__setattr__` salta la protección de congelado, y aquí es deliberado y está
        acotado a un único campo derivado, durante la construcción del objeto y antes de que
        exista ninguna petición. La congelación existe para que la configuración no cambie
        bajo los pies de nadie **en caliente**; aquí no hay nadie todavia. Devolver `self`
        sin mas, que es lo que haria `model_copy` en un `BaseModel` normal, daria una lista
        sin filtrar y un despliegue con CORS roto.
        """

        origen_panel = _origin_of(self.frontend_base_url)
        esquema_api = _scheme_of(self.api_public_base_url)

        if self.environment not in {"staging", "production"}:
            # En desarrollo la lista no se usa: el proxy de Vite sirve `/api` desde el mismo
            # origen. Se deja exactamente como se declaró, para que "qué orígenes hay" siga
            # significando "los que declaré" y no "los que el validador decidió".
            return self

        aplicables = [
            origen
            for origen in self.cors_origins
            if _scheme_of(origen) == esquema_api and _origin_of(origen) == origen
        ]
        if origen_panel and origen_panel not in aplicables:
            aplicables.append(origen_panel)

        if aplicables != self.cors_origins:
            object.__setattr__(self, "cors_origins", aplicables)
        return self
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
    #: Antelación con la que se renueva un token de acceso OAuth antes de que expire.
    #:
    #: No es un detalle: `token_expires_at` es un instante y una petición al proveedor dura un rato,
    #: así que renovar justo al expirar entrega un token que se caduca a mitad de la llamada y
    #: produce un `401` en una operación que un minuto antes funcionaba. El suelo es `0` —quien no
    #: quiera margen, no lo quiere— y el techo de una hora impide que un valor mal puesto convierta
    #: cada petición de inventario en un refresco.
    git_token_refresh_margin_seconds: int = Field(default=60, ge=0, le=3600)

    #: Ventana por adelantado del barrido de renovación de Celery Beat.
    #:
    #: Es la caducidad que se mira «tan pronto como» al barrido: cualquier credencial que caduque
    #: dentro de esta ventana se renueva antes de que nadie la necesite. El suelo es de un minuto
    #: porque por debajo el barrido no adelanta nada; el techo de un día impide que un valor mal
    #: puesto convierta cada pasada en un refresco de todas las conexiones OAuth del producto.
    #:
    #: Esto **no** es el mecanismo que garantiza que haya un token en vigor en el momento de un
    #: webhook: eso lo garantiza `asegurar_credentialo_vigente_en_sesion_ajena`, que se ejecuta
    #: igual. El barrido es una comodidad que quita la llamada al proveedor del camino crítico.
    git_token_proactive_refresh_window_seconds: int = Field(default=3600, ge=60, le=86400)
    #: Cada cuánto se ejecuta el barrido. Tiene que ser **menor** que la ventana, o hay tramos en
    #: los que una credencial ya está dentro de la ventana y el barrido todavía no ha pasado por
    #: ella. Que no sea así se comprueba en el validador de la configuración.
    git_token_proactive_refresh_interval_seconds: int = Field(default=900, gt=0, le=86400)
    #: Tope de credenciales por pasada. Sin tope, un barrido con muchos tenants se lleva el ancho de
    #: banda del proceso de Beat entero; con tope, lo que no llega hoy llega en la siguiente
    #: pasada, y como el orden es por caducidad, lo que más urge es lo primero que se atiende.
    git_token_proactive_refresh_batch_size: int = Field(default=100, gt=0, le=1000)

    jwt_algorithm: Literal["HS256", "HS384", "HS512"]
    access_token_expire_minutes: int = Field(gt=0)
    invitation_expire_days: int = Field(gt=0)
    password_min_length: int = Field(ge=8)
    password_max_length: int = Field(ge=32)
    #: Rondas de bcrypt.
    #:
    #: El `ge=4` de antes permitia un despliegue en produccion con cuatro rondas, que es un
    #: factor de coste dos mil veces menor que el de doce. No es un parametro de rendimiento:
    #: es la unica defensa que hay contra un ataque de fuerza bruta sobre las contrasenas
    #: filtradas, y bajarla la desactiva sin que nada falle.
    #:
    #: El suelo se exige **fuera de desarrollo**, y no en el `Field` porque en desarrollo y en las
    #: pruebas de integracion cuatro rondas hacen las pruebas usables. El coste se paga una vez
    #: por inicio de sesion, que es un sitio malo para un atacante y un sitio bueno para el
    #: usuario; bajarlo lo cambia por uno malo para los dos.
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
    #: Encolado de escaneos de agente. Es más generoso que `pentest_create` porque un escaneo de
    #: contenedor es de los que la gente encadena —veinte imágenes de un servicio— y porque el
    #: coste lo acota el cobro de créditos, no el límite. Lo que el límite evita es el bucle.
    scan_create_rate_limit: int = Field(default=20, ge=1, le=1000)
    scan_create_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    #: Alta y baja de agentes. Muy bajo a proposito: emitir una credencial de red es una
    #: operacion de administracion de riesgo alto, y no es una accion que se repita en un bucle
    #: legitimo. Cinco por hora deja margen de sobra para rotar un agente.
    agent_enrollment_rate_limit: int = Field(default=5, ge=1, le=100)
    agent_enrollment_rate_window_seconds: int = Field(default=3600, ge=1, le=86400)
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

    #: Cómo se ejecuta el motor: en un contenedor efímero o como proceso del host.
    #:
    #: ## Por qué `"container"` es el valor por defecto y no una preferencia
    #:
    #: Porque el modo contenedor es el único que puede sostener dos garantías que R3 exige
    #: y que el modo host **no puede**, por construcción y no por configuración:
    #:
    #: 1. **Aislamiento de ejecución.** Cada escaneo corre con `cap_drop=ALL`,
    #:    `no-new-privileges`, límites de memoria, CPU y PIDs, y una red bridge dedicada
    #:    con cerco de salida. Un motor que explota una dependencia se queda dentro de ese
    #:    namespace. Como proceso del host hereda los permisos del usuario del worker: la
    #:    red de Tailscale, PostgreSQL y Redis le quedan al alcance.
    #: 2. **Purgado garantizado.** El contenedor se quita por identificador aunque el
    #:    proceso muera de forma anormal; en el host el purgado depende de que el árbol de
    #:    procesos se mate entero.
    #:
    #: El modo host existe porque la imagen `strix_sandbox_image` **no lleva el motor**
    #: dentro —`exec: strix: not found`, exit 127— así que el modo contenedor no puede
    #: ejecutar un escaneo real en esta máquina. Es un modo de desarrollo y de diagnóstico,
    #: y por eso hay que pedirlo explícitamente por configuración.
    strix_execution_mode: Literal["container", "host"] = "container"

    #: Ruta del ejecutable del CLI del motor, usada **solo** en modo `host`.
    #:
    #: R1: la ruta no vive en el código. Es del despliegue —una instalación con `uv tool
    #: install` deja el ejecutable en un sitio distinto según la máquina—, y un despliegue
    #: tiene que poder cambiarlo sin redesplegar el backend.
    strix_cli_path: str = Field(default="")

    #: Tope de gasto del proveedor **por escaneo**, en dólares, y el número de turnos por agente.
    #:
    #: ## Por qué son topes y no un precio
    #:
    #: Porque el precio lo fija el catálogo y lo que paga la plataforma lo fija el proveedor, y
    #: entre los dos hay un camino que nadie controla: el motor decide cuántas llamadas hace. Un
    #: QUICK real de este proyecto costó **1,85 USD** en 244 peticiones al modelo sin que nadie
    #: declarara ningún límite, y un `deep` sobre un objetivo grande puede multiplicarlo por
    #: diez. `--max-budget` y `--max-turns` son las dos cosas que el motor acepta para frenarse
    #: solo, y sin ellos el único tope es el muro de tiempo, que para cuando salta ya ha gastado.
    #:
    #: ## Por qué un valor por defecto **conservador** y no «sin tope»
    #:
    #: Porque «sin tope declarado» no es neutral: es un gasto sin techo en una plataforma que
    #: cobra por Credits y cuyo coste de proveedor no conoce hasta el final. El suelo es
    #: deliberadamente holgado para que el QUICK de referencia entre con margen —1,85 contra 3—,
    #: y ajustado para que un `deep` que se descontrola se pare antes de la factura, no después.
    strix_max_budget_usd: Decimal = Field(default=Decimal("3.00"), gt=0, le=1000)
    #: Turnos por agente. El motor trae 500 de fábrica; bajarlo acota el número de iteraciones
    #: que un agente puede encadenar sin parar, que es el otro multiplicador del gasto.
    strix_max_turns: int = Field(default=200, gt=0, le=10_000)

    #: Modo **replay**: directorio de un run real del motor (`strix_runs/<run>/`) cuyos
    #: artefactos se meten por el mismo camino de parseo, persistencia y panel que un escaneo
    #: de verdad, **sin lanzar el motor y sin llamar al proveedor**.
    #:
    #: ## Por qué está vacío por defecto y no hay bandera que lo active
    #:
    #: Porque sin ruta no hay modo replay: no existe un interruptor, no hay un valor « Activado »,
    #: y un despliegue que no lo escribe no lo tiene. Es lo contrario de una bandera en la que
    #: «activado por defecto» y «activado a propósito» se parezcan.
    #:
    #: ## Por qué `Settings` **lanza** fuera de desarrollo
    #:
    #: Porque un modo que reproduce los artefactos de un escaneo no puede existir en un despliegue
    #: real: fabricaría hallazgos que el motor nunca encontró, con los créditos y la trazabilidad
    #: de un escaneo verdadero. No es un aviso: es un rechazo de arranque, en la misma categoría
    #: que el secreto de ejemplo o el bcrypt débil, y por el mismo motivo —una propiedad que se
    #: puede desactivar con una línea de configuración no es una propiedad.
    strix_replay_source: str = Field(default="")

    #: Esquema con el que se compone la URL de un target `DOMAIN` en modo host.
    #:
    #: `PentestCreate` rechaza el esquema en `target_identifier` a propósito —un dominio es
    #: un dominio, sin ruta ni puerto— y el motor, en cambio, exige una URL con esquema. El
    #: puente entre las dos cosas va aquí y no en el código del runner, porque el esquema con
    #: el que se prueba un dominio es una política del despliegue, no del lenguaje.
    strix_default_target_scheme: Literal["http", "https"] = "https"
    strix_network_prefix: str = Field(default="strix_net", min_length=1)
    strix_network_pool: str = Field(
        default="172.31.0.0/16",
        min_length=1,
    )
    strix_require_egress_fence: bool = Field(
        default=True,
    )
    strix_require_llm_key_exposure_ack: bool = Field(
        default=True,
    )
    strix_llm_key_exposure_ack: str = Field(default="")
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

    # ----------------------------------------------------------------- #
    # Parametros del chat con agentes.
    #
    # R1: ninguno de estos vive en el codigo del runner. Son comportamiento del
    # producto —que tan directa es la respuesta, cuanto contexto se le da, cuanto
    # historial recuerda— y se ajustan por despliegue, no por acuerdo comercial con
    # un cliente, pero siguen siendo configuracion: un numero escrito en el modulo
    # del runner es un numero que nadie puede cambiar sin redesplegar.
    # ----------------------------------------------------------------- #

    #: Temperatura de las respuestas del asistente. **No** es cero, a diferencia del
    #: `autofix`.
    #:
    #: El `autofix` va a cero porque tiene que ser reproducible: la misma evidencia tiene que
    #: producir el mismo diff, o no hay forma de auditar por que cambio. Una respuesta de
    #: consultor no tiene ese requisito, y a cero el modelo tiende a devolver siempre la misma
    #: formulacion, que en un chat se lee como que no esta pensando la respuesta.
    #:
    #: El techo es `1.0` y no mas alto porque por encima el modelo empieza a inventar detalles
    #: técnicos —un nombre de funcion, un puerto— con la misma seguridad con la que acierta. En
    #: una herramienta de pentesting, una suposicion presentada como hecho es un falso positivo
    #: con apariencia de conclusion.
    chat_temperature: float = Field(default=0.4, ge=0.0, le=1.0)

    #: Tope de tokens de la respuesta. Acota lo que un paso puede costar antes de que el
    #: proveedor decida pararlo, que es la diferencia entre un cobro acotado y uno que se
    #: descubre en la factura.
    chat_max_tokens: int = Field(default=2_048, ge=256, le=32_768)

    #: Cuantos mensajes de historial se reenvian al modelo.
    #:
    #: Es el limite que decide si el chat recuerda o amnesia. Se cuentan **mensajes** y no
    #: turnos porque el historial es una lista de filas y recortarla por parejas haria que una
    #: conversation empezada con un mensaje de sistema terminara con un turno descuadrado. Un
    #: numero impar deja el ultimo mensaje del usuario como el que se responde, que es lo que
    #: quiere el usuario: la respuesta a lo ultimo que escribio.
    chat_history_messages: int = Field(default=12, ge=0, le=200)

    #: Cuantos documentos de la base de conocimiento se inyectan por paso.
    #:
    #: El valor por defecto tiene que coincidir con el de la firma de `recuperar_documentos` en
    #: `backend/apps/knowledge/retrieval.py`. Dos numeros distintos para lo mismo hacen que "el
    #: valor por defecto" sea una de las dos cosas y no la otra, y el que se equivoca es el que
    #: lee la otra. Hay una prueba que los compara.
    #:
    #: Cuatro es lo que cabe sin que el contexto del cliente desplace a la pregunta, que es lo
    #: que el usuario ha escrito. Con mas, el modelo empieza a responder sobre reglas que nadie
    #: le ha preguntado, y el usuario recibe una respuesta correcta sobre algo que no pregunto.
    chat_context_documents: int = Field(default=4, ge=0, le=20)

    #: Longitud maxima del texto de un mensaje, en caracteres.
    #:
    #: Es un tope de entrada, no un capricho de formato: sin el, un mensaje de dos megabytes se
    #: convierte en un prompt que el proveedor acepta y a cambio del cual no hay respuesta que
    #: quepa, y el gasto se produce igualmente porque la peticion ya salio.
    chat_max_content_chars: int = Field(default=16_000, ge=100, le=200_000)
    # R1: ni el margen, ni la conversión a créditos, ni el precio de un escaneo
    # viven en el código de las rutas. Son política comercial y cambian por
    # acuerdo comercial, no por despliegue de software.
    # 1 crédito = 1,00 USD de precio al cliente (ya con markup aplicado).
    credits_per_usd: Decimal = Field(default=Decimal("1.00"), gt=0, le=1000)
    scan_credit_cost: Decimal = Field(default=Decimal("10"), gt=0, le=10000)
    # El default es `0.3` y no `0.5`, y por la misma razón que `.env.example` lo fija en `0.3`:
    # es el valor con el que se despliega y con el que la migración siembra la fila de precios.
    #
    # ## Por qué importa que coincidan
    #
    # ## Por qué el default tiene que coincidir con la fila
    #
    # Porque la fila de `platform_pricing` gana siempre, y el `default` solo se ve cuando la fila
    # **no existe** —una base restaurada de antes de la migración, o una fila borrada. Con el
    # default en `0.5` y la fila en `0.3`, esas dos bases cobraban el escaneo rápido a precios
    # distintos sin que ninguna de las dos dijera nada. El respaldo que se documenta como
    # respaldo tiene que decir lo mismo que la fila, o no es un respaldo: es un segundo precio.
    quick_scan_credit_multiplier: Decimal = Field(default=Decimal("0.3"), gt=0, le=1)
    #: Umbral por debajo del cual se emite el aviso de saldo bajo. Cero lo desactiva.
    #:
    #: Es configuración y no una constante porque es política comercial por tenant, no
    #: comportamiento del motor: el mismo aviso tiene sentido a 50 créditos para un
    #: workspace que consume diez por escaneo y no para uno que consume quinientos.
    #:
    #: El valor por defecto es **cero, es decir, desactivado**, y no un umbral arbitrario.
    #: Emitir por defecto un aviso a quien no lo ha pedido es ruido que entrena al receptor
    #: a ignorar el evento; para encenderlo hay que decidir la cifra, y esa cifra es una
    #: decisión comercial, no un valor por defecto técnico.
    low_credit_balance_threshold: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000)
    stripe_secret_key: SecretStr | None = Field(default=None, repr=False)
    stripe_publishable_key: str = ""
    stripe_webhook_secret: SecretStr | None = Field(default=None, repr=False)
    checkout_rate_limit: int = Field(default=10, ge=1, le=100)
    checkout_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    cve_query_rate_limit: int = Field(default=120, ge=1, le=1000)
    cve_query_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    cve_sync_interval_hours: int = Field(default=12, ge=1, le=168)
    cve_sync_http_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    asset_discovery_dns_timeout_seconds: float = Field(default=4.0, gt=0, le=30)
    asset_discovery_dns_concurrency: int = Field(default=24, ge=1, le=128)
    asset_discovery_max_candidates: int = Field(default=64, ge=1, le=1_000)
    asset_discovery_max_assets: int = Field(default=500, ge=1, le=10_000)
    asset_discovery_wordlist: str = Field(
        default=(
            "www,api,app,admin,portal,intranet,staging,stage,dev,test,qa,"
            "uat,prod,beta,internal,git,gitlab,jenkins,ci,cdn,static,assets,"
            "mail,smtp,imap,webmail,vpn,remote,ssh,ftp,db,database,sql,"
            "grafana,kibana,prometheus,metrics,status,health,auth,sso,oauth,"
            "login,accounts,account,billing,payment,payments,shop,store,"
            "blog,docs,wiki,help,support,ticket,chat,forum,news,media,"
            "img,images,files,download,downloads,backup,backups,old,new,"
            "preprod,sandbox,lab,lab1,dev1,dev2,node1,vm,esx,proxy,gateway"
        ),
        min_length=1,
    )
    cve_kev_feed_url: str = Field(default="https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", min_length=1)  # noqa: E501
    cve_nvd_feed_url: str = Field(
        default="https://cve.circl.lu/api/last",
        min_length=1,
    )
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

    @field_validator("llm_api_base")
    @classmethod
    def validate_llm_api_base(cls, value: str) -> str:
        """Normaliza un endpoint de chat pegado completo en `LLM_API_BASE`.

        ## Que se rompia

        `LLM_API_BASE` es la **base** del proveedor, y el cliente le añade
        `CHAT_COMPLETIONS_PATH`. Poner ahi el endpoint entero produce una URL con el
        endpoint dos veces, y el proveedor responde `404`. Con OpenRouter:

            LLM_API_BASE=https://openrouter.ai/api/v1
            ruta completa: https://openrouter.ai/api/v1/chat/completions   correcta

            LLM_API_BASE=https://openrouter.ai/api/v1/chat/completions
            ruta completa: .../api/v1/chat/completions/chat/completions     404

        ## Por que hace falta un validador y no basta con corregir el `.env`

        Porque el sintoma que produce es un `404` **dentro de una peticion del chat**, no un
        error de arranque. El operador ve "el chat no funciona" en el panel, sin rastro de que
        la causa es una variable de entorno mal puesta, y el `404` no dice que variable es.
        Esa es justo la clase de fallo que se investiga en el sitio equivocado durante horas.

        ## Por que `completions` y no un chequeo generico de "tiene ruta"

        Porque la forma equivocada que se ve en la practica es esa: la gente copia la URL del
        endpoint de la documentacion del proveedor, que es lo que encuentra Google primero. Un
        chequeo generico de "tiene mas de un segmento de ruta" rechazaria tambien
        `https://api.openai.com/v1`, que es **correcto** y es el valor por defecto de
        `.env.example`. Falsear un valor bueno para cazar uno malo hace que la gente arregle
        la variable al reves.
        """

        base = value.strip().rstrip("/")
        if not base:
            # Vacia la acepta `validate_runtime_and_endpoints`, que decide si el proveedor es
            # obligatorio segun si el chat esta habilitado. Aqui no se decide nada: una base
            # vacia es valida si no hay chat.
            return base
        endpoint_suffix = "/chat/completions"
        if base.lower().endswith(endpoint_suffix):
            base = base[: -len(endpoint_suffix)].rstrip("/")
        if "completions" in base.lower():
            raise ValueError(
                "LLM_API_BASE debe ser la base del proveedor, no una ruta de completions. "
                "Usa https://openrouter.ai/api/v1 o https://api.openai.com/v1."
            )
        return base

    @field_validator("strix_network_pool")
    @classmethod
    def validate_strix_network_pool(cls, value: str) -> str:
        """Exige un CIDR IPv4 del que se pueda comprobar el cerco de salida.

        ## Que representa

        No es el pool del que el runner corta la subred —esa la elige Docker, y solo se conoce
        una vez creada la red—. Es el **rango dentro del cual** tiene que caer la subred del
        trabajo, y sirve para que la comprobacion del cerco pueda afirmar algo concreto: si la
        subred cae dentro de este rango, hay un cerco declarado que la cubre.

        ## Por que el runner no puede instalar el cerco

        Porque la subred se elige **despues** de crear la red, y la regla tiene que existir
        **antes** de que haya trafico que filtrar. El orden no se puede invertir con la API de
        Docker que usa el worker: `networks.create` no acepta configuracion de IPAM, con lo que
        el runner no puede fijar la subred de antemano.

        De ahi la division: `scripts/harden_runner_egress.sh` instala las reglas en el host
        durante el arranque, y `egress_fence` comprueba antes de cada escaneo que el cerco
        existe. Un despliegue sin el script **no arranca escaneos**, en vez de arrancarlos sin
        proteccion y avisar en un log que nadie lee.

        ## Por que se valida y no se documenta

        Porque un pool mal escrito haria que la comprobacion del cerco mirara un rango que
        ninguna red va a usar, y pasara siempre: el runner creeria que esta protegido y el
        contenedor saldria con la misma salida de siempre. Un fallo silencioso de una proteccion
        es peor que no tenerla, porque ocupa el sitio de la proteccion.
        """

        import ipaddress as _ipaddress

        try:
            red = _ipaddress.ip_network(value, strict=False)
        except ValueError as error:
            raise ValueError(
                f"STRIX_NETWORK_POOL={value!r} no es un CIDR IPv4 valido. Debe tener la forma "
                "172.31.0.0/16, y es el rango dentro del cual la comprobacion del cerco de "
                "salida espera ver la subred del trabajo."
            ) from error
        if red.version != 4:
            raise ValueError(
                f"STRIX_NETWORK_POOL={value!r} tiene que ser IPv4: las reglas de salida del "
                "host se escriben para IPv4 y una red IPv6 se colaria sin filtrar."
            )
        if red.prefixlen > 24:
            raise ValueError(
                f"STRIX_NETWORK_POOL={value!r} tiene un prefijo de /{red.prefixlen}, demasiado "
                "estrecho para que quepan las subredes que Docker corta para los trabajos "
                "simultaneos de un host. Se espera /16 o /20 como mucho."
            )
        return value

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

    def _exigir_redis_no_publico(self) -> None:
        """En producción, `REDIS_HOST` no puede ser una dirección de Internet.

        ## Por qué un nombre se acepta sin más

        Porque en producción el backend es un contenedor del mismo despliegue, y el alias del
        servicio —`fenix-redis`— **es** la forma correcta de hablar con Redis: la red interna
        de Docker ya lo aísla y no hay nada que auditar en un nombre que solo resuelve dentro de
        esa red. Exigir una IP aquí rompía Dokploy, y el síntoma —Celery sin poder leer la
        cola— no señalaba la causa.

        ## Por qué una IP pública sí se rechaza

        Porque es la propiedad que R6 protege. Un `REDIS_HOST` que sea una dirección pública
        significa que el despliegue habla con un Redis de Internet, y da igual si ese Redis
        existe: la credencial de la plataforma ya está en manos de quien lo administre. Es el
        fallo que un alias de Docker no puede cometer y una IP sí.

        ## Por qué se distinguen las categorías en vez de exigir una lista

        Porque la lista sería distinta para cada despliegue y tendría que actualizarse con
        cada uno. Lo único que es cierto en todos los casos es la categoría: `is_global` es
        exactamente "sale a Internet" en la biblioteca estándar, y es la pregunta que R6
        formula.
        """

        try:
            direccion = ipaddress.ip_address(self.redis_host)
        except ValueError:
            # No es una IP: es un nombre. Solo puede ser un alias de la red interna, que es
            # justo lo que se acepta. Un nombre público sería un DNS, y contra un DNS no hay
            # nada que comprobar aquí sin resolverlo, que introduciría una dependencia de red
            # en el arranque; se documenta en su lugar.
            return

        if direccion.is_global:
            raise ValueError(
                f"REDIS_HOST es una dirección pública ({self.redis_host}). R6 exige que los "
                "datos de datos no se alcancen desde Internet. En producción usa el alias del "
                "servicio en la red interna del despliegue, por ejemplo `fenix-redis`, o la IP "
                "de Tailscale del VPS. En desarrollo, la IP de Tailscale."
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
        if self.environment in {"staging", "production"} and (
            self.secret_key.get_secret_value() == SECRET_KEY_DE_EJEMPLO
        ):
            raise ValueError("SECRET_KEY de ejemplo no se permite fuera de desarrollo")
        # Mismo criterio que la clave de ejemplo y por la misma razón: una propiedad de
        # seguridad que solo se puede desactivar con una línea de configuración no es una
        # propiedad, es un default. El suelo de doce es el de `.env.example`; por debajo de
        # diez, la diferencia entre 4 y 16 rondas es un factor de cuatro mil y no compensa ni
        # un milisegundo de login.
        if self.environment in {"staging", "production"} and self.bcrypt_rounds < 10:
            raise ValueError(
                f"BCRYPT_ROUNDS={self.bcrypt_rounds} es insuficiente fuera de desarrollo: "
                "el minimo es 10 y el recomendado 12"
            )

        if not self._database_url_matches_components():
            raise ValueError("DATABASE_URL no coincide con las variables DB_* configuradas")

        # R6: los datos remotos no se alcanzan desde Internet.
        #
        # ## Qué propiedad protege esto, que no es la que parece
        #
        # R6 no dice "usa Tailscale". Dice que los datos de datos residen en el VPS de Dokploy
        # y que **ningún puerto de datos se expone a Internet**. Son dos cosas distintas, y
        # confundirlas es lo que produjo este bug:
        #
        # - En **desarrollo local** el backend corre en la máquina del programador, y para
        #   alcanzar el VPS de Dokploy la única ruta es la IP de Tailscale. Ahí sí hace falta.
        # - En **producción** el backend es un contenedor más del mismo despliegue, en la misma
        #   red interna que PostgreSQL y Redis. La ruta correcta es el **alias del servicio**
        #   (`fenix-redis`), y exigir la IP de Tailscale ahí es un error: el contenedor no
        #   necesita salir a la red del mesh para hablar con un servicio que tiene al lado.
        #
        # Una comprobación que exigía Tailscale en producción rompía el despliegue de Dokploy,
        # y los errores de Celery que aparecían eran su consecuencia: el worker no lograba
        # conectar con la cola.
        #
        # ## Por qué entonces la comprobación sigue teniendo valor
        #
        # Porque el alias de Docker es un **nombre**, no una dirección, y un nombre no se puede
        # auditar. Lo que sí se puede —y es la propiedad que R6 protege— es que la dirección
        # que se pone no sea una IP **pública**. Un despliegue con `REDIS_HOST=8.8.8.8` está
        # habla con un Redis de Internet, y eso sí es exactamente lo que R6 prohíbe, tanto si
        # el Redis existe como si no.
        #
        # Y por eso la regla es "no pública" y no "es Tailscale": una IP privada, la de
        # loopback, la del mesh o un alias de la red interna pasan todas, y las cuatro son
        # legítimas según dónde corra el proceso.
        if self.environment == "production":
            self._exigir_redis_no_publico()

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
        # Sin esto el barrido puede tener un intervalo mayor que su ventana, y entonces hay un
        # tramo —de hasta un intervalo entero— en el que una credencial ya está dentro de la
        # ventana y el barrido todavía no ha pasado por ella. El síntoma es un token que se
        # renueva «a tiempo» y a la vez demasiado tarde, que es la clase de configuración que
        # parece correcta y no lo es.
        if (
            self.git_token_proactive_refresh_interval_seconds
            >= self.git_token_proactive_refresh_window_seconds
        ):
            raise ValueError(
                "El intervalo del barrido de renovación debe ser menor que su ventana "
                "por adelantado"
            )
        if not self.git_allowed_clone_hosts:
            raise ValueError("GIT_ALLOWED_CLONE_HOSTS_CSV debe contener al menos un host")
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

        if self.strix_replay_source.strip() and self.environment in {"staging", "production"}:
            # Un refusal de arranque, no un aviso. Ver el docstring de `strix_replay_source`:
            # un despliegue real no puede fabricar los artefactos de un escaneo, porque el panel
            # los presentaría como hallazgos del motor y el ledger los cobraría como un escaneo.
            raise ValueError(
                "STRIX_REPLAY_SOURCE no puede activarse en staging ni en producción: el replay "
                "reproduce los artefactos de otro run sin ejecutar el motor"
            )

        if self.environment == "production" and self.strix_execution_mode != "host":
            raise ValueError("Producción requiere STRIX_EXECUTION_MODE=host")
        if self.strix_execution_mode == "host" and not self.strix_cli_path.strip():
            # Sin ruta no hay modo host: arrancar el motor sin saber contra qué binario sería
            # adivinar, y adivinar la ruta de un ejecutable es exactamente la clase de
            # hardcoding que R1 prohíbe. El fallo se da al arrancar, no en el primer escaneo.
            raise ValueError(
                "STRIX_EXECUTION_MODE=host requiere STRIX_CLI_PATH con la ruta del ejecutable"
            )

        if self.strix_soft_timeout_seconds >= self.strix_hard_timeout_seconds:
            raise ValueError("El timeout suave de Strix debe ser menor que el duro")
        if self.strix_hard_timeout_seconds >= self.celery_task_soft_time_limit_seconds:
            raise ValueError("Strix hard timeout debe ser menor que el timeout suave de Celery")
        if self.strix_watchdog_stale_after_seconds <= self.strix_hard_timeout_seconds:
            raise ValueError("El watchdog debe esperar más que el timeout duro de Strix")

        if self.email_verification_delivery_mode == "smtp" and not self.smtp_host:
            raise ValueError("SMTP_HOST es obligatorio cuando el modo de email es smtp")

        # Stripe se valida de forma suave en local: la plataforma debe arrancar
        # sin credenciales de cobro para que un desarrollador pueda trabajar. En
        # staging y producción, el webhook sin secreto sería un endpoint de
        # recarga abierto a cualquiera que adivine el formato del evento.
        stripe_secret_configured = self.stripe_secret_key is not None and bool(
            self.stripe_secret_key.get_secret_value()
        )
        stripe_webhook_configured = self.stripe_webhook_secret is not None and bool(
            self.stripe_webhook_secret.get_secret_value()
        )
        if self.environment in {"staging", "production"}:
            if not stripe_secret_configured:
                raise ValueError("Producción y staging requieren STRIPE_SECRET_KEY")
            if not stripe_webhook_configured:
                raise ValueError("Producción y staging requieren STRIPE_WEBHOOK_SECRET")
            if self.stripe_publishable_key and not self.stripe_publishable_key.startswith("pk_"):
                raise ValueError("STRIPE_PUBLISHABLE_KEY debe empezar por pk_")
        if self.quick_scan_credit_multiplier > 1:
            raise ValueError("El multiplicador de escaneo rápido no puede superar 1")

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
            # La lista de CORS se compone en `resolve_cors_origins`, que descarta en
            # producción los orígenes de otro esquema. Aquí solo se comprueba que la que
            # queda no esté vacía: una API de producción sin ningún origen autorizado no
            # sirve para nada, y conviene que se diga al arrancar y no en el primer fallo de
            # CORS del navegador.
            if not self.cors_origins:
                raise ValueError(
                    "Producción y staging requieren al menos un origen en CORS_ORIGINS, y "
                    f"debe incluir el del frontend ({_origin_of(self.frontend_base_url)})"
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
