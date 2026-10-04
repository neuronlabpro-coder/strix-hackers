"""Tareas asíncronas de integración Git."""

from __future__ import annotations

import asyncio
import logging

from backend.apps.repositories.clients.base import GitRateLimitError, GitServerError
from backend.apps.repositories.pipeline import _run_pr_security_pipeline
from backend.apps.repositories.token_refresh import (
    GitCredencialNoDisponibleError,
    renovar_credenciales_por_vencer,
)
from backend.apps.repositories.webhook_events import (
    WebhookEventError,
    _process_git_webhook_event,
)
from backend.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

#: Fallos transitorios que reintentan con backoff. Los dos primeros son del API de Git; el tercero
#: es del **endpoint de token** del proveedor, que es otra cosa y merece su propia entrada para que
#: el motivo de cada reintento quede en el log con su propio nombre.
#:
#: `GitCredencialNoRenovableError` **no** está aquí, y su ausencia es deliberada: reintentar un
#: `refresh_token` revocado es lo que hace que un proveedor bloquee la OAuth App de la plataforma.
_ERRORES_REINTENTABLES: tuple[type[BaseException], ...] = (
    GitRateLimitError,
    GitServerError,
    GitCredencialNoDisponibleError,
)


@celery_app.task(
    bind=True,
    name="repositories.run_pr_security_pipeline",
    autoretry_for=_ERRORES_REINTENTABLES,
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def run_pr_security_pipeline(self, review_id: str) -> str:
    """Ejecuta el scan rápido de un PR y publica su veredicto."""

    return asyncio.run(
        _run_pr_security_pipeline(
            review_id,
            retry_failed=self.request.retries > 0,
            celery_task_id=self.request.id,
        )
    )


def _enqueue_pr_review(review_id: str) -> None:
    run_pr_security_pipeline.delay(review_id)  # pyright: ignore[reportFunctionMemberAccess]


@celery_app.task(
    name="repositories.process_git_webhook_event",
    autoretry_for=(WebhookEventError, *_ERRORES_REINTENTABLES),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=5,
)
def process_git_webhook_event(
    provider: str,
    event_type: str,
    payload: dict[str, object],
    repository_id: str | None = None,
    organization_id: str | None = None,
    delivery_id: str | None = None,
) -> dict[str, object]:
    """Normaliza el evento y encola el pipeline cuando corresponde."""

    logger.info(
        "Webhook Git procesado: provider=%s event=%s repository=%s organization=%s delivery=%s",
        provider,
        event_type,
        repository_id,
        organization_id,
        delivery_id,
    )
    return asyncio.run(
        _process_git_webhook_event(
            provider,
            event_type,
            payload,
            repository_id,
            organization_id,
            delivery_id,
            enqueue_review=_enqueue_pr_review,
        )
    )


@celery_app.task(name="repositories.refresh_expiring_git_credentials")
def refresh_expiring_git_credentials() -> dict[str, int]:
    """Renueva por adelantado las credenciales Git que van a caducar.

    ## Por qué existe esta tarea y no basta con renovar en el camino del worker

    Porque renovar en el camino del worker funciona, pero obliga al webhook —que es la vía por la
    que el producto hace su trabajo principal— a pagar una conexión del pool, una llamada al
    proveedor y una escritura, en el momento en que GitHub está esperando. Adelantarlo lo quita de
    ese camino.

    Y **no lo sustituye**, y esa es la parte que hay que tener clara: esta tarea solo funciona
    mientras este proceso esté sano. Un `beat` caído, una ventana más corta que el intervalo —que la
    configuración rechaza—, o un proveedor caído más rato que la ventana dejan la credencial
    caducada igual. Por eso `build_client_for_repository` **también** renueva. Con las dos, un fallo
    de cualquiera de las dos se nota en un camino y lo tapa el otro; con solo una de las dos habría
    un modo de fallo entero sin cubrir.

    ## Por qué devuelve un recuento y no un estado de fallo

    Porque un fallo individual ya se registra con su organización, su proveedor y su estado —los
    log los dice «no se pudo renovar por adelantado» con el motivo— y el recuento da el agregado sin
    tener que leerlos uno a uno. Si la tarea devolviera `FAILURE` por cada credencial que no se pudo
    renovar, Celery retry no ayudaría en nada: no hay nada que reintentar con mejor suerte, y una
    tarea que falla siempre es indistinguible de una que no se ejecuta.
    """

    resumen = asyncio.run(renovar_credenciales_por_vencer())
    return {
        "candidatas": resumen.candidatas,
        "renovadas": resumen.renovate,
        "fallidas": resumen.fallidas,
        "sin_refresh_token": resumen.sin_refresh_token,
    }
