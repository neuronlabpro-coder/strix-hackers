"""Normalización del inventario de repositorios devuelto por cada proveedor Git."""

from __future__ import annotations

from dataclasses import dataclass

from backend.apps.repositories.models import GitProviderEnum
from backend.apps.repositories.validation import (
    GitReferenceError,
    validate_git_branch,
    validate_git_clone_url,
)
from backend.core.config import settings


class RemoteRepositoryError(ValueError):
    """El proveedor devolvió metadatos de repositorio inservibles o inseguros."""


@dataclass(frozen=True, slots=True)
class NormalizedRepository:
    """Metadatos canónicos de un repositorio remoto ya validados."""

    remote_repo_id: str
    name: str
    full_name: str
    clone_url: str
    default_branch: str
    is_private: bool


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise RemoteRepositoryError(f"El proveedor no devolvió el campo {key}")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)
    ):
        raise RemoteRepositoryError(f"El campo {key} tiene un formato inválido")
    return cleaned


def _remote_id(payload: dict[str, object]) -> str:
    value = payload.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RemoteRepositoryError("El proveedor no devolvió un identificador de repositorio")
    remote_id = str(value).strip()
    if not remote_id or not remote_id.isdigit() or len(remote_id) > 128:
        raise RemoteRepositoryError("El identificador de repositorio remoto no es válido")
    return remote_id


def _default_branch(payload: dict[str, object]) -> str:
    raw_branch = payload.get("default_branch")
    if not isinstance(raw_branch, str) or not raw_branch.strip():
        return "main"
    try:
        return validate_git_branch(raw_branch.strip(), field_name="la rama por defecto")
    except GitReferenceError as error:
        raise RemoteRepositoryError(
            "El proveedor devolvió una rama por defecto inválida"
        ) from error


def _clone_url(payload: dict[str, object], key: str) -> str:
    clone_url = _text(payload, key)
    try:
        return validate_git_clone_url(clone_url)
    except GitReferenceError as error:
        raise RemoteRepositoryError("La URL de clonado remota no es segura") from error


def _is_private(payload: dict[str, object]) -> bool:
    private_flag = payload.get("private")
    if isinstance(private_flag, bool):
        return private_flag
    visibility = payload.get("visibility")
    if isinstance(visibility, str):
        return visibility.casefold() in {"private", "internal"}
    return False


def normalize_remote_repository(
    provider: GitProviderEnum,
    payload: dict[str, object],
) -> NormalizedRepository:
    """Traduce la respuesta heterogénea de GitHub o GitLab a un contrato único."""

    if provider == GitProviderEnum.GITHUB:
        return NormalizedRepository(
            remote_repo_id=_remote_id(payload),
            name=_text(payload, "name"),
            full_name=_text(payload, "full_name"),
            clone_url=_clone_url(payload, "clone_url"),
            default_branch=_default_branch(payload),
            is_private=_is_private(payload),
        )
    if provider == GitProviderEnum.GITLAB:
        return NormalizedRepository(
            remote_repo_id=_remote_id(payload),
            name=_text(payload, "name"),
            full_name=_text(payload, "path_with_namespace"),
            clone_url=_clone_url(payload, "http_url_to_repo"),
            default_branch=_default_branch(payload),
            is_private=_is_private(payload),
        )
    raise RemoteRepositoryError("El proveedor todavía no está soportado")


def webhook_callback_url(provider: GitProviderEnum) -> str:
    """Construye la URL pública que el proveedor debe usar para entregar eventos."""

    base = settings.api_public_base_url.rstrip("/")
    return f"{base}/api/v1/webhooks/git/{provider.value.lower()}"


def webhook_subscription_events() -> tuple[str, ...]:
    """Eventos que la plataforma necesita para revisar PRs y operar por ChatOps."""

    events = tuple(
        event.strip()
        for event in settings.git_webhook_subscription_events.split(",")
        if event.strip()
    )
    if not events:
        raise RemoteRepositoryError("No hay eventos de webhook configurados")
    return events
