"""Tareas asíncronas de integración Git."""

from __future__ import annotations

import asyncio
import logging

from backend.apps.repositories.clients.base import GitRateLimitError, GitServerError
from backend.apps.repositories.pipeline import (
    _run_pr_security_pipeline,
    reintentar_comentario_de_revision,
)
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

#: Cuántos intentos hace como mucho una tarea que habla con el proveedor, y cuánto espera entre
#: ellos como tope.
#:
#: ## Por qué son constantes y no números escritos en cada decorador
#:
#: Porque hay **dos** tareas que reintentan contra el mismo proveedor con la misma política —el
#: pipeline de la revisión y el reintento del comentario—, y dos decoradores con dos números
#: escritos a mano divergen sin que nada se entere: uno acabaría reintentando cuatro veces y el
#: otro tres, y el registro del worker mostraría dos políticas distintas para el mismo GitHub.
#:
#: ## Por qué viven aquí y no en la configuración
#:
#: Porque no son un parámetro del producto: son la política de resiliencia de un cliente HTTP
#: concreto, y no hay operador que los negocie ni un precio que dependa de ellos. El contraste
#: con R1 está en `platform_pricing`, que sí es un precio y por eso sí sale de la base de datos.
MAX_INTENTOS_GIT = 3
ESPERA_MAXIMA_ENTRE_INTENTOS = 300


@celery_app.task(
    bind=True,
    name="repositories.run_pr_security_pipeline",
    autoretry_for=_ERRORES_REINTENTABLES,
    retry_backoff=True,
    retry_backoff_max=ESPERA_MAXIMA_ENTRE_INTENTOS,
    max_retries=MAX_INTENTOS_GIT,
)
def run_pr_security_pipeline(self, review_id: str) -> str:
    """Ejecuta el scan rápido de un PR y publica su veredicto.

    ## Por qué `retry_failed` es `self.request.retries > 0` y no un parámetro

    Porque es la **única** señal que distingue «el proveedor nos lo ha pedido otra vez por lo
    mismo» de «el cliente pidió analizar otra vez». Un reintento automático de esta tarea es la
    misma tarea con el mismo argumento: no es un encargo nuevo, y por eso `_claim_review` no lo
    vuelve a cobrar. Un relanzamiento desde el panel llega por aquí con `retries == 0`, y ese sí
    cobra, porque es un análisis nuevo que alguien pidió.

    Lo que mantiene esa separación en su sitio es que **solo** `autoretry_for` produce reintentos:
    `process_git_webhook_event`, `_enqueue_pr_review` y el watchdog encolan siempre con
    `.delay(review_id)`, que siempre arranca en `retries == 0`.
    """

    return asyncio.run(
        _run_pr_security_pipeline(
            review_id,
            retry_failed=self.request.retries > 0,
            celery_task_id=self.request.id,
        )
    )


@celery_app.task(
    name="repositories.publish_pr_review_comment",
    autoretry_for=_ERRORES_REINTENTABLES,
    retry_backoff=True,
    retry_backoff_max=ESPERA_MAXIMA_ENTRE_INTENTOS,
    max_retries=MAX_INTENTOS_GIT,
)
def publicar_comentario_de_review(review_id: str) -> str:
    """Republica el comentario de un pull request **sin repetir el escaneo**.

    ## Por qué esta tarea existe y no es una bandera del pipeline

    Porque el fallo que la justificaba no era «el comentario no se publicó», sino «no se publicó
    el comentario y **el análisis tampoco**». `post_pr_comment` y `update_pr_comment` lanzaban
    `GitClientError`, ese error salía por el `except Exception` del pipeline, `_mark_pipeline_error`
    ponía la revisión en `ERROR` con el run ya en `COMPLETED`, y Celery —que reintenta ante
    `GitRateLimitError`— relanzaba el pipeline entero para un problema de permisos de escritura
    en el pull request.

    Con el comentario detrás de su propia tarea, el fallo del comentario tiene su propio
    reintento y ese reintento **no puede** repetir el escaneo: no pasa por `_claim_review`, no
    crea un run y no toca el ledger. El precio de que exista una tarea más es una función de
    treinta líneas; el precio de no tenerla era perder análisis ya pagados.

    ## Por qué devuelve `SKIPPED` sin más

    Porque `reintentar_comentario_de_revision` es idempotente y su resultado lo dice: `POSTED`
    cuando publicó, `SKIPPED` cuando no había nada que publicar. Convertir un «no había nada» en
    una tarea roja sería ruido que obliga a mirar un registro de worker para descubrir que no
    pasó nada.
    """

    return asyncio.run(reintentar_comentario_de_revision(review_id))


def _enqueue_pr_review(review_id: str) -> None:
    run_pr_security_pipeline.delay(review_id)  # pyright: ignore[reportFunctionMemberAccess]


@celery_app.task(
    name="repositories.process_git_webhook_event",
    autoretry_for=(WebhookEventError, *_ERRORES_REINTENTABLES),
    retry_backoff=True,
    retry_backoff_max=ESPERA_MAXIMA_ENTRE_INTENTOS,
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
