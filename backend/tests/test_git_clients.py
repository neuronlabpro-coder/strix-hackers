import base64
import uuid

import httpx
import pytest

from backend.apps.repositories.clients.base import GitClientError, GitRateLimitError
from backend.apps.repositories.clients.github import GitHubClient
from backend.apps.repositories.clients.gitlab import GitLabClient
from backend.apps.repositories.models import GitProviderEnum

_WEBHOOK_SECRET = "w6Kq3Jm2XbT9pL4nR7sV1yA0cD5fG8hJ2kM6nQ9tU3w"


def test_github_client_uses_bearer_auth_and_returns_repositories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer github-token"
        return httpx.Response(
            200,
            json=[{"id": 1, "name": "app", "full_name": "acme/app"}],
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=http_client,
    )

    repositories = client.list_repositories()

    assert repositories[0]["full_name"] == "acme/app"
    assert client.provider == GitProviderEnum.GITHUB


def test_gitlab_client_posts_merge_request_note_with_private_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["private-token"] == "gitlab-token"
        assert str(request.url).endswith("/projects/acme%2Fapp/merge_requests/7/notes")
        return httpx.Response(201, json={"id": 42})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitLabClient(
        access_token="gitlab-token",
        organization_id=uuid.uuid4(),
        http_client=http_client,
    )

    comment_id = client.post_pr_comment("acme/app", 7, "security review")

    assert comment_id == "42"


def test_github_client_updates_comment_and_checks_write_permission() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 9})
        return httpx.Response(200, json={"permission": "write"})

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.update_pr_comment("acme/app", "9", "updated")
    assert client.has_write_access("acme/app", "reviewer") is True
    assert [request.method for request in requests] == ["PATCH", "GET"]


def test_github_client_manages_repository_metadata_and_webhook() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repositories/101":
            return httpx.Response(
                200,
                json={
                    "id": 101,
                    "name": "app",
                    "full_name": "acme/app",
                    "clone_url": "https://github.com/acme/app.git",
                    "default_branch": "main",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/hooks"):
            return httpx.Response(201, json={"id": 77})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        allowed_repo_full_name="acme/app",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    repository = client.get_repository("101")
    webhook_id = client.create_webhook(
        "acme/app",
        "https://api.example.com/api/v1/webhooks/git/github",
        _WEBHOOK_SECRET,
        ("pull_request", "issue_comment"),
    )
    client.delete_webhook("acme/app", webhook_id)

    assert repository["full_name"] == "acme/app"
    assert webhook_id == "77"
    assert ("DELETE", "/repos/acme/app/hooks/77") in calls


def test_github_client_creates_branch_and_pull_request_from_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and "/git/ref/heads/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        if request.method == "GET" and "/contents/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "content": base64.b64encode(b"old\n").decode("ascii"),
                    "sha": "content-sha",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/fenix/fix-1234abcd"})
        if request.method == "PUT" and "/contents/" in request.url.path:
            return httpx.Response(200, json={"content": {"sha": "new-sha"}})
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/acme/app/pull/1"})
        return httpx.Response(500)

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        allowed_repo_full_name="acme/app",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+safe\n"

    result = client.create_autofix_branch_and_pr(
        "acme/app",
        "main",
        "fenix/fix-1234abcd",
        patch,
        "[Fenix Security Fix] Demo",
    )

    assert result == "https://github.com/acme/app/pull/1"
    assert any(request.method == "PUT" for request in requests)
    assert any(
        request.method == "POST" and request.url.path.endswith("/pulls")
        for request in requests
    )


def test_github_client_fetches_current_pull_request_head() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pulls/31")
        return httpx.Response(
            200,
            json={
                "number": 31,
                "head": {"ref": "feature/current", "sha": "a" * 40},
                "base": {"ref": "main", "sha": "b" * 40},
            },
        )

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    details = client.get_pull_request("acme/app", 31)

    head = details["head"]
    base = details["base"]
    assert isinstance(head, dict)
    assert isinstance(base, dict)
    assert head["sha"] == "a" * 40
    assert base["sha"] == "b" * 40


def test_gitlab_client_manages_repository_metadata_and_webhook() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/v4/projects/202":
            return httpx.Response(
                200,
                json={
                    "id": 202,
                    "name": "app",
                    "path_with_namespace": "acme/app",
                    "http_url_to_repo": "https://gitlab.com/acme/app.git",
                    "default_branch": "main",
                    "visibility": "private",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/hooks"):
            return httpx.Response(201, json={"id": 88})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    client = GitLabClient(
        access_token="gitlab-token",
        organization_id=uuid.uuid4(),
        allowed_repo_full_name="acme/app",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    repository = client.get_repository("202")
    webhook_id = client.create_webhook(
        "acme/app",
        "https://api.example.com/api/v1/webhooks/git/gitlab",
        _WEBHOOK_SECRET,
        ("pull_request", "issue_comment"),
    )
    client.delete_webhook("acme/app", webhook_id)

    assert repository["path_with_namespace"] == "acme/app"
    assert webhook_id == "88"
    assert ("DELETE", "/api/v4/projects/acme/app/hooks/88") in calls


def test_gitlab_client_reports_write_access() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/members/all/reviewer")
        return httpx.Response(200, json={"access_level": 30})

    client = GitLabClient(
        access_token="gitlab-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.has_write_access("acme/app", "reviewer") is True


def test_bound_client_rejects_another_repository() -> None:
    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        allowed_repo_full_name="acme/app",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200))
        ),
    )

    with pytest.raises(GitClientError):
        client.post_pr_comment("other/app", 8, "security review")


def test_git_client_preserves_rate_limit_retry_metadata() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"})

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(GitRateLimitError) as error:
        client.list_repositories()

    assert error.value.status_code == 429
    assert error.value.retry_after == 12


def test_git_clients_do_not_expose_access_token_in_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "token github-secret rejected"})

    client = GitHubClient(
        access_token="github-secret",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        client.list_repositories()
    except Exception as error:
        assert "github-secret" not in str(error)
    else:
        raise AssertionError("Se esperaba un error del cliente Git")


def test_github_client_identifies_the_token_owner() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user"
        return httpx.Response(
            200,
            json={
                "id": 583231,
                "login": "octocat",
                "name": "The Octocat",
                "email": "octocat@github.com",
                "avatar_url": "https://example.com/a.png",
            },
        )

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    identity = client.get_authenticated_user()

    assert identity.login == "octocat"
    assert identity.provider_user_id == "583231"
    assert identity.email == "octocat@github.com"
    client.close()


def test_github_identity_treats_a_null_email_as_absent() -> None:
    """GitHub devuelve `"email": null` cuando el correo es privado.

    Sin normalizar, el panel pintaría un campo de correo con formato de correo y
    contenido vacío, que es peor que no mostrarlo.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "login": "anon", "email": None})

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_authenticated_user().email is None
    client.close()


def test_github_rejects_a_200_without_identity() -> None:
    """Un 200 que no es la respuesta de `/user` no se guarda como credencial válida."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "payload"})

    client = GitHubClient(
        access_token="github-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(GitClientError, match="identidad"):
        client.get_authenticated_user()
    client.close()


def test_gitlab_identity_maps_username_to_login_and_hides_the_internal_email() -> None:
    """GitLab llama `username` a lo que GitHub llama `login`.

    Y su `email` interno es la dirección de registro, no la pública: mandarlo al panel
    expondría un dato que el usuario no piensa compartir. Solo se usa `public_email`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/user"
        return httpx.Response(
            200,
            json={
                "id": 7,
                "username": "root",
                "name": "Administrator",
                "email": "admin@corp-interno.example",
                "public_email": "root@example.com",
                "avatar_url": "https://example.com/g.png",
            },
        )

    client = GitLabClient(
        access_token="gitlab-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    identity = client.get_authenticated_user()

    assert identity.login == "root"
    assert identity.email == "root@example.com"
    assert identity.provider_user_id == "7"
    client.close()


def test_gitlab_identity_falls_back_to_the_scoped_email() -> None:
    """Sin `read_user` no hay `public_email`; el `email` visible sí está disponible."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": 7, "username": "root", "email": "root@example.com"}
        )

    client = GitLabClient(
        access_token="gitlab-token",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_authenticated_user().email == "root@example.com"
    client.close()


def test_a_revoked_token_is_rejected_before_it_can_be_stored() -> None:
    """El caso que justifica verificar: un PAT revocado falla en el borde.

    Sin esta comprobación, la credencial se cifraría y guardaría, el panel diría que la
    conexión funcionó y el fallo aparecería en mitad de una sincronización, sin nada que
    señalara la causa real.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = GitHubClient(
        access_token="token-revocado",
        organization_id=uuid.uuid4(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(GitClientError):
        client.get_authenticated_user()
    client.close()
