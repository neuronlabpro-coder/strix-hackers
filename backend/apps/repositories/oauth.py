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
    if not isinstance(payload, dict):
        raise OAuthExchangeError("El proveedor devolvió un token OAuth inválido")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthExchangeError("El proveedor no devolvió access token")
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
