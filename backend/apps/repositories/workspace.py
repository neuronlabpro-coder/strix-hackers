"""Materialización segura y efímera de repositorios para revisiones de PR."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.services import get_organization_credential
from backend.core.config import settings
from backend.core.crypto import decrypt_secret
from backend.core.database import AsyncSessionLocal

type CommandRunner = Callable[
    [list[str], Path, dict[str, str], bytes | None],
    subprocess.CompletedProcess[bytes],
]


class WorkspaceMaterializationError(RuntimeError):
    """Error controlado al clonar o preparar un workspace de PR."""


def _default_git_runner(
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    input_data: bytes | None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            env=env,
            input=input_data,
            capture_output=True,
            check=False,
            shell=False,
            timeout=settings.git_command_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceMaterializationError("No se pudo ejecutar Git") from error


def _validate_branch(value: str, field_name: str) -> str:
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
    ):
        raise WorkspaceMaterializationError(f"Nombre de {field_name} inválido")
    return value


def _validate_commit_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise WorkspaceMaterializationError("El commit SHA del PR no es válido")
    return value.lower()


def _validate_clone_url(clone_url: str) -> str:
    try:
        parsed = urlsplit(clone_url)
    except ValueError as error:
        raise WorkspaceMaterializationError("La URL de clonado no es válida") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WorkspaceMaterializationError("La URL de clonado no es segura")
    hostname = parsed.hostname.lower()
    if hostname not in settings.git_allowed_clone_hosts:
        raise WorkspaceMaterializationError("El host de clonado no está autorizado")
    return clone_url


def _safe_remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _prepare_target_dir(target_dir: Path) -> Path:
    path = Path(target_dir)
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise WorkspaceMaterializationError("El workspace tiene un ancestro symlink")
    if path.is_symlink():
        raise WorkspaceMaterializationError("El workspace destino es un symlink")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise WorkspaceMaterializationError("El workspace destino no está vacío")
    else:
        path.mkdir(parents=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        _safe_remove(path)
        raise
    return path.absolute()


def _authorization_header(provider: GitProviderEnum, token: str) -> str:
    if provider == GitProviderEnum.GITHUB:
        return f"Bearer {token}"
    if provider == GitProviderEnum.GITLAB:
        encoded = base64.b64encode(f"oauth2:{token}".encode()).decode("ascii")
        return f"Basic {encoded}"
    if provider == GitProviderEnum.GITEA:
        return f"token {token}"
    return f"Bearer {token}"


def _git_environment(provider: GitProviderEnum, token: str) -> dict[str, str]:
    environment = os.environ.copy()
    blocked_keys = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
    for key in blocked_keys:
        environment.pop(key, None)
    for key in tuple(environment):
        if key.startswith(("GIT_TRACE", "GIT_CURL_VERBOSE")):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_PARAMETERS": "",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": _authorization_header(provider, token),
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return environment


async def _run_git(
    runner: CommandRunner,
    args: list[str],
    cwd: Path,
    environment: Mapping[str, str],
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = await asyncio.to_thread(runner, args, cwd, dict(environment), input_data)
    except WorkspaceMaterializationError:
        raise
    except Exception as error:
        raise WorkspaceMaterializationError("No se pudo ejecutar Git") from error
    if not isinstance(result, subprocess.CompletedProcess):
        raise WorkspaceMaterializationError("Git devolvió un resultado inválido")
    if result.returncode != 0:
        raise WorkspaceMaterializationError("Git rechazó la operación")
    return result


def _decode_git_lines(output: bytes) -> list[str]:
    values = output.split(b"\0")
    if values and values[-1] == b"":
        values.pop()
    try:
        return [value.decode("utf-8") for value in values if value]
    except UnicodeDecodeError as error:
        raise WorkspaceMaterializationError("Git devolvió una ruta no UTF-8") from error


def _validate_diff_path(path: str, root: Path) -> str:
    if (
        not path
        or len(path) > settings.git_max_diff_path_chars
        or "\x00" in path
        or "\\" in path
    ):
        raise WorkspaceMaterializationError("Git devolvió una ruta insegura")
    pure_path = PurePosixPath(path)
    if (
        pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or pure_path.as_posix() != path
    ):
        raise WorkspaceMaterializationError("Git devolvió una ruta insegura")
    candidate = (root / Path(*pure_path.parts)).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise WorkspaceMaterializationError("Git devolvió una ruta fuera del workspace")
    if candidate.is_symlink():
        raise WorkspaceMaterializationError("El diff contiene un symlink inseguro")
    return path


def _assert_workspace_has_no_external_symlinks(root: Path) -> None:
    try:
        entries = tuple(root.rglob("*"))
    except OSError as error:
        raise WorkspaceMaterializationError("No se pudo inspeccionar el workspace") from error
    for entry in entries:
        if entry.is_symlink():
            raise WorkspaceMaterializationError("El repositorio contiene un symlink no permitido")
        if not entry.resolve(strict=False).is_relative_to(root):
            raise WorkspaceMaterializationError("El repositorio contiene una ruta externa")


async def _resolve_credential(
    repo: Repository,
    credential: GitCredential | None,
) -> GitCredential:
    if credential is not None:
        if (
            credential.organization_id != repo.organization_id
            or credential.provider != repo.provider
        ):
            raise WorkspaceMaterializationError("La credencial no pertenece al repositorio")
        return credential
    async with AsyncSessionLocal() as session:
        return await get_organization_credential(
            session,
            repo.organization_id,
            repo.provider,
        )


async def _resolve_base_ref(
    runner: CommandRunner,
    target_path: Path,
    environment: Mapping[str, str],
    base_clone_url: str,
    base_sha: str,
) -> str:
    await _run_git(
        runner,
        ["remote", "add", "base", base_clone_url],
        target_path,
        environment,
    )
    await _run_git(
        runner,
        ["fetch", "--depth=1", "--no-tags", "base", base_sha],
        target_path,
        environment,
    )
    return base_sha


async def materialize_pr_workspace(
    repo: Repository,
    pr_review: PullRequestReview,
    target_dir: Path,
    *,
    credential: GitCredential | None = None,
    command_runner: CommandRunner | None = None,
) -> list[str]:
    """Clona una rama de PR y devuelve sus rutas modificadas sin filtrar credenciales."""

    source_branch = _validate_branch(pr_review.source_branch, "branch")
    _validate_branch(pr_review.target_branch, "rama objetivo")
    commit_sha = _validate_commit_sha(pr_review.commit_sha)
    if not pr_review.base_sha:
        raise WorkspaceMaterializationError("El PR no tiene un commit base inmutable")
    base_sha = _validate_commit_sha(pr_review.base_sha)
    clone_url = _validate_clone_url(pr_review.head_clone_url or repo.clone_url)
    base_clone_url = _validate_clone_url(repo.clone_url)
    resolved_credential = await _resolve_credential(repo, credential)
    token = decrypt_secret(
        resolved_credential.encrypted_access_token,
        organization_id=str(repo.organization_id),
        provider=repo.provider.value,
        field="access",
    )
    environment = _git_environment(repo.provider, token)
    runner = command_runner or _default_git_runner
    target_path = _prepare_target_dir(target_dir)
    root = target_path.resolve()
    try:
        try:
            await _run_git(
                runner,
                [
                    "clone",
                    "--depth=1",
                    "--single-branch",
                    "--no-tags",
                    "--branch",
                    source_branch,
                    "--",
                    clone_url,
                    str(target_path),
                ],
                target_path.parent,
                environment,
            )
        except WorkspaceMaterializationError:
            _safe_remove(target_path)
            target_path = _prepare_target_dir(target_path)
            await _run_git(runner, ["init"], target_path, environment)
            await _run_git(
                runner,
                ["remote", "add", "origin", clone_url],
                target_path,
                environment,
            )
            await _run_git(
                runner,
                ["fetch", "--depth=1", "--no-tags", "origin", commit_sha],
                target_path,
                environment,
            )
            await _run_git(
                runner,
                ["checkout", "--detach", commit_sha],
                target_path,
                environment,
            )
        head = await _run_git(runner, ["rev-parse", "HEAD"], target_path, environment)
        actual_sha = head.stdout.decode("ascii", errors="strict").strip().lower()
        if actual_sha != commit_sha:
            await _run_git(
                runner,
                ["fetch", "--depth=1", "--no-tags", "origin", commit_sha],
                target_path,
                environment,
            )
            await _run_git(
                runner,
                ["checkout", "--detach", commit_sha],
                target_path,
                environment,
            )
            head = await _run_git(runner, ["rev-parse", "HEAD"], target_path, environment)
            actual_sha = head.stdout.decode("ascii", errors="strict").strip().lower()
        if actual_sha != commit_sha:
            raise WorkspaceMaterializationError("El commit clonado no coincide con el PR")
        base_ref = await _resolve_base_ref(
            runner,
            target_path,
            environment,
            base_clone_url,
            base_sha,
        )
        diff = await _run_git(
            runner,
            ["diff", "--name-only", "-z", base_ref, "HEAD", "--"],
            target_path,
            environment,
        )
        modified_files = [
            _validate_diff_path(path, root) for path in _decode_git_lines(diff.stdout)
        ]
        if len(modified_files) > settings.git_max_diff_files:
            raise WorkspaceMaterializationError("El diff supera el límite de archivos")
        _assert_workspace_has_no_external_symlinks(root)
        return modified_files
    except Exception:
        _safe_remove(target_path)
        raise
