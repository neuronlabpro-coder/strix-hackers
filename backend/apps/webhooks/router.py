"""Router de webhooks salientes.

Cada endpoint declara su scope con `require_scope`, y esa es la garantía: el alcance de lo
que un token puede hacer se decide en la línea que declara la dependencia, no en el
cuerpo de la función. Una función de negocio a la que se pueda llamar desde un contexto sin
scope es una vía por la que se salta la autorización.

## Por qué el identificador de la URL se compara con el del contexto

El `tenant` sale del contexto, y el `id` de la URL se compara con él antes de tocar nada.
Sin esa comparación, un `DELETE` de un endpoint ajeno respondería `204` y el usuario creería
que lo borró, cuando lo único que pasó es que se le dijo que sí. Se responde `404` para que
un endpoint de otro tenant sea indistinguible de uno que no existe.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.apps.api_access.auth import Session, TenantPrincipal, require_scope
from backend.apps.api_access.scopes import Scope
from backend.apps.webhooks.dispatcher import (
    deliver_with_retries,
    encrypt_endpoint_secret,
)
from backend.apps.webhooks.events import EventType
from backend.apps.webhooks.models import (
    WebhookEndpoint,
    generate_webhook_secret,
    is_webhook_secret,
)
from backend.apps.webhooks.schemas import (
    WebhookCreate,
    WebhookCreatedResponse,
    WebhookDeliveryPage,
    WebhookDeliveryResponse,
    WebhookEventCatalogResponse,
    WebhookPage,
    WebhookPingResult,
    WebhookResponse,
    WebhookUpdate,
    event_catalog_response,
)
from backend.core.ssrf import DnsResolutionError, SsrfBlockedError, resolve_and_validate

logger = logging.getLogger(__name__)

#: `422` va como literal y no como `status.HTTP_422_UNPROCESSABLE_ENTITY`. Starlette
#: renombró la constante y avisa en cada uso, así que referringla produce una deprecación
#: por llamada. El número es el contrato de la API y no va a cambiar; la constante sí. En
#: cuanto el proyecto fije una versión de Starlette que ya no avise, se puede volver a
#: escribir el nombre sin cambiar nada del comportamiento.
HTTP_422 = 422

router = APIRouter()

#: Tamaño de página por defecto del historial. Cuarenta entregas caben en una pantalla sin
#: paginar y son suficientes para diagnosticar un fallo reciente, que es cuando se mira.
DELIVERY_PAGE_SIZE = 20


@router.get(
    "/api/v1/webhooks/events",
    response_model=WebhookEventCatalogResponse,
)
async def list_events(
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_READ))],
) -> WebhookEventCatalogResponse:
    """Expone el catálogo de eventos para que el panel no lo repita.

    Pide `webhooks:read` y no un permiso propio: quien puede ver los endpoints de su
    organización es exactamente quien va a tener que elegir los eventos al crear uno.
    Exigir además `webhooks:create` impediría que un token de solo lectura abriera el
    asistente de alta para ver qué opciones tiene, que es un callejón sin salida para el
    usuario.
    """

    del principal
    return event_catalog_response()


@router.post(
    "/api/v1/webhooks",
    response_model=WebhookCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook(
    payload: WebhookCreate,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_CREATE))],
    session: Session,
) -> WebhookCreatedResponse:
    """Registra un endpoint y devuelve su secreto de firma **una única vez**.

    ## Por qué la URL se valida en dos momentos distintos

    Al registrar, para que el error llegue al usuario que está escribiendo la URL y no
    como un fallo de envío tres días después. Y otra vez en cada envío, porque una URL
    aceptada hoy puede apuntar a la red interna mañana. La validación del registro sola no
    protege nada: acepta un nombre público y el problema aparece cuando ese nombre cambia.

    ## Por qué se comprueba el secreto de API dentro de la URL

    Publicar el token de API propio en un endpoint ajeno es el error más fácil de cometer
    al copiar y el más caro: en cada entrega se le revelaría a quien administre el
    receptor. Se rechaza
    con un mensaje propio y no con un `422` genérico, para que se entienda qué pasó.
    """

    if is_webhook_secret(payload.url):
        raise HTTPException(
            status_code=HTTP_422,
            detail=(
                "La URL parece contener un secreto de firma de webhook. Publicarlo en un "
                "endpoint lo revelaría a quien administre el receptor en cada evento."
            ),
        )

    try:
        direcciones = resolve_and_validate(payload.url)
    except DnsResolutionError as error:
        raise HTTPException(
            status_code=HTTP_422, detail=str(error)
        ) from error
    except SsrfBlockedError as error:
        # El detalle dice **por qué** está bloqueada. Un "URL no válida" genérico deja al
        # usuario mirando la sintaxis cuando el problema es que su nombre resuelve a la red
        # interna, que es justo lo que hay que corregir.
        raise HTTPException(
            status_code=HTTP_422, detail=str(error)
        ) from error

    logger.info(
        "Webhook registrado: organization_id=%s destinos=%s",
        principal.organization.id,
        ",".join(direcciones),
    )

    secreto = generate_webhook_secret()
    endpoint = WebhookEndpoint(
        organization_id=principal.organization.id,
        url=payload.url,
        description=payload.description,
        encrypted_secret=encrypt_endpoint_secret(secreto, principal.organization.id),
        event_types=list(payload.event_types),
    )
    session.add(endpoint)
    await session.commit()
    await session.refresh(endpoint)
    # El secreto se registra como presente, nunca como valor. Esta es la linea que un
    # revisor tiene que poder leer sin tener la credencial delante.
    logger.info(
        "Secreto de firma emitido para el webhook %s (valor no registrado)", endpoint.id
    )
    return WebhookCreatedResponse(
        **WebhookResponse.from_model(endpoint).model_dump(), signing_secret=secreto
    )


@router.get("/api/v1/webhooks", response_model=WebhookPage)
async def list_webhooks(
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_READ))],
    session: Session,
) -> WebhookPage:
    """Lista los endpoints del tenant activo.

    El filtro por organización va en la consulta y no se aplica en memoria: una lista de
    endpoints de otro tenant filtrada en Python no está protegida por nada.
    """

    from sqlalchemy import select

    endpoints = (
        (
            await session.execute(
                select(WebhookEndpoint)
                .where(WebhookEndpoint.organization_id == principal.organization.id)
                .order_by(WebhookEndpoint.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return WebhookPage(
        items=[WebhookResponse.from_model(e) for e in endpoints], total=len(endpoints)
    )


async def _find_endpoint(
    session: Session, organization_id: UUID, endpoint_id: UUID
) -> WebhookEndpoint:
    """Resuelve el endpoint dentro del tenant, o responde `404`.

    El filtro lleva el `organization_id` en la consulta en vez de comprobarlo después. Las
    dos formas devuelven `404` hoy, pero con la comprobación posterior basta con que una
    ruta futura se la salte para que exista una fuga entre tenants.
    """

    from sqlalchemy import select

    endpoint = (
        (
            await session.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.id == endpoint_id,
                    WebhookEndpoint.organization_id == organization_id,
                )
            )
        )
        .scalar_one_or_none()
    )
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint no encontrado"
        )
    return endpoint


@router.patch("/api/v1/webhooks/{endpoint_id}", response_model=WebhookResponse)
async def update_webhook(
    endpoint_id: UUID,
    payload: WebhookUpdate,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_UPDATE))],
    session: Session,
) -> WebhookResponse:
    """Cambia la URL, la descripción, los eventos o el estado de un endpoint.

    Cambiar la URL **no** rota el secreto de firma: quien ya lo conoce
    seguiría recibiendo los eventos. Rotarlo es una operación aparte que este bloque no
    entonces quien controle la URL antigua sigue recibiendo. Es el comportamiento correcto
    y conviene que sea consciente, no un descuido pendiente de arreglar.
    """

    endpoint = await _find_endpoint(session, principal.organization.id, endpoint_id)
    cambios = payload.changes()
    if not cambios:
        return WebhookResponse.from_model(endpoint)

    if "url" in cambios and cambios["url"] is not None:
        try:
            resolve_and_validate(cambios["url"])
        except (SsrfBlockedError, DnsResolutionError) as error:
            raise HTTPException(
                status_code=HTTP_422, detail=str(error)
            ) from error
        endpoint.url = cambios["url"]
    if "description" in cambios:
        endpoint.description = cambios["description"]
    if "event_types" in cambios and cambios["event_types"] is not None:
        endpoint.event_types = list(cambios["event_types"])
    if "is_active" in cambios and cambios["is_active"] is not None:
        endpoint.is_active = bool(cambios["is_active"])
        # Reactivar a mano pone el contador a cero. Dejarlo donde estaba haría que un
        # endpoint reactivado se desactivara otra vez en la décima entrega siguiente, sin
        # que el usuario hubiera vuelto a equivocarse.
        if endpoint.is_active:
            endpoint.consecutive_failures = 0

    await session.commit()
    await session.refresh(endpoint)
    return WebhookResponse.from_model(endpoint)


@router.delete("/api/v1/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    endpoint_id: UUID,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_DELETE))],
    session: Session,
) -> None:
    """Elimina un endpoint y su historial de entregas.

    Se borran juntos porque el historial solo tiene sentido junto al endpoint que lo
    produjo. Lo que **no** se borra nunca es el rastro forense: si este endpoint estaba
    desactivado automáticamente, el asiento de `audit_log` sobrevive a su eliminación, y
    por eso un endpoint desaparecido puede seguir explicándose.
    """

    endpoint = await _find_endpoint(session, principal.organization.id, endpoint_id)
    await session.delete(endpoint)
    await session.commit()
    logger.info(
        "Webhook eliminado: organization_id=%s id=%s", principal.organization.id, endpoint_id
    )


@router.post("/api/v1/webhooks/{endpoint_id}/ping", response_model=WebhookPingResult)
async def ping_webhook(
    endpoint_id: UUID,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_UPDATE))],
    session: Session,
) -> WebhookPingResult:
    """Envía un evento `ping` y registra la entrega.

    ## Por qué el ping se envía de verdad y no se simula

    Un ping que devuelve `{"ok": true}` sin salir del proceso no demuestra nada: el
    usuario necesita saber si su endpoint responde, alcanza la plataforma y acepta el
    cuerpo firmado. La única forma de saberlo es hacerlo.

    ## Por qué pide `webhooks:update` y no `webhooks:create`

    Un ping es una escritura: genera una fila de historial y puede mover el contador de
    fallos. Concederlo con `webhooks:read` dejaría que un token de solo lectura
    provocara el auto-desactivado de un endpoint, que es un efecto secundario real.
    """

    endpoint = await _find_endpoint(session, principal.organization.id, endpoint_id)

    payload = {
        "event": EventType.PING.value,
        "message": "Mind Guard Fenix test delivery",
    }
    entrega = await deliver_with_retries(
        session, endpoint, EventType.PING.value, payload
    )
    if entrega is None:  # pragma: no cover - solo si MAX_ATTEMPTS fuera 0
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo registrar el intento de entrega",
        )
    return WebhookPingResult(
        delivered=entrega.succeeded,
        status_code=entrega.status_code,
        execution_time_ms=entrega.execution_time_ms,
        error_message=entrega.error_message,
        delivery_id=entrega.id,
    )


@router.get(
    "/api/v1/webhooks/{endpoint_id}/deliveries", response_model=WebhookDeliveryPage
)
async def list_deliveries(
    endpoint_id: UUID,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.WEBHOOKS_READ))],
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = DELIVERY_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> WebhookDeliveryPage:
    """Historial de entregas de un endpoint, del más reciente al más antiguo.

    El orden inverso al tiempo no es un detalle: quien mira un historial está
    diagnosticando un fallo recién ocurrido, y el evento que le interesa es el primero.
    """

    from sqlalchemy import func, select

    from backend.apps.webhooks.models import WebhookDelivery

    await _find_endpoint(session, principal.organization.id, endpoint_id)

    total = (
        await session.execute(
            select(func.count(WebhookDelivery.id)).where(
                WebhookDelivery.endpoint_id == endpoint_id,
                # R3 también en la tabla del historial: el filtro por organización no
                # puede depender de un `JOIN` que una ruta futura se salte.
                WebhookDelivery.organization_id == principal.organization.id,
            )
        )
    ).scalar_one()
    entregas = (
        (
            await session.execute(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.endpoint_id == endpoint_id,
                    WebhookDelivery.organization_id == principal.organization.id,
                )
                .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.attempt.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return WebhookDeliveryPage(
        items=[WebhookDeliveryResponse.from_model(e) for e in entregas],
        total=total,
        limit=limit,
        offset=offset,
    )
