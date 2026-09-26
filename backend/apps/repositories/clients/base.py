"""Contrato común para clientes Git autenticados."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from backend.apps.repositories.models import GitProviderEnum
from backend.core.config import settings


class GitClientError(RuntimeError):
    """Error sanitizado al comunicar con un proveedor Git."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class GitRateLimitError(GitClientError):
    """El proveedor Git agotó su cuota temporal."""


class GitServerError(GitClientError):
    """El proveedor Git devolvió un error temporal 5xx."""


def optional_text(value: object) -> str | None:
    """Normaliza un campo de texto que el proveedor puede mandar vacío o ausente.

    GitLab devuelve `"public_email": null` y omite `email` si al token le falta el
    ámbito `read_user`. Tratar ambos casos como cadena vacía haría que la interfaz
    mostrara un correo en blanco con formato de correo.
    """

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class GitUserIdentity:
    """Identidad de la cuenta a la que pertenece una credencial.

    Normalizada porque GitHub y GitLab no coinciden ni en el nombre del campo de
    usuario (`login` frente a `username`) ni en su disponibilidad: el correo de
    GitLab solo aparece si el token tiene el ámbito `read_user`, así que es
    opcional por diseño y no por descuido.
    """

    provider_user_id: str
    login: str
    display_name: str | None = None
    email: str | None = None
    avatar_url: str | None = None


class BaseGitClient(ABC):
    """Interfaz de operaciones que un conector debe ofrecer al orquestador."""

    def __init__(
        self,
        provider: GitProviderEnum,
        access_token: str,
        organization_id: UUID,
        api_base_url: str,
        http_client: httpx.Client | None = None,
        allowed_repo_full_name: str | None = None,
    ) -> None:
        if not access_token:
            raise GitClientError("El token Git no puede estar vacío")
        parsed_api_url = urlsplit(api_base_url)
        if parsed_api_url.username or parsed_api_url.password:
            raise GitClientError("La URL del proveedor no puede contener credenciales")
        if settings.environment in {"staging", "production"} and parsed_api_url.scheme != "https":
            raise GitClientError("El proveedor Git debe usar HTTPS en producción")
        self.provider = provider
        self.organization_id = organization_id
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.allowed_repo_full_name = allowed_repo_full_name
        self.http_client = http_client or httpx.Client(
            base_url=self.api_base_url,
            timeout=settings.git_api_timeout_seconds,
        )

    def _validate_repo_reference(self, value: str) -> None:
        if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise GitClientError("Referencia de repositorio inválida")
        if self.allowed_repo_full_name is not None and value != self.allowed_repo_full_name:
            raise GitClientError("El cliente solo puede operar sobre su repositorio")

    @staticmethod
    def _validate_pr_number(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GitClientError("Número de Pull Request inválido")

    @staticmethod
    def _validate_status(state: str) -> None:
        if state not in {"pending", "success", "failure", "error"}:
            raise GitClientError("Estado de commit inválido")

    @staticmethod
    def _validate_remote_repo_id(remote_repo_id: str) -> None:
        if (
            not remote_repo_id
            or len(remote_repo_id) > 128
            or not remote_repo_id.isdigit()
        ):
            raise GitClientError("Identificador de repositorio remoto inválido")

    def _validate_webhook_target(self, callback_url: str, secret: str) -> None:
        parsed = urlsplit(callback_url)
        if (
            parsed.scheme != "https"
            and settings.environment in {"staging", "production"}
        ) or not parsed.hostname or parsed.username or parsed.password:
            raise GitClientError("La URL de webhook no es válida")
        if len(secret) < 32 or len(secret) > 128 or any(ord(char) < 33 for char in secret):
            raise GitClientError("El secreto de webhook no cumple los requisitos")

    @abstractmethod
    def list_repositories(self) -> list[dict[str, object]]:
        """Lista repositorios visibles para la credencial."""

    @abstractmethod
    def get_clone_token(self) -> str:
        """Devuelve el token temporal que debe existir solo en memoria."""

    @abstractmethod
    def get_authenticated_user(self) -> GitUserIdentity:
        """Devuelve la identidad de la cuenta a la que pertenece la credencial.

        Es la única forma de saber si un token es válido sin esperar a una
        sincronización: un PAT con formato válido pero revocado se acepta en el borde
        y falla tres horas después, en mitad de un escaneo. También es lo que impide
        conectar el repositorio equivocado: el panel muestra a quién pertenece la
        credencial antes de que el usuario sincronice nada.
        """

    @abstractmethod
    def get_repository(self, remote_repo_id: str) -> dict[str, object]:
        """Obtiene los metadatos canónicos de un repositorio por su id remoto."""

    @abstractmethod
    def create_webhook(
        self,
        repo_full_name: str,
        callback_url: str,
        secret: str,
        events: tuple[str, ...],
    ) -> str:
        """Registra el webhook HMAC del repositorio y devuelve su identificador."""

    @abstractmethod
    def delete_webhook(self, repo_full_name: str, webhook_id: str) -> None:
        """Elimina el webhook registrado previamente; 404 se considera éxito."""

    @abstractmethod
    def set_commit_status(
        self,
        repo_full_name: str,
        sha: str,
        state: str,
        description: str,
        target_url: str,
    ) -> None:
        """Publica el estado de seguridad de un commit."""

    @abstractmethod
    def post_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> str:
        """Publica un comentario y devuelve su identificador."""

    @abstractmethod
    def update_pr_comment(
        self,
        repo_full_name: str,
        comment_id: str,
        body: str,
    ) -> None:
        """Actualiza un comentario previously publicado."""

    @abstractmethod
    def has_write_access(self, repo_full_name: str, username: str) -> bool:
        """Comprueba permiso de escritura sin revelar credenciales."""

    @abstractmethod
    def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, object]:
        """Obtiene el head y base actuales del Pull/Merge Request."""

    @abstractmethod
    def create_autofix_branch_and_pr(
        self,
        repo_full_name: str,
        base_branch: str,
        branch_name: str,
        patch_diff: str,
        title: str,
    ) -> str:
        """Crea una rama, aplica un patch y devuelve la URL del PR/MR."""

    @staticmethod
    def _raise_http_error(error: httpx.HTTPStatusError) -> None:
        status_code = error.response.status_code
        retry_after: int | None = None
        raw_retry_after = error.response.headers.get("Retry-After")
        if raw_retry_after is not None:
            try:
                retry_after = max(int(raw_retry_after), 1)
            except ValueError:
                retry_after = None
        if status_code == 429:
            raise GitRateLimitError(
                "El proveedor Git agotó su cuota temporal",
                status_code=status_code,
                retry_after=retry_after,
            ) from error
        if status_code >= 500:
            raise GitServerError(
                "El proveedor Git devolvió un error temporal",
                status_code=status_code,
                retry_after=retry_after,
            ) from error
        raise GitClientError(
            "El proveedor Git rechazó la operación",
            status_code=status_code,
            retry_after=retry_after,
        ) from error

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            request_url = (
                path if path.startswith("http") else f"{self.api_base_url}/{path.lstrip('/')}"
            )
            response = self.http_client.request(
                method,
                request_url,
                json=json,
                headers=headers,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            self._raise_http_error(error)
            raise AssertionError("unreachable") from error
        except httpx.HTTPError as error:
            raise GitClientError("No se pudo contactar con el proveedor Git") from error

    def _request_optional(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """Permite distinguir recursos ausentes (404) de fallos del proveedor."""

        try:
            request_url = (
                path if path.startswith("http") else f"{self.api_base_url}/{path.lstrip('/')}"
            )
            response = self.http_client.request(
                method,
                request_url,
                json=json,
                headers=headers,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            self._raise_http_error(error)
            raise AssertionError("unreachable") from error
        except httpx.HTTPError as error:
            raise GitClientError("No se pudo contactar con el proveedor Git") from error

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        try:
            value = cast(object, response.json())
        except ValueError as error:
            raise GitClientError("El proveedor Git devolvió una respuesta inválida") from error
        if not isinstance(value, dict):
            raise GitClientError("El proveedor Git devolvió una respuesta inesperada")
        return cast(dict[str, object], value)

    @staticmethod
    def _json_list(response: httpx.Response) -> list[dict[str, object]]:
        try:
            value = cast(object, response.json())
        except ValueError as error:
            raise GitClientError("El proveedor Git devolvió una lista inválida") from error
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise GitClientError("El proveedor Git devolvió una lista inesperada")
        return [cast(dict[str, object], item) for item in value]

    def close(self) -> None:
        """Cierra el transporte HTTP propio del cliente."""

        self.http_client.close()
