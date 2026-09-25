"""Normalización de eventos PR y parser de comandos ChatOps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from backend.apps.repositories.models import GitProviderEnum
from backend.core.config import settings

_PR_ACTIONS = {
    "open",
    "opened",
    "synchronize",
    "reopen",
    "reopened",
    "update",
    "updated",
}


@dataclass(frozen=True, slots=True)
class NormalizedPullRequestEvent:
    """Contexto mínimo de un PR o comentario de PR received."""

    provider: GitProviderEnum
    pr_number: int
    title: str | None
    author: str | None
    source_branch: str | None
    target_branch: str | None
    commit_sha: str | None
    is_comment: bool
    head_clone_url: str | None = None
    base_sha: str | None = None
    comment_body: str | None = None
    comment_author: str | None = None
    action: str | None = None


def parse_review_command(
    body: str,
    *,
    commands: tuple[str, ...] | None = None,
) -> bool:
    """Detecta un comando ChatOps sin depender de mayúsculas o espacios repetidos."""

    configured_commands = commands or settings.chatops_review_command_aliases
    normalized_body = " ".join(body.casefold().split())
    for command in configured_commands:
        normalized_command = " ".join(command.casefold().split())
        if not normalized_command:
            continue
        pattern = rf"(?<!\w){re.escape(normalized_command)}(?!\w)"
        if re.search(pattern, normalized_body):
            return True
    return False


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _github_event(
    event_type: str,
    payload: dict[str, object],
) -> NormalizedPullRequestEvent | None:
    normalized_type = event_type.casefold()
    if normalized_type == "pull_request":
        pull_request = _mapping(payload.get("pull_request"))
        action = _text(payload.get("action"))
        if pull_request is None or (action is not None and action.casefold() not in _PR_ACTIONS):
            return None
        head = _mapping(pull_request.get("head")) or {}
        head_repository = _mapping(head.get("repo")) or {}
        base = _mapping(pull_request.get("base")) or {}
        user = _mapping(pull_request.get("user")) or {}
        return NormalizedPullRequestEvent(
            provider=GitProviderEnum.GITHUB,
            pr_number=_number(pull_request.get("number")) or 0,
            title=_text(pull_request.get("title")),
            author=_text(user.get("login")),
            source_branch=_text(head.get("ref")),
            target_branch=_text(base.get("ref")),
            commit_sha=_text(head.get("sha")),
            is_comment=False,
            head_clone_url=_text(head_repository.get("clone_url")),
            base_sha=_text(base.get("sha")),
            action=action,
        )
    if normalized_type != "issue_comment":
        return None
    action = _text(payload.get("action"))
    if action is not None and action.casefold() != "created":
        return None
    issue = _mapping(payload.get("issue")) or {}
    pull_request_ref = _mapping(issue.get("pull_request"))
    comment = _mapping(payload.get("comment")) or {}
    if pull_request_ref is None or not comment:
        return None
    comment_user = _mapping(comment.get("user")) or {}
    return NormalizedPullRequestEvent(
        provider=GitProviderEnum.GITHUB,
        pr_number=_number(issue.get("number")) or 0,
        title=_text(issue.get("title")),
        author=_text(comment_user.get("login")),
        source_branch=None,
        target_branch=None,
        commit_sha=None,
        is_comment=True,
        comment_body=_text(comment.get("body")),
        comment_author=_text(comment_user.get("login")),
        action=action,
    )


def _gitlab_event(
    event_type: str,
    payload: dict[str, object],
) -> NormalizedPullRequestEvent | None:
    object_kind = (_text(payload.get("object_kind")) or event_type).casefold()
    attributes = _mapping(payload.get("object_attributes")) or {}
    if "merge_request" in object_kind or attributes.get("iid") is not None:
        action = _text(attributes.get("action"))
        if action is not None and action.casefold() not in _PR_ACTIONS:
            return None
        last_commit = _mapping(attributes.get("last_commit")) or {}
        diff_refs = _mapping(attributes.get("diff_refs")) or {}
        user = _mapping(payload.get("user")) or {}
        return NormalizedPullRequestEvent(
            provider=GitProviderEnum.GITLAB,
            pr_number=_number(attributes.get("iid")) or 0,
            title=_text(attributes.get("title")),
            author=_text(user.get("username") or user.get("name")),
            source_branch=_text(attributes.get("source_branch")),
            target_branch=_text(attributes.get("target_branch")),
            commit_sha=_text(last_commit.get("id") or attributes.get("sha")),
            is_comment=False,
            head_clone_url=_text(attributes.get("head_clone_url")),
            base_sha=_text(diff_refs.get("base_sha")),
            action=action,
        )
    if "note" not in object_kind:
        return None
    note_action = _text(attributes.get("action"))
    if note_action is not None and note_action.casefold() != "created":
        return None
    merge_request = _mapping(payload.get("merge_request")) or {}
    user = _mapping(payload.get("user")) or {}
    return NormalizedPullRequestEvent(
        provider=GitProviderEnum.GITLAB,
        pr_number=_number(merge_request.get("iid")) or 0,
        title=_text(merge_request.get("title")),
        author=_text(user.get("username") or user.get("name")),
        source_branch=_text(merge_request.get("source_branch")),
        target_branch=_text(merge_request.get("target_branch")),
        commit_sha=_text(merge_request.get("sha")),
        is_comment=True,
        comment_body=_text(attributes.get("note")),
        comment_author=_text(user.get("username") or user.get("name")),
        action="created",
    )


def normalize_pull_request_event(
    provider: GitProviderEnum,
    event_type: str,
    payload: dict[str, object],
) -> NormalizedPullRequestEvent | None:
    """Convierte payloads de GitHub/GitLab y filtra eventos no relacionados."""

    if provider == GitProviderEnum.GITHUB:
        return _github_event(event_type, payload)
    if provider == GitProviderEnum.GITLAB:
        return _gitlab_event(event_type, payload)
    return None


def normalize_pull_request_details(
    provider: GitProviderEnum,
    data: dict[str, object],
) -> NormalizedPullRequestEvent | None:
    """Normaliza la respuesta de detalle del API usada por ChatOps."""

    if provider == GitProviderEnum.GITHUB:
        return _github_event(
            "pull_request",
            {"action": "synchronize", "pull_request": data},
        )
    if provider == GitProviderEnum.GITLAB:
        author = data.get("author")
        if not isinstance(author, dict):
            author = {}
        return _gitlab_event(
            "merge_request_hook",
            {
                "object_kind": "merge_request",
                "object_attributes": data,
                "user": author,
            },
        )
    return None
