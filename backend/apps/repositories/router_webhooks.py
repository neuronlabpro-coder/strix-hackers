"""Endpoint público para recibir y validar webhooks Git."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.models import GitProviderEnum, Repository
from backend.apps.repositories.tasks import process_git_webhook_event
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.rate_limit import (
    enforce_git_webhook_rate_limit,
    get_rate_limit_redis,
    rate_limit_key,
)

router = APIRouter()
logger = logging.getLogger(__name__)
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
ReplayRedisDependency = Annotated[Redis, Depends(get_rate_limit_redis)]

_SIGNATURE_HEADERS = {
    GitProviderEnum.GITHUB: "X-Hub-Signature-256",
    GitProviderEnum.GITLAB: "X-Gitlab-Token",
    GitProviderEnum.BITBUCKET: "X-Hub-Signature",
    GitProviderEnum.GITEA: "X-Gitea-Signature",
}
_EVENT_HEADERS = {
    GitProviderEnum.GITHUB: "X-GitHub-Event",
    GitProviderEnum.GITLAB: "X-Gitlab-Event",
    GitProviderEnum.BITBUCKET: "X-Event-Key",
    GitProviderEnum.GITEA: "X-Gitea-Event",
}


def _invalid_signature() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "invalid_signature"},
    )


def _repository_remote_id(
    provider: GitProviderEnum,
    payload: dict[str, object],
) -> str | None:
    if provider == GitProviderEnum.GITLAB:
        project = payload.get("project")
        if not isinstance(project, dict):
            return None
        value = project.get("id")
    else:
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            return None
        value = repository.get("id")
        if value is None and provider == GitProviderEnum.BITBUCKET:
            value = repository.get("uuid")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized if normalized and len(normalized) <= 128 else None


def _verify_signature(
    provider: GitProviderEnum,
    secret: str,
    body: bytes,
    provided: str | None,
) -> bool:
    if not provided:
        return False
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    if provider == GitProviderEnum.GITLAB:
        return hmac.compare_digest(provided_bytes, secret.encode("utf-8"))
    if provider == GitProviderEnum.GITEA:
        received_digest = provided.removeprefix("sha256=").lower()
    else:
        if not provided.startswith("sha256="):
            return False
        received_digest = provided.removeprefix("sha256=").lower()
    expected_digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received_digest.encode("ascii"), expected_digest.encode("ascii"))


def _event_type(provider: GitProviderEnum, headers: Mapping[str, str]) -> str:
    header_name = _EVENT_HEADERS[provider]
    value = headers.get(header_name, "unknown")
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:128]


def _delivery_id(
    provider: GitProviderEnum,
    headers: Mapping[str, str],
    body: bytes,
) -> str:
    del headers
    # La HMAC cubre únicamente el body; la clave de replay también debe derivar
    # únicamente de material firmado para impedir reutilizarlo con otro header.
    return f"body-{hashlib.sha256(provider.value.encode() + b'|' + body).hexdigest()}"


async def _claim_delivery(
    redis_client: Redis,
    provider: GitProviderEnum,
    delivery_id: str,
) -> bool:
    """Bloquea reentregas del mismo delivery ID durante la ventana configurada."""
    key = rate_limit_key("git-delivery", f"{provider.value}:{delivery_id}")
    try:
        claimed = await redis_client.set(
            key,
            "1",
            ex=settings.git_webhook_replay_ttl_seconds,
            nx=True,
        )
    except (RedisError, OSError, TimeoutError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo verificar la entrega del webhook",
            headers={"Retry-After": "10"},
        ) from error
    return bool(claimed)


async def _release_delivery(
    redis_client: Redis,
    provider: GitProviderEnum,
    delivery_id: str,
) -> None:
    """Libera una reserva si Celery no acepta el evento."""

    key = rate_limit_key("git-delivery", f"{provider.value}:{delivery_id}")
    try:
        await redis_client.delete(key)
    except (RedisError, OSError, TimeoutError):
        logger.exception("No se pudo liberar la reserva de entrega del webhook")


async def _read_limited_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.git_webhook_max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Payload demasiado grande",
            )
        body.extend(chunk)
    return bytes(body)


@router.post(
    "/api/v1/webhooks/git/{provider}",
    dependencies=[Depends(enforce_git_webhook_rate_limit)],
)
async def receive_git_webhook(
    provider: str,
    request: Request,
    session: SessionDependency,
    replay_redis: ReplayRedisDependency,
) -> JSONResponse:
    """Valida la firma y encola el evento sin ejecutar lógica de escaneo."""

    try:
        selected_provider = GitProviderEnum(provider.upper())
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proveedor no soportado",
        ) from error

    signature_header = _SIGNATURE_HEADERS[selected_provider]
    provided_signature = request.headers.get(signature_header)
    if not provided_signature:
        return _invalid_signature()

    body = await _read_limited_body(request)
    try:
        decoded = cast(object, json.loads(body))
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload JSON inválido",
        ) from error
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload JSON inválido",
        )
    payload = cast(dict[str, object], decoded)

    remote_repo_id = _repository_remote_id(selected_provider, payload)
    if remote_repo_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repositorio no encontrado",
        )
    result = await session.execute(
        select(Repository).where(
            Repository.provider == selected_provider,
            Repository.remote_repo_id == remote_repo_id,
            Repository.is_active.is_(True),
        )
    )
    repository = result.scalar_one_or_none()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repositorio no encontrado",
        )
    if not _verify_signature(
        selected_provider,
        repository.webhook_secret,
        body,
        provided_signature,
    ):
        return _invalid_signature()

    delivery_id = _delivery_id(selected_provider, request.headers, body)
    if not await _claim_delivery(replay_redis, selected_provider, delivery_id):
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"accepted": True, "duplicate": True},
        )

    try:
        await asyncio.to_thread(
            process_git_webhook_event.delay,  # pyright: ignore[reportFunctionMemberAccess]
            selected_provider.value,
            _event_type(selected_provider, request.headers),
            payload,
            repository_id=str(repository.id),
            organization_id=str(repository.organization_id),
            delivery_id=delivery_id,
        )
    except Exception as error:
        await _release_delivery(replay_redis, selected_provider, delivery_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo encolar el webhook",
            headers={"Retry-After": "10"},
        ) from error
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"accepted": True},
    )
