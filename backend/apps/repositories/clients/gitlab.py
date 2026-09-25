"""Cliente REST de GitLab con autenticación por PRIVATE-TOKEN."""

from __future__ import annotations

import base64
from urllib.parse import quote
from uuid import UUID

import httpx

from backend.apps.repositories.clients.base import BaseGitClient, GitClientError
from backend.apps.repositories.models import GitProviderEnum
from backend.apps.repositories.patches import AutofixPatchError, apply_patch, parse_patch

_DEFAULT_API_URL = "https://gitlab.com/api/v4"


class GitLabClient(BaseGitClient):
    """Adaptador mínimo para proyectos, estados y notas de GitLab."""

    def __init__(
        self,
        access_token: str,
        organization_id: UUID,
        api_base_url: str = _DEFAULT_API_URL,
        http_client: httpx.Client | None = None,
        allowed_repo_full_name: str | None = None,
    ) -> None:
        super().__init__(
            GitProviderEnum.GITLAB,
            access_token,
            organization_id,
            api_base_url,
            http_client,
            allowed_repo_full_name,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.access_token}

    def list_repositories(self) -> list[dict[str, object]]:
        response = self._request(
            "GET",
            "projects?membership=true&per_page=100",
            headers=self._headers,
        )
        return self._json_list(response)

    def get_clone_token(self) -> str:
        return self.access_token

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
        gitlab_state = {"failure": "failed", "error": "failed"}.get(state, state)
        project_path = quote(repo_full_name, safe="")
        commit_path = quote(sha, safe="")
        self._request(
            "POST",
            f"projects/{project_path}/statuses/{commit_path}",
            json={
                "state": gitlab_state,
                "description": description,
                "target_url": target_url,
            },
            headers=self._headers,
        )

    def post_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> str:
        self._validate_repo_reference(repo_full_name)
        self._validate_pr_number(pr_number)
        project_path = quote(repo_full_name, safe="")
        response = self._request(
            "POST",
            f"projects/{project_path}/merge_requests/{pr_number}/notes",
            json={"body": body},
            headers=self._headers,
        )
        data = self._json_object(response)
        comment_id = data.get("id")
        if comment_id is None:
            raise GitClientError("GitLab no devolvió el identificador del comentario")
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
        project_path = quote(repo_full_name, safe="")
        self._request(
            "PUT",
            f"projects/{project_path}/merge_requests/notes/{quote(comment_id, safe='')}"
            "?resolved=false",
            json={"body": body},
            headers=self._headers,
        )

    def has_write_access(self, repo_full_name: str, username: str) -> bool:
        self._validate_repo_reference(repo_full_name)
        if not username or len(username) > 255 or any(ord(char) < 32 for char in username):
            return False
        project_path = quote(repo_full_name, safe="")
        response = self._request_optional(
            "GET",
            f"projects/{project_path}/members/all/{quote(username, safe='')}",
            headers=self._headers,
        )
        if response is None:
            return False
        access_level = self._json_object(response).get("access_level")
        return isinstance(access_level, int) and access_level >= 30

    def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, object]:
        self._validate_repo_reference(repo_full_name)
        self._validate_pr_number(pr_number)
        project_path = quote(repo_full_name, safe="")
        response = self._request(
            "GET",
            f"projects/{project_path}/merge_requests/{pr_number}",
            headers=self._headers,
        )
        data = self._json_object(response)
        source_project_id = data.get("source_project_id")
        project_id = data.get("project_id")
        if (
            isinstance(source_project_id, int)
            and not isinstance(source_project_id, bool)
            and source_project_id != project_id
        ):
            source_response = self._request(
                "GET",
                f"projects/{source_project_id}",
                headers=self._headers,
            )
            clone_url = self._json_object(source_response).get("http_url_to_repo")
            if isinstance(clone_url, str):
                data["head_clone_url"] = clone_url
        return data

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
        project_path = quote(repo_full_name, safe="")
        self._request(
            "POST",
            f"projects/{project_path}/repository/branches",
            json={"branch": branch_name, "ref": base_branch},
            headers=self._headers,
        )
        original_files: dict[str, str | None] = {}
        last_commit_ids: dict[str, str] = {}
        for patch_file in patch_files:
            response = self._request_optional(
                "GET",
                f"projects/{project_path}/repository/files/{quote(patch_file.path, safe='')}"
                f"?ref={quote(base_branch, safe='')}",
                headers=self._headers,
            )
            if response is None:
                continue
            data = self._json_object(response)
            encoded_content = data.get("content")
            last_commit_id = data.get("last_commit_id")
            if not isinstance(encoded_content, str) or not isinstance(last_commit_id, str):
                raise GitClientError("GitLab devolvió un archivo de contenido inválido")
            try:
                original_files[patch_file.path] = base64.b64decode(encoded_content).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise GitClientError("GitLab devolvió un archivo no UTF-8") from error
            last_commit_ids[patch_file.path] = last_commit_id
        try:
            changes = apply_patch(original_files, patch_diff)
        except AutofixPatchError as error:
            raise GitClientError("El diff no aplica sobre el repositorio") from error
        actions: list[dict[str, object]] = []
        for change in changes:
            action: dict[str, object] = {"file_path": change.path}
            if change.content is None:
                action["action"] = "delete"
                action["last_commit_id"] = last_commit_ids[change.path]
            else:
                action["action"] = "update" if change.path in last_commit_ids else "create"
                action["content"] = change.content
                if change.path in last_commit_ids:
                    action["last_commit_id"] = last_commit_ids[change.path]
            actions.append(action)
        self._request(
            "POST",
            f"projects/{project_path}/repository/commits",
            json={
                "branch": branch_name,
                "commit_message": title,
                "actions": actions,
            },
            headers=self._headers,
        )
        merge_request = self._request(
            "POST",
            f"projects/{project_path}/merge_requests",
            json={
                "source_branch": branch_name,
                "target_branch": base_branch,
                "title": title,
                "description": "Autofix generado por Fenix Security.",
            },
            headers=self._headers,
        )
        merge_data = self._json_object(merge_request)
        merge_url = merge_data.get("web_url")
        return merge_url if isinstance(merge_url, str) else str(merge_data.get("id", ""))
