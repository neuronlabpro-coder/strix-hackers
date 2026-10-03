"""Primitivas OAuth firmadas para conectores Git de GitHub y GitLab."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from backend.apps.repositories.models import GitProviderEnum
from backend.core.config import settings

_STATE_VERSION = 1


class OAuthError(RuntimeError):
    """Error controlado del flujo OAuth Git."""


class OAuthStateError(OAuthError):
    """El state no es válido, expiró o no corresponde al proveedor."""


class OAuthConfigurationError(OAuthError):
    """El proveedor OAuth no está configurado en el entorno."""


class OAuthExchangeError(OAuthError):
    """El proveedor rechazó el canje del código temporal."""


class OAuthRefreshError(OAuthError):
    """No se pudo renovar el token de acceso de una credencial ya conectada."""


class OAuthRefreshRejectedError(OAuthRefreshError):
    """El proveedor rechazó el `refresh_token`: la única salida es volver a conectar."""


class OAuthRefreshUnavailableError(OAuthRefreshError):
    """El proveedor no respondió al refresco. Es transitorio y se puede reintentar."""


@dataclass(frozen=True, slots=True)
class OAuthProviderSettings:
    """Configuración no secreta y secreta mínima de un proveedor OAuth."""

    provider: GitProviderEnum
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OAuthState:
    """Claims firmados que vinculan un callback con tenant y usuario."""

    provider: GitProviderEnum
    organization_id: UUID
    user_id: UUID
    nonce: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """Token efímero devuelto por el proveedor y nunca registrado en logs."""

    access_token: str
    refresh_token: str | None
    expires_in: int | None


@dataclass(frozen=True, slots=True)
class OAuthAuthorizeResponse:
    """URL de consentimiento para clientes XHR que ya autenticaron la sesión."""

    authorization_url: str
    expires_in: int


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(char in value for char in "/+="):
        raise OAuthStateError("State OAuth mal codificado")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise OAuthStateError("State OAuth mal codificado") from error


def _sign(payload: bytes) -> str:
    return _b64encode(
        hmac.new(
            settings.secret_key.get_secret_value().encode("utf-8"),
            payload,
            hashlib.sha256,
        ).digest()
    )


def create_oauth_state(
    provider: GitProviderEnum,
    organization_id: UUID,
    user_id: UUID,
    *,
    now: datetime | None = None,
) -> str:
    """Crea un state HMAC con expiración y nonce de un solo uso."""

    current_time = now or datetime.now(UTC)
    payload = {
        "v": _STATE_VERSION,
        "p": provider.value,
        "o": str(organization_id),
        "u": str(user_id),
        "n": secrets.token_urlsafe(32),
        "e": int(current_time.timestamp()) + settings.oauth_state_ttl_seconds,
    }
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign(encoded_payload.encode("ascii"))
    return f"{encoded_payload}.{signature}"


def decode_oauth_state(
    state: str,
    expected_provider: GitProviderEnum,
    *,
    now: datetime | None = None,
) -> OAuthState:
    """Valida firma, proveedor y expiración antes de consumir Redis."""

    try:
        encoded_payload, provided_signature = state.split(".", 1)
    except ValueError as error:
        raise OAuthStateError("State OAuth inválido") from error
    expected_signature = _sign(encoded_payload.encode("ascii"))
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise OAuthStateError("Firma de state OAuth inválida")
    try:
        payload = cast(dict[str, object], json.loads(_b64decode(encoded_payload)))
        provider = GitProviderEnum(payload["p"])
        organization_id = UUID(str(payload["o"]))
        user_id = UUID(str(payload["u"]))
        nonce = payload["n"]
        expires_at = payload["e"]
        version = payload["v"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OAuthStateError("Claims de state OAuth inválidos") from error
    if version != _STATE_VERSION or provider != expected_provider:
        raise OAuthStateError("El state no corresponde al proveedor")
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise OAuthStateError("Nonce de state OAuth inválido")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise OAuthStateError("Expiración de state OAuth inválida")
    current_time = now or datetime.now(UTC)
    if expires_at <= int(current_time.timestamp()):
        raise OAuthStateError("El state OAuth expiró")
    return OAuthState(provider, organization_id, user_id, nonce, expires_at)


def oauth_state_key(state: str) -> str:
    """Devuelve una clave Redis que no expone el state en comandos de infraestructura."""

    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return f"fenix:oauth-state:{digest}"


def get_oauth_provider_settings(provider: GitProviderEnum) -> OAuthProviderSettings:
    """Construye la configuración de un provider supported."""

    if provider == GitProviderEnum.GITHUB:
        client_id = settings.github_oauth_client_id
        secret = settings.github_oauth_client_secret
        return OAuthProviderSettings(
            provider=provider,
            client_id=client_id,
            client_secret=secret.get_secret_value() if secret is not None else "",
            authorize_url=settings.github_oauth_authorize_url,
            token_url=settings.github_oauth_token_url,
            scopes=tuple(settings.github_oauth_scopes.split()),
        )
    if provider == GitProviderEnum.GITLAB:
        client_id = settings.gitlab_oauth_client_id
        secret = settings.gitlab_oauth_client_secret
        return OAuthProviderSettings(
            provider=provider,
            client_id=client_id,
            client_secret=secret.get_secret_value() if secret is not None else "",
            authorize_url=settings.gitlab_oauth_authorize_url,
            token_url=settings.gitlab_oauth_token_url,
            scopes=tuple(settings.gitlab_oauth_scopes.split()),
        )
    raise OAuthConfigurationError("El proveedor OAuth no está soportado")


def validate_oauth_provider_settings(config: OAuthProviderSettings) -> None:
    """Falla cerrado si client ID o client secret no están configurados."""

    if not config.client_id or not config.client_secret:
        raise OAuthConfigurationError("El proveedor OAuth no está configurado")
    for url in (config.authorize_url, config.token_url):
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not parsed.host:
            raise OAuthConfigurationError("El proveedor OAuth debe usar URLs HTTPS")


def build_authorization_url(
    config: OAuthProviderSettings,
    state: str,
    redirect_uri: str,
) -> str:
    """Construye la URL de consentimiento codificando todos los parámetros."""

    parsed = urlsplit(config.authorize_url)
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(config.scopes),
            "state": state,
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _token_from_payload(payload: object, error: type[OAuthError]) -> OAuthToken:
    """Normaliza el cuerpo de un token OAuth sin repetir el análisis en dos sitios.

    ## Por qué el tipo de error es un parámetro y no uno fijo

    Porque los dos llamadores leen el mismo cuerpo pero lo que significa un cuerpo malo no es
    lo mismo: en el canje de un código es un canje que no se pudo hacer, y en un refresco es una
    credencial que hay que volver a conectar. Fijar el error dentro del analizador obligaría a
    traducir el resultado en el llamador —que es donde no se puede— y a guardar la razón del
    fallo dentro de una excepción que después se traduce a un código HTTP.
    """

    if not isinstance(payload, dict):
        raise error("El proveedor devolvió un token OAuth inválido")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise error("El proveedor no devolvió access token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_in=(
            expires_in
            if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0
            else None
        ),
    )


async def exchange_oauth_code(
    config: OAuthProviderSettings,
    code: str,
    redirect_uri: str,
) -> OAuthToken:
    """Canjea un código temporal sin incluir el secreto o token en excepciones."""

    validate_oauth_provider_settings(config)
    if not code or len(code) > 2048:
        raise OAuthExchangeError("El código OAuth no es válido")
    try:
        async with httpx.AsyncClient(
            timeout=settings.git_api_timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                config.token_url,
                data={
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise OAuthExchangeError("El proveedor rechazó el canje OAuth") from error
    return _token_from_payload(payload, OAuthExchangeError)


async def refresh_oauth_token(
    config: OAuthProviderSettings,
    refresh_token: str,
) -> OAuthToken:
    """Renueva el token de acceso con el `refresh_token` de una credencial guardada.

    ## Qué flujo OAuth usa el producto y por qué el endpoint es el mismo

    El producto registra una **OAuth App**, no una GitHub App: `authorize` manda `client_id`,
    `scope` y `state` a `settings.github_oauth_authorize_url`, y el canje es
    `grant_type=authorization_code` contra `settings.github_oauth_token_url` con `client_id` y
    `client_secret`. No hay `installation_id`, ni clave privada, ni firma JWT en ningún sitio:
    `installation_id` está en `git_credentials` desde la migración de la fase 3 y sigue a `NULL`
    en las dos filas que hay en la base. Por eso el refresco es el de OAuth App y no el de GitHub
    App.

    ## Por qué el endpoint es el de `token_url` y no un URL nuevo

    Porque los dos proveedores usan **el mismo** para canjear y para refrescar:
    `POST https://github.com/login/oauth/access_token` y `POST https://gitlab.com/oauth/token`, los
    dos, con `grant_type=refresh_token`. Añadir un `github_oauth_refresh_url` a la configuración
    sería un parámetro que solo puede tener un valor y cuyo error —apuntar al sitio equivocado— no
    lo detectaría nadie hasta que una integración dejara de refrescar en producción.

    ## Por qué no se llama a `raise_for_status`

    Porque **GitHub contesta `200` con un cuerpo de error**. Un `refresh_token` revocado produce
    `{"error": "bad_refresh_token", ...}` con código `200`, así que `raise_for_status()` no
    dispararía nada y el cuerpo se leería como un token válido. Por eso el estado y el cuerpo se
    miran por separado, y el cuerpo es el que manda.

    ## Por qué el cuerpo se mira antes que el estado

    Por lo mismo, y por la costumbre de GitLab, que sí usa `4xx`. Se decide en este orden:
    `5xx` o red caída es transitorio; un cuerpo que no es un objeto JSON es transitorio también;
    un `error` en el cuerpo es rechazo; cualquier otro estado que no sea `2xx` es rechazo.

    ## Por qué `expires_in` ausente no es un token sin caducidad

    Porque `token_expires_at` a `None` significa, en esta tabla, «token personal de acceso», que es
    justo lo que este camino no debe producir: una credencial OAuth sin fecha dejaría de refrescar
    para siempre y volvería al síntoma que esto arregla. `token_refresh.py` lo detecta y avisa.

    ## Por qué ningún secreto entra en el mensaje

    Porque estas excepciones se encadenan con `from error` y acaban en un log o en un `detail`. El
    cuerpo del proveedor se inspecciona pero **no** se copia: solo se propaga el tipo de error de
    `httpx`, que no contiene ni la petición ni el cuerpo.
    """

    validate_oauth_provider_settings(config)
    # El límite de longitud es la misma guarda de forma que el canje de un código: un valor que no
    # cabe en una credencial legítima no es una credencial, y mandarlo a GitHub sería regalarle el
    # `client_secret` de la plataforma a un endpoint que no lo necesita para contestar.
    if not refresh_token or len(refresh_token) > 4096:
        raise OAuthRefreshRejectedError("El refresh token no es utilizable")
    try:
        async with httpx.AsyncClient(
            timeout=settings.git_api_timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                config.token_url,
                data={
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as error:
        raise OAuthRefreshUnavailableError(
            "El proveedor no respondió al intento de refresco"
        ) from error
    if response.status_code >= 500:
        raise OAuthRefreshUnavailableError("El proveedor falló al refrescar el token")
    try:
        payload = response.json()
    except ValueError as error:
        raise OAuthRefreshUnavailableError(
            "El proveedor devolvió una respuesta ilegible al refresco"
        ) from error
    if not isinstance(payload, dict) or not payload:
        raise OAuthRefreshUnavailableError("El proveedor devolvió un cuerpo vacío al refresco")
    codigo_de_error = payload.get("error")
    if codigo_de_error:
        raise OAuthRefreshRejectedError(
            f"El proveedor rechazó el refresh token: {_codigo_de_error(codigo_de_error)}"
        )
    if not 200 <= response.status_code < 300:
        raise OAuthRefreshRejectedError("El proveedor rechazó el refresh token")
    return _token_from_payload(payload, OAuthRefreshRejectedError)


def _codigo_de_error(valor: object) -> str:
    """Reduce el `error` del proveedor a un slug corto y sin comillas raras.

    ## Por qué se acota a 64 caracteres

    Porque este texto acaba en un log. El campo `error` de GitHub y GitLab es un vocabulario
    cerrado —`bad_refresh_token`, `invalid_grant`, `incorrect_client_credentials`—, pero el campo
    es texto libre para quien lo escriba, y un log no es el sitio donde se copia lo que contesta
    un tercero. El `error_description` **no** se propaga nunca: es el campo donde un proveedor con
    una configuración equivocada, o un intermediario, repetiría el valor enviado.
    """

    if not isinstance(valor, str):
        return "sin_codigo"
    limpio = "".join(caracter for caracter in valor if caracter.isalnum() or caracter in "_-.")
    return (limpio or "sin_codigo")[:64]
