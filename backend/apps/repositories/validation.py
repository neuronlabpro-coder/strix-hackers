"""Validación canónica de referencias Git compartida por conectores y workspaces."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from backend.core.config import settings

_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40,64}")


class GitReferenceError(ValueError):
    """Una referencia Git recibida del proveedor no cumple las reglas de seguridad."""


def validate_git_branch(value: str, *, field_name: str = "la rama") -> str:
    """Rechaza refs que Git interpretaría como opción, ruta o secuencia insegura."""

    if (
        not value
        or len(value) > 255
        or value.startswith("-")
        or value.endswith((".", "/"))
        or value in {".", ".."}
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(char.isspace() or ord(char) < 32 for char in value)
        or any(char in value for char in "~^:?*[\\")
        or value.endswith(".lock")
    ):
        raise GitReferenceError(f"Nombre de {field_name} inválido")
    return value


def validate_git_commit_sha(value: str) -> str:
    """Normaliza a minúsculas un SHA de 40 o 64 caracteres hexadecimales."""

    if not _COMMIT_SHA_PATTERN.fullmatch(value):
        raise GitReferenceError("El commit SHA no es válido")
    return value.lower()


def validate_git_clone_url(clone_url: str) -> str:
    """Exige HTTPS sin credenciales y un host presente en la allowlist operacional."""

    try:
        parsed = urlsplit(clone_url)
    except ValueError as error:
        raise GitReferenceError("La URL de clonado no es válida") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GitReferenceError("La URL de clonado no es segura")
    if parsed.hostname.lower() not in settings.git_allowed_clone_hosts:
        raise GitReferenceError("El host de clonado no está autorizado")
    return clone_url
