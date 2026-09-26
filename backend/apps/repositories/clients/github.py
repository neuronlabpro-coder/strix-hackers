"""Cliente REST de GitHub con autenticación Bearer."""

from __future__ import annotations

import base64
from urllib.parse import quote
from uuid import UUID

import httpx

from backend.apps.repositories.clients.base import (
    BaseGitClient,
    GitClientError,
    GitUserIdentity,
    optional_text,
)
from backend.apps.repositories.models import GitProviderEnum
from backend.apps.repositories.patches import AutofixPatchError, apply_patch, parse_patch

_DEFAULT_API_URL = "https://api.github.com"


class GitHubClient(BaseGitClient):
    """Adaptador mínimo para repos, estados y comentarios de GitHub."""

    def __init__(
        self,
        access_token: str,
        organization_id: UUID,
        api_base_url: str = _DEFAULT_API_URL,
        http_client: httpx.Client | None = None,
        allowed_repo_full_name: str | None = None,
    ) -> None:
        super().__init__(
            GitProviderEnum.GITHUB,
            access_token,
            organization_id,
            api_base_url,
            http_client,
            allowed_repo_full_name,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def list_repositories(self) -> list[dict[str, object]]:
        response = self._request(
            "GET",
            "user/repos?per_page=100&affiliation=owner,collaborator,organization_member",
            headers=self._headers,
        )
        return self._json_list(response)

    def get_clone_token(self) -> str:
        return self.access_token

    def get_authenticated_user(self) -> GitUserIdentity:
        response = self._request("GET", "user", headers=self._headers)
        payload = self._json_object(response)
        login = payload.get("login")
        if not isinstance(login, str) or not login:
            # Un 200 sin `login` significa que la respuesta no es la de `/user`. Se
            # trata como credencial inválida en lugar de guardar una fila sin dueño.
            raise GitClientError("GitHub no devolvió una identidad para la credencial")
        user_id = payload.get("id")
        return GitUserIdentity(
            provider_user_id=str(user_id) if user_id is not None else "",
            login=login,
            display_name=optional_text(payload.get("name")),
            email=optional_text(payload.get("email")),
            avatar_url=optional_text(payload.get("avatar_url")),
        )

    def get_repository(self, remote_repo_id: str) -> dict[str, object]:
        self._validate_remote_repo_id(remote_repo_id)
        response = self._request(
            "GET",
            f"repositories/{quote(remote_repo_id, safe='')}",
            headers=self._headers,
        )
        return self._json_object(response)

    def create_webhook(
        self,
        repo_full_name: str,
        callback_url: str,
        secret: str,
        events: tuple[str, ...],
    ) -> str:
        self._validate_repo_reference(repo_full_name)
        self._validate_webhook_target(callback_url, secret)
        if not events:
            raise GitClientError("GitHub requiere al menos un evento de webhook")
        repo_path = quote(repo_full_name, safe="/")
        response = self._request(
            "POST",
            f"repos/{repo_path}/hooks",
            json={
                "name": "web",
                "active": True,
                "events": list(events),
                "config": {
                    "url": callback_url,
                    "content_type": "json",
                    "secret": secret,
                    "insecure_ssl": "0",
                },
            },
            headers=self._headers,
        )
        webhook_id = self._json_object(response).get("id")
        if webhook_id is None:
            raise GitClientError("GitHub no devolvió el identificador del webhook")
        return str(webhook_id)

    def delete_webhook(self, repo_full_name: str, webhook_id: str) -> None:
        self._validate_repo_reference(repo_full_name)
        if not webhook_id or len(webhook_id) > 128 or any(ord(char) < 32 for char in webhook_id):
            raise GitClientError("Identificador de webhook inválido")
        repo_path = quote(repo_full_name, safe="/")
        self._request_optional(
            "DELETE",
            f"repos/{repo_path}/hooks/{quote(webhook_id, safe='')}",
            headers=self._headers,
        )

    def set_commit_status(
        self,
        repo_full_name: str,
        sha: str,
        state: str,
        description: str,
        target_url: str,
    ) -> None:
        self._validate_repo_reference(repo_full_name)
        self._validate_status(state)
        repo_path = quote(repo_full_name, safe="/")
        commit_path = quote(sha, safe="")
        self._request(
            "POST",
            f"repos/{repo_path}/statuses/{commit_path}",
            json={
                "state": state,
                "description": description,
                "target_url": target_url,
            },
            headers=self._headers,
        )

    def post_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> str:
        self._validate_repo_reference(repo_full_name)
        self._validate_pr_number(pr_number)
        repo_path = quote(repo_full_name, safe="/")
        response = self._request(
            "POST",
            f"repos/{repo_path}/issues/{pr_number}/comments",
            json={"body": body},
            headers=self._headers,
        )
        data = self._json_object(response)
        comment_id = data.get("id")
        if comment_id is None:
            raise GitClientError("GitHub no devolvió el identificador del comentario")
        return str(comment_id)

    def update_pr_comment(
        self,
        repo_full_name: str,
        comment_id: str,
        body: str,
    ) -> None:
        self._validate_repo_reference(repo_full_name)
        if not comment_id or any(ord(char) < 32 for char in comment_id):
            raise GitClientError("Identificador de comentario inválido")
        repo_path = quote(repo_full_name, safe="/")
        self._request(
            "PATCH",
            f"repos/{repo_path}/issues/comments/{quote(comment_id, safe='')}",
            json={"body": body},
            headers=self._headers,
        )

    def has_write_access(self, repo_full_name: str, username: str) -> bool:
        self._validate_repo_reference(repo_full_name)
        if not username or len(username) > 255 or any(ord(char) < 32 for char in username):
            return False
        repo_path = quote(repo_full_name, safe="/")
        response = self._request(
            "GET",
            f"repos/{repo_path}/collaborators/{quote(username, safe='')}/permission",
            headers=self._headers,
        )
        permission = self._json_object(response).get("permission")
        return isinstance(permission, str) and permission.casefold() in {
            "admin",
            "maintain",
            "write",
            "push",
        }

    def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, object]:
        self._validate_repo_reference(repo_full_name)
        self._validate_pr_number(pr_number)
        repo_path = quote(repo_full_name, safe="/")
        response = self._request(
            "GET",
            f"repos/{repo_path}/pulls/{pr_number}",
            headers=self._headers,
        )
        return self._json_object(response)

    def create_autofix_branch_and_pr(
        self,
        repo_full_name: str,
        base_branch: str,
        branch_name: str,
        patch_diff: str,
        title: str,
    ) -> str:
        self._validate_repo_reference(repo_full_name)
        if not base_branch or not branch_name or not title:
            raise GitClientError("Parámetros de autofix inválidos")
        try:
            patch_files = parse_patch(patch_diff)
        except AutofixPatchError as error:
            raise GitClientError("El diff de autofix no es válido") from error
        repo_path = quote(repo_full_name, safe="/")
        base_path = quote(base_branch, safe="")
        ref_response = self._request(
            "GET",
            f"repos/{repo_path}/git/ref/heads/{base_path}",
            headers=self._headers,
        )
        ref_data = self._json_object(ref_response)
        ref_object = ref_data.get("object")
        base_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
        if not isinstance(base_sha, str) or not base_sha:
            raise GitClientError("GitHub no devolvió el commit base")
        self._request(
            "POST",
            f"repos/{repo_path}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            headers=self._headers,
        )
        original_files: dict[str, str | None] = {}
        existing_shas: dict[str, str] = {}
        for patch_file in patch_files:
            content_response = self._request_optional(
                "GET",
                f"repos/{repo_path}/contents/{quote(patch_file.path, safe='/')}"
                f"?ref={base_path}",
                headers=self._headers,
            )
            if content_response is None:
                continue
            content_data = self._json_object(content_response)
            encoded_content = content_data.get("content")
            content_sha = content_data.get("sha")
            if not isinstance(encoded_content, str) or not isinstance(content_sha, str):
                raise GitClientError("GitHub devolvió un archivo de contenido inválido")
            try:
                original_files[patch_file.path] = base64.b64decode(encoded_content).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise GitClientError("GitHub devolvió un archivo no UTF-8") from error
            existing_shas[patch_file.path] = content_sha
        try:
            changes = apply_patch(original_files, patch_diff)
        except AutofixPatchError as error:
            raise GitClientError("El diff no aplica sobre el repositorio") from error
        for change in changes:
            file_path = quote(change.path, safe="/")
            if change.content is None:
                self._request(
                    "DELETE",
                    f"repos/{repo_path}/contents/{file_path}",
                    json={
                        "message": title,
                        "branch": branch_name,
                        "sha": existing_shas[change.path],
                    },
                    headers=self._headers,
                )
                continue
            payload: dict[str, object] = {
                "message": title,
                "content": base64.b64encode(change.content.encode("utf-8")).decode("ascii"),
                "branch": branch_name,
            }
            if change.path in existing_shas:
                payload["sha"] = existing_shas[change.path]
            self._request(
                "PUT",
                f"repos/{repo_path}/contents/{file_path}",
                json=payload,
                headers=self._headers,
            )
        pull_response = self._request(
            "POST",
            f"repos/{repo_path}/pulls",
            json={
                "title": title,
                "head": branch_name,
                "base": base_branch,
                "body": "Autofix generado por Fenix Security.",
            },
            headers=self._headers,
        )
        pull_data = self._json_object(pull_response)
        pull_url = pull_data.get("html_url")
        return pull_url if isinstance(pull_url, str) else str(pull_data.get("id", ""))
