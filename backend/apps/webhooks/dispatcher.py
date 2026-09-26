"""Despacho de entregas de webhook.

## Por qué la llamada es síncrona dentro de una tarea de Celery

La firma del dispatcher es síncrona (`def`, no `async def`) porque Celery ejecuta las
tareas en un proceso con su propio bucle de eventos. Una tarea `async def` necesita un
`asyncio.run` explícito y, peor, el cliente HTTP síncrono (`httpx.Client`) es el que
bloquea un hilo. La tarea ya ocupa su propio proceso, así que un cliente asíncrono ahí
solo añadiría una capa sin ganar nada.

## Por qué se revalida la URL justo antes de enviar

Una URL aceptada al registrarse puede haber cambiado de DNS meses después. Revalidar
cuesta una resolución y es la única vez que la guarda afecta a la petición que sale. Sin
ello, el registro no protege nada: se acepta un nombre público y, cuando ese nombre pasa
a apuntar a la red interna, la plataforma ya está haciendo la petición.

## Por qué no se siguen redirecciones

Un `302` a `http://169.254.169.254/` es la forma más barata de saltarse toda la
validación de la URL inicial, porque el destino lo decide el servidor receptor. Las
redirecciones no se siguen y se registran como lo que son: una respuesta más, con su
código y su cuerpo, visible en el historial.

## Por qué el fallo no propaga

La tarea se ejecuta después de que la operación de dominio haya terminado. Si el envío
fallara y eso hiciera fallar el escaneo, el escaneo dependería de un servidor externo del
cliente, y con el portátil sin conexión no se podría ni completar un análisis. El error
se guarda en el historial y se avisa, pero no sube.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.webhooks.events import EventType
from backend.apps.webhooks.models import (
    FAILURE_AUTO_DISABLE_THRESHOLD,
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BASE_SECONDS,
    WebhookDelivery,
    WebhookEndpoint,
)
from backend.apps.webhooks.signing import SignedPayload, sign_payload
from backend.core.crypto import decrypt_secret, encrypt_secret
from backend.core.ssrf import (
    DELIVERY_TIMEOUT_SECONDS,
    DnsResolutionError,
    SsrfBlockedError,
    ensure_delivery_url_allowed,
    is_redirect,
    truncate_response,
)

logger = logging.getLogger(__name__)

#: Constructor de cliente HTTP. Lo recibe la función de despacho en vez de sustituirse
#: globalmente para que una prueba pueda inyectar un transporte sin romper a las que
#: corren en paralelo del resto de la suite.
ClientFactory = Callable[[], httpx.Client]

#: Códigos que merecen reintento. Un 4xx en general no: el receptor entendió la petición
#: y la rechazó, y repetirla idéntica solo multiplica el rechazo. El 429 sí, porque es el
#: receptor el que pide esperar. Los 5xx y los timeouts sí, porque pueden ser transitorios.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class DeliveryHistoryError(RuntimeError):
    """La entrega se hizo pero su historial no se pudo guardar.

    Se distingue del fallo de envío a propósito: el evento **sí** llegó al cliente, y
    repetirlo lo duplicaría. Un `RuntimeError` genérico invites al worker de Celery a
    reintentar la tarea entera, que es exactamente lo que no hay que hacer.
    """


def backoff_seconds(attempt: int) -> float:
    """Espera antes del reintento número `attempt`, en retroceso exponencial.

    Se duplica en cada intento: 2 s, 4 s, 8 s. Con tres intentos el total de espera son
    6 segundos más el tiempo de las peticiones, que es lo que cabe en el presupuesto de
    una tarea sin retener el worker. Un retroceso fijo trataría igual a un receptor caído
    y a uno que reinicia, y el segundo se llevaría al primero.
    """

    if attempt < 1:
        return 0.0
    return RETRY_BASE_SECONDS * (2 ** (attempt - 1))


def should_retry(status_code: int | None) -> bool:
    """`True` si el resultado de un intento merece otro.

    `None` es un timeout o un error de conexión, y sí se reintenta. Un `4xx` que no está
    en `RETRYABLE_STATUS` no se reintenta nunca.
    """

    if status_code is None:
        return True
    return status_code in RETRYABLE_STATUS


def encrypt_endpoint_secret(plaintext: str, organization_id: uuid.UUID) -> str:
    """Cifra el secreto de firma con AAD ligado al tenant y al uso.

    El `provider` y el `field` forman parte del AAD, así que un secreto de webhook no se
    puede descifrar como si fuera un token de API ni con el AAD de otro tenant. Esa
    ligazón es la que impide mover un secreto entre organizaciones en la base.
    """

    return encrypt_secret(
        plaintext,
        organization_id=str(organization_id),
        provider="webhook",
        field="signing",
    )


def decrypt_endpoint_secret(endpoint: WebhookEndpoint) -> str:
    """Descifra el secreto de la fila, ligado a su organización.

    Toma la fila y no un `organization_id` suelto a propósito: es la fila la que
    garantiza que el secreto se descifra en el contexto del tenant al que pertenece.
    """

    return decrypt_secret(
        endpoint.encrypted_secret,
        organization_id=str(endpoint.organization_id),
        provider="webhook",
        field="signing",
    )


def _default_client_factory() -> httpx.Client:
    """Cliente con el timeout de la especificación y sin seguir redirecciones."""

    return httpx.Client(timeout=DELIVERY_TIMEOUT_SECONDS, follow_redirects=False)


async def _sleep(segundos: float) -> None:
    """Duerme sin bloquear el bucle.

    Se aísla en una función propia para que las pruebas puedan sustituirla y no esperar
    seis segundos reales por cada reintento. Una prueba de reintentos que tarda medio
    minuto es una prueba que nadie ejecuta a menudo, y una prueba que no se ejecuta es una
    prueba que se pudre.
    """

    await asyncio.sleep(segundos)


async def _record_delivery(
    session: AsyncSession,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: dict[str, Any],
    *,
    attempt: int,
    status_code: int | None,
    response_body: str | None,
    execution_time_ms: int | None,
    error_message: str | None,
) -> WebhookDelivery:
    """Persiste un intento de entrega."""

    entrega = WebhookDelivery(
        endpoint_id=endpoint.id,
        organization_id=endpoint.organization_id,
        event_type=event_type,
        payload=payload,
        status_code=status_code,
        response_body=response_body,
        execution_time_ms=execution_time_ms,
        attempt=attempt,
        error_message=error_message,
    )
    try:
        session.add(entrega)
        await session.commit()
        await session.refresh(entrega)
    except Exception as error:
        await session.rollback()
        logger.error(
            "No se pudo guardar la entrega endpoint=%s: %s",
            endpoint.id,
            type(error).__name__,
        )
        raise DeliveryHistoryError(str(error)) from error
    return entrega


async def dispatch_webhook(
    session: AsyncSession,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    client_factory: ClientFactory | None = None,
) -> WebhookDelivery:
    """Envía un evento a un endpoint, registra el intento y actualiza el contador.

    ## Por qué la guarda SSRF va antes de firmar

    Firmar primero expondría el secreto a una URL que a continuación se va a rechazar. No
    hay ninguna razón para pagar ese coste de CPU, ni para que el orden cambie por error
    en el futuro y deje de cumplirse.

    ## Por qué un fallo de DNS también se registra y no sube

    Un nombre que no resuelve es un destino inalcanzable, que es un resultado de la
    entrega y no un error del motor. Si la excepción escapara, Celery la marcaría como
    fallida y, con `task_acks_late`, la devolvería a la cola: un dominio que dejó de
    existir en DNS produciría reintentos indefinidos. Se registra como entrega fallida,
    que además es lo que el usuario necesita ver: "tu dominio ya no resuelve".
    """

    try:
        ensure_delivery_url_allowed(endpoint.url)
    except SsrfBlockedError as error:
        # Se registra como entrega fallida en vez de lanzar: la fila es lo que permite
        # ver que un endpoint dejó de ser válido, que es un estado que el usuario necesita
        # descubrir por sí mismo.
        return await _record_delivery(
            session,
            endpoint,
            event_type,
            payload,
            attempt=attempt,
            status_code=None,
            response_body=None,
            execution_time_ms=None,
            error_message=f"Bloqueado por la guarda anti-SSRF: {error}",
        )
    except DnsResolutionError as error:
        return await _record_delivery(
            session,
            endpoint,
            event_type,
            payload,
            attempt=attempt,
            status_code=None,
            response_body=None,
            execution_time_ms=None,
            error_message=f"El destino no se pudo resolver: {error}",
        )

    signed: SignedPayload = sign_payload(decrypt_endpoint_secret(endpoint), payload)
    headers = signed.headers(event_type, str(uuid.uuid4()))
    crear = client_factory or _default_client_factory

    status_code: int | None = None
    response_body: str | None = None
    error_message: str | None = None
    inicio = time.monotonic()

    try:
        with crear() as client:
            respuesta = client.post(endpoint.url, content=signed.body, headers=headers)
            status_code = respuesta.status_code
            response_body = truncate_response(respuesta.text)
    except httpx.TimeoutException:
        error_message = f"Tiempo de espera agotado tras {DELIVERY_TIMEOUT_SECONDS:g} s"
    except httpx.HTTPError as error:
        error_message = f"Error de red: {type(error).__name__}"

    execution_time_ms = int((time.monotonic() - inicio) * 1000)

    if status_code is not None and is_redirect(status_code):
        error_message = (
            f"El receptor respondió {status_code} y su redirección no se sigue por "
            "seguridad: apunta a un destino no validado."
        )

    entrega = await _record_delivery(
        session,
        endpoint,
        event_type,
        payload,
        attempt=attempt,
        status_code=status_code,
        response_body=response_body,
        execution_time_ms=execution_time_ms,
        error_message=error_message,
    )
    await _apply_outcome(session, endpoint, entrega)
    return entrega


async def _apply_outcome(
    session: AsyncSession, endpoint: WebhookEndpoint, entrega: WebhookDelivery
) -> None:
    """Pone a cero el contador en un 2xx, lo incrementa en un fallo y desactiva al llegar
    al umbral.

    La desactivación automática va acompañada de un asiento de auditoría. Sin él, un
    endpoint que se desactiva solo es un misterio: nadie sabe cuándo pasó, por qué ni
    quién estaba al otro lado. El asiento responde a las tres.
    """

    if entrega.succeeded:
        if endpoint.consecutive_failures != 0:
            endpoint.consecutive_failures = 0
            await session.commit()
        return

    endpoint.consecutive_failures = (endpoint.consecutive_failures or 0) + 1
    if endpoint.should_auto_disable():
        endpoint.is_active = False
        session.add(
            AuditLogEntry(
                organization_id=endpoint.organization_id,
                actor_user_id=None,
                action=AuditActionEnum.WEBHOOK_AUTO_DISABLED,
                entity_type="webhook_endpoint",
                entity_id=endpoint.id,
                from_state="active",
                to_state="auto_disabled",
            )
        )
        logger.warning(
            "Webhook desactivado por %s fallos consecutivos: endpoint=%s url=%s",
            FAILURE_AUTO_DISABLE_THRESHOLD,
            endpoint.id,
            endpoint.url,
        )
    await session.commit()


async def deliver_with_retries(
    session: AsyncSession,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: dict[str, Any],
    *,
    client_factory: ClientFactory | None = None,
    sleeper: Callable[[float], Any] | None = None,
) -> WebhookDelivery | None:
    """Envía el evento reintentando en retroceso exponencial.

    ## Por qué se registra cada intento y no solo el último

    El diagnóstico que necesita el cliente es "cuántas veces intentó y qué vio cada vez".
    Guardar solo el intento final deja esa pregunta sin respuesta, y la respuesta es
    justo la que separa "se cayó dos segundos" de "lleva días caído".

    ## Por qué un fallo de historial corta la cadena

    A partir del momento en que una fila no se guarda, cada reintento vuelve a enviar sin
    dejar rastro. Eso es peor que un fallo visible: el cliente recibe duplicados y el
    historial no muestra ni uno, así que la auditoría no cuadra.
    """

    esperar = sleeper or _sleep
    ultima: WebhookDelivery | None = None
    for intento in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        ultima = await dispatch_webhook(
            session,
            endpoint,
            event_type,
            payload,
            attempt=intento,
            client_factory=client_factory,
        )
        if ultima.succeeded or not should_retry(ultima.status_code):
            return ultima
        if intento < MAX_DELIVERY_ATTEMPTS:
            espera = backoff_seconds(intento)
            logger.info(
                "Reintento %d/%d en %gs: endpoint=%s status=%s",
                intento,
                MAX_DELIVERY_ATTEMPTS,
                espera,
                endpoint.id,
                ultima.status_code,
            )
            await esperar(espera)
    return ultima


# --------------------------------------------------------------------------- #
# Selección de endpoints
# --------------------------------------------------------------------------- #


async def subscribed_endpoint_ids(
    session: AsyncSession, organization_id: uuid.UUID, event_type: str
) -> list[str]:
    """IDs de los endpoints **activos** y suscritos al evento, en el orden de la tabla.

    El filtro por organización va en el `WHERE` y no se aplica después en memoria: R3 exige
    que la restricción esté en la consulta, y filtrar en Python dejaría la puerta abierta a
    que un cambio futuro de esta función la olvide. Con 46 scopes de API ya se aprendió
    que la puerta se abre por un cambio de una línea.
    """

    resultado = await session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.organization_id == organization_id,
            WebhookEndpoint.is_active.is_(True),
        )
    )
    return [str(e.id) for e in resultado.scalars().all() if e.has_event(event_type)]


def enqueue_event(
    event_type: EventType | str, organization_id: uuid.UUID, payload: dict[str, Any]
) -> int:
    """Encola el evento para los endpoints suscritos del tenant. Devuelve cuántos son.

    Encolar no es enviar, y por eso **no propaga excepciones**: si el broker no está
    disponible, la operación de dominio que emite no debe fallar. Un escaneo que no se
    completa porque el cliente tiene el webhook caído sería un fallo de la plataforma
    causado por un tercero.

    La cuenta y el encolado usan **la misma lista** de endpoints. Consultar dos veces y
    contar una mientras se encola otra es como aparece un "despachado a 2 endpoints" y
    solo llega a uno.
    """

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.core.config import settings
    from backend.core.database import create_database_engine

    nombre = event_type.value if isinstance(event_type, EventType) else str(event_type)
    cuerpo = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    async def _ids() -> list[str]:
        engine = create_database_engine(settings)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as session:
                return await subscribed_endpoint_ids(session, organization_id, nombre)
        finally:
            await engine.dispose()

    try:
        destinos = asyncio.run(_ids())
    except Exception as error:
        logger.error(
            "No se pudieron resolver los endpoints suscritos a %s: %s",
            nombre,
            type(error).__name__,
        )
        return 0

    from backend.workers.celery_app import celery_app

    for endpoint_id in destinos:
        try:
            celery_app.send_task(
                "webhooks.deliver_event",
                args=[endpoint_id, nombre, cuerpo],
            )
        except Exception as error:
            # Un endpoint que no se pudo encolar no impide encolar los demás: son
            # independientes, y perder todos por el fallo de uno sería lo contrario.
            logger.error(
                "No se pudo encolar la entrega del endpoint %s: %s",
                endpoint_id,
                type(error).__name__,
            )
    return len(destinos)


# --------------------------------------------------------------------------- #
# Tarea de Celery
# --------------------------------------------------------------------------- #


def deliver_event_task(endpoint_id: str, event_type: str, payload_json: str) -> str:
    """Punto de entrada de Celery.

    Corre en su propio proceso y abre su propia conexión: no comparte sesión con quien
    encola, que en un broker real está en otra máquina y en otro momento.
    """

    from backend.core.config import settings

    payload: dict[str, Any] = json.loads(payload_json)
    return asyncio.run(
        _deliver_from_worker(endpoint_id, event_type, payload, settings)
    )


async def _deliver_from_worker(
    endpoint_id: str, event_type: str, payload: dict[str, Any], settings: Any
) -> str:
    """Cuerpo asíncrono de la tarea, con su propio motor de base de datos."""

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.core.database import create_database_engine

    engine = create_database_engine(settings)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                objetivo = uuid.UUID(endpoint_id)
            except ValueError:
                return "identificador_invalido"
            endpoint = (
                await session.execute(
                    select(WebhookEndpoint).where(WebhookEndpoint.id == objetivo)
                )
            ).scalar_one_or_none()
            if endpoint is None:
                # El endpoint se borró entre el encolado y la ejecución. No es un error:
                # se registró con éxito y luego se eliminó, y la entrega ya no tiene a
                # quién mandarse.
                logger.info(
                    "Entrega de webhook descartada: el endpoint %s ya no existe", endpoint_id
                )
                return "endpoint_ausente"
            # Se revalidan las dos condiciones que el emisor ya comprobó. Entre el encolado
            # y la ejecución el usuario puede haber pausado el endpoint o haberlo
            # desuscrito, y enviar a un endpoint pausado es justo lo que pausar significa.
            if not endpoint.is_active:
                logger.info(
                    "Entrega de webhook descartada: el endpoint %s esta pausado", endpoint_id
                )
                return "endpoint_pausado"
            if not endpoint.has_event(event_type):
                return "sin_suscripcion"

            entrega = await deliver_with_retries(session, endpoint, event_type, payload)
            return "ok" if entrega is not None and entrega.succeeded else "fallido"
    finally:
        await engine.dispose()
