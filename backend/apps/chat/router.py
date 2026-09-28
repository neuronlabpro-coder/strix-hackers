"""Endpoints del chat con agentes, montados en `/api/v1/chat`.

## Qué exige cada uno

- `chat:read` para listar y leer.
- `chat:write` para crear una conversación, enviar mensajes y borrar hilos.

Los dos permisos estan **separados** a proposito: un token de solo lectura puede servir para
**mostrar** el historial —un panel de actividad, un bot que resume lo detectado— sin poder
**gastar**. Con un unico permiso de chat, esa integracion no existiria.

Ninguno de los dos esta en el juego por defecto de un token nuevo, con el mismo criterio que
excluyo a `mcp:invoke`: un token que nace pudiendo responder preguntas **gasta** creditos, y
gastar sin que nadie lo haya decidido no es un detalle.

## Por qué las lecturas aceptan un token y las escrituras exigen una persona

Porque `chat_conversations.user_id` es `NOT NULL` y es el **autor** del hilo, no su propietario:
una conversación pertenece al workspace y la ve quien esté dentro. Un token de servicio no
tiene persona detras, y atribuirle un hilo a un usuario sintetico dejaria conversaciones en el
menu de alguien que nunca las abrio.

Asi que la escritura comprueba, ademas del permiso, que haya un sujeto humano. El permiso
sigue siendo la puerta —un token con `chat:write` pasa la primera comprobacion— y la autoria es
una segunda condicion, independiente: son dos reglas distintas y por eso son dos comprobaciones.
Fusionarlas haria que arreglar un caso rompiera el otro.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from backend.apps.api_access.auth import (
    Session,
    TenantPrincipal,
    UserPrincipal,
    require_scope,
)
from backend.apps.api_access.scopes import Scope
from backend.apps.chat import service
from backend.apps.chat.billing import ChatNoModelAvailableError
from backend.apps.chat.models import ChatConversation, ChatMessage
from backend.apps.chat.schemas import (
    ChatContextSource,
    ChatConversationCreate,
    ChatConversationDetail,
    ChatConversationItem,
    ChatConversationPage,
    ChatMessageCreate,
    ChatMessageItem,
    ChatStepResponse,
)
from backend.apps.llm_router.client import DEFAULT_TIMEOUT, LlmClientError, LlmUpstreamError

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

#: Tamaño de página del historial lateral. Cincuenta caben en el menu sin paginar, y son mas
#: que los hilos que alguien consulta para volver a uno.
PAGE_SIZE = 50

#: El "no hay modelo activo" y el "el proveedor falló" son `503` y no `500`.
#:
#: Un `500` le dice al panel que el error es suyo y que no hay nada que hacer. Los dos casos
#: son del servidor y ambos tienen remedio: sin modelo activo basta con activar uno, y un fallo
#: del proveedor se resuelve reintentando. Un `503` es lo que permite a un cliente reintentar
#: solo, que es la conducta correcta en los dos casos.
SERVICIO_NO_DISPONIBLE = status.HTTP_503_SERVICE_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Dependencias
# --------------------------------------------------------------------------- #


async def _proveedor_de_transporte() -> AsyncIterator[httpx.AsyncClient]:
    """El cliente HTTP contra el proveedor de inferencia.

    ## Por qué está en una dependencia y no se crea dentro del runner

    Porque una dependencia es el unico sitio donde se puede sustituir por un `MockTransport`
    sin tocar la firma de la funcion. Si el runner crease su propio cliente, la prueba tendria
    que interceptar la red —con un servidor de verdad, o parcheando `httpx` a mano— y pasaria a
    depender de como funciona la red en vez de de que devuelve el proveedor.

    ## Por qué es un generador y no una funcion normal

    Porque sin `yield` el cliente se crearia por peticion y **nunca se cerraria**. Un
    `AsyncClient` sin cerrar mantiene su pool de conexiones abierto, y el recolector de basura
    lo cierra tarde y con un aviso. En un endpoint que se llama a menudo, eso es un sockets
    agotado esperando a que pase el recolector.

    ## Por qué el timeout es el de `complete()` y no uno propio

    Importando `DEFAULT_TIMEOUT` del cliente y no escribiendo numeros aqui. Dos juegos de
    timeouts —el de la dependencia y el de `complete`— significan que el comportamiento depende
    de quien llama, y un timeout que difiere segun el camino es el peor sitio posible para que
    difiera.
    """

    cliente = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    try:
        yield cliente
    finally:
        await cliente.aclose()


TransporteDependency = Annotated[httpx.AsyncClient, Depends(_proveedor_de_transporte)]


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def _no_encontrado() -> HTTPException:
    """El `404` de estos endpoints.

    `404` y no `403`, a proposito: un `403` confirmaria que la conversacion existe, y con eso
    basta para enumerar identificadores y saber que tiene el tenant vecino. El `404` no
    distingue "no existe" de "no es tuya", que es lo unico que no filtra informacion.
    """

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada"
    )


def _exige_persona(principal: TenantPrincipal) -> UserPrincipal:
    """El sujeto humano, o `403` con el motivo.

    No se reescribe como un error generico. Un `403` sin explicacion deja al integrador
    pensando que su scope esta mal, cuando lo que le falta es una persona que firme el hilo, y
    eso no lo arregla marcando otra casilla.
    """

    if not isinstance(principal, UserPrincipal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Enviar mensajes requiere una sesión de panel: una conversación tiene autor "
                "humano y un token de servicio no lo tiene."
            ),
        )
    return principal


async def _cargar(
    session: Session,
    principal: TenantPrincipal,
    conversation_id: uuid.UUID,
) -> ChatConversation:
    """La conversacion del workspace, o `404`.

    El filtro por organizacion lo hace el servicio, en la misma sentencia que el
    identificador. Enviar el identificador de otro workspace devuelve el mismo `404` que uno
    inexistente, y por eso un `conversation_id` adivinado no revela nada.
    """

    try:
        return await service.obtener_conversacion(
            session, principal.organization.id, conversation_id
        )
    except service.ConversationNotFoundError as error:
        raise _no_encontrado() from error


def _a_item(conversacion: ChatConversation, message_count: int) -> ChatConversationItem:
    return ChatConversationItem(
        id=conversacion.id,
        title=conversacion.title,
        created_at=conversacion.created_at,
        updated_at=conversacion.updated_at,
        message_count=message_count,
    )


def _a_mensaje(
    mensaje: ChatMessage, consumo_no_verificable: bool = False
) -> ChatMessageItem:
    return ChatMessageItem(
        id=mensaje.id,
        role=mensaje.role,
        content=mensaje.content,
        tokens_in=mensaje.tokens_in,
        tokens_out=mensaje.tokens_out,
        credits_cost=mensaje.credits_cost,
        model_id=mensaje.model_id,
        created_at=mensaje.created_at,
        consumo_no_verificable=consumo_no_verificable,
    )


# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #


@router.get("/conversations", response_model=ChatConversationPage)
async def listar_conversaciones(
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.CHAT_READ))],
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChatConversationPage:
    """Las conversaciones del workspace activo, de la mas reciente a la mas antigua.

    Devuelve tambien cuantos mensajes tiene cada una, en una sola consulta agrupada. Sin ese
    numero el panel haria una consulta por fila para pintar el contador, y con treinta
    conversaciones en el menu eso son treinta consultas en cada recarga.
    """

    organization_id = principal.organization.id
    conversaciones, total = await service.listar_conversaciones(
        session, organization_id, limite=limit, offset=offset
    )
    # Los identificadores se copian a `UUID` planos antes de la siguiente consulta: despues de
    # un `flush` la sesion no esta en un contexto de carga, y leerlos de forma perezosa
    # lanzaria `MissingGreenlet`.
    ids = [conversacion.id for conversacion in conversaciones]
    conteos = await service.contar_mensajes(session, ids)
    return ChatConversationPage(
        conversations=[
            _a_item(conversacion, conteos.get(conversacion.id, 0))
            for conversacion in conversaciones
        ],
        total=total,
    )


@router.post(
    "/conversations",
    response_model=ChatConversationDetail,
    status_code=status.HTTP_201_CREATED,
)
async def crear_conversacion(
    payload: ChatConversationCreate,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.CHAT_WRITE))],
    session: Session,
) -> ChatConversationDetail:
    """Abre un hilo nuevo.

    Sin `title`, nace con el texto por defecto y **cambia al primer mensaje**: veinte "Nueva
    conversacion" en el menu lateral son veinte filas indistinguibles, y el usuario tiene que
    abrirlas todas para saber cual era cual.
    """

    usuario = _exige_persona(principal)
    conversacion = await service.crear_conversacion(
        session,
        organization_id=principal.organization.id,
        user_id=usuario.user.id,
        title=payload.title,
    )
    await session.commit()
    return ChatConversationDetail(conversation=_a_item(conversacion, 0), messages=[])


@router.get("/conversations/{conversation_id}", response_model=ChatConversationDetail)
async def leer_conversacion(
    conversation_id: uuid.UUID,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.CHAT_READ))],
    session: Session,
) -> ChatConversationDetail:
    """Un hilo con todos sus mensajes, en orden cronologico.

    El `404` no distingue "no existe" de "no es tuya": ver `_no_encontrado`.
    """

    conversacion = await _cargar(session, principal, conversation_id)
    mensajes = await service.listar_mensajes(session, conversacion.id)
    conteos = await service.contar_mensajes(session, [conversacion.id])
    return ChatConversationDetail(
        conversation=_a_item(conversacion, conteos.get(conversacion.id, 0)),
        messages=[_a_mensaje(mensaje) for mensaje in mensajes],
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def borrar_conversacion(
    conversation_id: uuid.UUID,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.CHAT_WRITE))],
    session: Session,
) -> Response:
    """Borra un hilo. Los mensajes caen en cascada en la base.

    Los asientos del ledger **no** se tocan: son registro contable y el saldo ya se desconto en
    su dia. Borrar el hilo no devuelve el dinero, y no debe: el trabajo se hizo y el consumo se
    produjo.
    """

    conversacion = await _cargar(session, principal, conversation_id)
    await service.borrar_conversacion(session, conversacion)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ChatStepResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enviar_mensaje(
    conversation_id: uuid.UUID,
    payload: ChatMessageCreate,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.CHAT_WRITE))],
    session: Session,
    transporte: TransporteDependency,
) -> ChatStepResponse:
    """Ejecuta un turno: guarda la pregunta, infiere, liquida y guarda la respuesta.

    ## Por qué se decide por separado si el mensaje del usuario sobrevive al fallo

    Porque **depende de si el prompt salio hacia el proveedor**, y eso lo decide la excepcion:

    - `ChatNoModelAvailableError` y `LlmClientError` fallan **antes** de la red: no hay gasto
      posible, y un turno sin respuesta que no ha costado nada no debe dejar rastro. Se
      descarta con `rollback`.
    - `LlmUpstreamError` significa que el prompt **si** salio. Se hace `commit` para que la
      pregunta no se pierda: el hilo queda con la pregunta y sin respuesta, que es lo que
      ocurrio, y el panel lo muestra como un turno pendiente en vez de borrarle al usuario lo
      que acaba de escribir.

    Confundir los dos casos es lo que hace que un fallo de configuracion —que se arregla
    activando un modelo— borre el trabajo del usuario.

    ## Por qué la respuesta se entrega aunque no se pueda cobrar

    Porque el trabajo ya esta hecho y ya se ha gastado en el proveedor. Cobrar cero y dejarlo
    anotado en el rastro es la unica salida que no inventa una cifra; devolver un error tiraria
    a la basura un trabajo pagado. El campo `consumo_no_verificable` viaja en el mensaje para que
    el panel lo distinga de un cobro real de cero.
    """

    _exige_persona(principal)
    conversacion = await _cargar(session, principal, conversation_id)

    try:
        _, asistente_msg, paso = await service.enviar_mensaje(
            session,
            conversation=conversacion,
            content=payload.content,
            opciones=payload.a_opciones(),
            client=transporte,
        )
    except ChatNoModelAvailableError as error:
        await session.rollback()
        raise HTTPException(
            status_code=SERVICIO_NO_DISPONIBLE, detail=str(error)
        ) from error
    except LlmUpstreamError as error:
        await session.commit()
        raise HTTPException(
            status_code=SERVICIO_NO_DISPONIBLE,
            detail=(
                "El proveedor de inferencia no respondió. Tu mensaje se ha guardado: puedes "
                "reenviarlo cuando el servicio esté disponible."
            ),
        ) from error
    except LlmClientError as error:
        await session.rollback()
        raise HTTPException(
            status_code=SERVICIO_NO_DISPONIBLE, detail=str(error)
        ) from error

    await session.commit()

    return ChatStepResponse(
        message=_a_mensaje(asistente_msg, paso.consumo_no_verificable),
        context_sources=[
            ChatContextSource(
                id=documento.document_id,
                title=documento.title,
                doc_type=documento.doc_type,
                description=documento.description,
                score=documento.score,
            )
            for documento in paso.documentos
        ],
    )


__all__ = ["PAGE_SIZE", "SERVICIO_NO_DISPONIBLE", "router"]
