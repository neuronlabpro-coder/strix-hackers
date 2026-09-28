"""Operaciones sobre conversaciones y mensajes.

## El invariante de aislamiento, y donde vive

Las consultas de este modulo se dividen en dos clases, y **no por capricho**:

- Las que reciben `organization_id` —`listar_conversaciones`, `obtener_conversacion`— filtran
  por el en la propia sentencia. R3 no es una recomendacion y no se deja a la confianza de quien
  llama.
- Las que reciben solo `conversation_id` —`listar_mensajes`, `contar_mensajes`— **no filtran
  por organizacion**, y eso es correcto: los mensajes no se piden nunca directamente, se piden a
  traves de una conversacion que ya se ha resuelto con `obtener_conversacion`, que si filtra.

El riesgo de esa division no es que hoy haya un agujero: es que el invariante lo carga el
**llamador** y no la firma. Anadir manana una llamada a `listar_mensajes` con un identificador
sin comprobar daria los mensajes de otro tenant sin que ninguna firma lo advierta y sin que
ninguna prueba falle, porque la prueba que cubre el aislamiento de la lectura pasa por el
`obtener_conversacion` de antes.

Por eso el invariante esta escrito aqui y no solo en el docstring de la funcion filtrada, y por
eso `obtener_conversacion` es la **unica** puerta de entrada a un hilo: si alguna vez hace falta
consultar mensajes por un identificador sin haber cargado antes la conversacion, hay que cambiar
esta division, no a anadir un filtro mas al lado.

## Por que el turno completo vive aqui y no en la ruta

Porque un turno son **tres** escrituras y una llamada externa en un orden que importa:

1. Se guarda el mensaje del usuario.
2. Se llama al proveedor.
3. Se liquida y se guarda el mensaje del asistente.

Si el paso 2 falla, el mensaje del usuario ya esta en la base y el hilo no queda descuadrado:
el usuario ve su pregunta y un turno sin respuesta, que es exactamente lo que ocurrio. Si se
guardara todo en memoria y se insertara al final, un fallo del proveedor perderia la pregunta, y
el usuario que la ve en pantalla y al recargar la encuentra desaparecida sin ninguna explicacion.

El paso 3 va en la misma transaccion que el 1 y el 4. El asiento del ledger y el mensaje que
lo justifica se escriben juntos o no se escriben: un mensaje persistido sin su asiento es un
consumo que no se puede facturar, y un asiento sin su mensaje es un cobro que no se puede
explicar.

## Por que el titulo se deriva del primer mensaje

Porque "Nueva conversacion" en el historial de un hilo de veinte mensajes es ruido que el
usuario tiene que distinguir de verdad. Y porque un titulo de "Nueva conversacion" repetido
veces en una columna no ayuda a nadie a volver a donde estaba.

Se deriva **una sola vez**, cuando el hilo aun no tiene mensajes. No se renombra despues aunque
el hilo derive hacia otro tema, porque un titulo que cambia mientras se lee hace que el menu
lateral parezca estar moviendose solo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.chat.billing import ChatNoModelAvailableError
from backend.apps.chat.models import (
    PESO_DE_ROL,
    ChatConversation,
    ChatMessage,
    ChatRoleEnum,
)
from backend.apps.chat.runner import (
    OpcionesDeContexto,
    PasoDeChat,
    ejecutar_paso_de_chat,
)
from backend.apps.llm_router.client import LlmClientError, LlmUpstreamError

#: Titulo de una conversacion recien creada, antes de que tenga mensajes.
#:
#: Lleva tilde a proposito, y es la **excepcion** a la convencion de este paquete de escribir
#: los literales sin acento. Los comentarios y los mensajes de log van sin ella porque no los
#: lee nadie mas que el equipo; esto lo lee el usuario en el menu lateral, en un producto en
#: español, y un titulo sin tilde en la interfaz es un texto que parece maquetado a medias.
#:
#: Los literales de la API —"Conversación no encontrada"— ya la llevan, y este es del mismo
#: tipo: texto que sale por la puerta hacia la persona.
TITULO_POR_DEFECTO = "Nueva conversación"

#: Caracteres que se copian del primer mensaje para el titulo.
#:
#: La columna es `varchar(200)` y el titulo se muestra en una columna estrecha del menu lateral,
#: asi que mas de esto no se lee. Se corta en un **limite de palabra** y no en un indice fijo:
#: cortar a media palabra produce un titulo que parece truncado por un fallo, y uno que no lo
#: esta.
LARGO_TITULO = 60

class ConversationNotFoundError(LookupError):
    """La conversacion no existe, o no es de esta organizacion.

    Un solo error para los dos casos, a proposito: ver la nota de `_no_encontrado` en el router.
    """


def _titulo_desde(contenido: str) -> str:
    """El titulo que hereda una conversacion de su primer mensaje."""

    limpio = " ".join(contenido.split())
    if len(limpio) <= LARGO_TITULO:
        return limpio
    corte = limpio[:LARGO_TITULO]
    if " " in corte:
        corte = corte[: corte.rindex(" ")]
    return f"{corte.rstrip()}…"


# --------------------------------------------------------------------------- #
# Conversaciones
# --------------------------------------------------------------------------- #


async def crear_conversacion(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str | None,
) -> ChatConversation:
    conversacion = ChatConversation(
        organization_id=organization_id,
        user_id=user_id,
        title=(title or TITULO_POR_DEFECTO)[:200],
    )
    session.add(conversacion)
    await session.flush()
    return conversacion


async def listar_conversaciones(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    limite: int = 50,
    offset: int = 0,
) -> tuple[list[ChatConversation], int]:
    """Las conversaciones del workspace, y cuantas hay en total.

    El filtro por `organization_id` es **obligatorio** y va en la misma sentencia que el
    `COUNT`. R3 no es una recomendacion: es la condicion de que el aislamiento exista.

    El recuento va por `func.count` sobre el mismo filtro, **no** por `len()` del resultado. Un
    `len()` de la pagina no es el total: con veinte paginas de cincuenta, el panel necesita
    saber que hay mil para pintar la paginacion, y `len()` le diria cincuenta.
    """

    total = (
        await session.execute(
            select(func.count())
            .select_from(ChatConversation)
            .where(ChatConversation.organization_id == organization_id)
        )
    ).scalar_one()

    conversaciones = (
        (
            await session.execute(
                select(ChatConversation)
                .where(ChatConversation.organization_id == organization_id)
                # El desempate por `id` **no** es decorativo. `updated_at` tiene precision de
                # microsegundo, asi que dos mensajes enviados en la misma transaccion —que es lo
                # que pasa con dos pestanas abiertas— comparten marca de tiempo. Sin el
                # desempate, la lista cambia de orden entre dos peticiones seguidas y el menu
                # lateral parece que salta solo.
                .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
                .limit(limite)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(conversaciones), int(total)


async def contar_mensajes(
    session: AsyncSession,
    conversation_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Cuantos mensajes tiene cada conversacion, en una sola consulta.

    Existe para que el listado lateral no haga un `N+1`. Con treinta conversaciones en el
    menu, el panel sin esto hace treinta consultas de conteo en cada recarga: la respuesta no
    lo nota, pero la base recibe treinta veces el trabajo y el numero crece con el uso.

    Se hace **una** consulta agrupada en vez de una por conversacion. Un `SELECT ... WHERE id =
    ANY(...)` con el filtro por organizacion seria mas codigo para la misma respuesta.
    """

    if not conversation_ids:
        return {}

    filas = (
        await session.execute(
            select(ChatMessage.conversation_id, func.count())
            .where(ChatMessage.conversation_id.in_(conversation_ids))
            .group_by(ChatMessage.conversation_id)
        )
    ).all()
    return {conversation_id: int(cuenta) for conversation_id, cuenta in filas}


async def obtener_conversacion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> ChatConversation:
    """La conversacion, o error. Filtra por organizacion y por identificador a la vez."""

    conversacion = (
        await session.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id,
                # R3, en la misma sentencia. Sin esto, un `conversation_id` adivinado de otro
                # workspace devolveria el hilo entero con sus mensajes, y el chat es la unica
                # superficie donde eso se lee como contenido y no como un identificador.
                ChatConversation.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()

    if conversacion is None:
        raise ConversationNotFoundError(str(conversation_id))
    return conversacion


async def listar_mensajes(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> list[ChatMessage]:
    """Los mensajes del hilo, en orden cronologico.

    ## Por que no filtra por organizacion

    Porque el `conversation_id` llega de `obtener_conversacion`, que si lo hace, y los
    mensajes no se piden nunca por su cuenta. El invariante completo esta en el encabezado
    del modulo.

    ## Por que el desempate es por rol y no por `id`

    Porque `now()` de PostgreSQL es la hora de inicio de transaccion: los dos mensajes de un
    turno comparten `created_at` **exactamente**, y un UUIDv4 no ordena nada. Sin el desempate
    el hilo muestra la respuesta del asistente **antes** de la pregunta que la provoke. El
    razonamiento completo esta en `PESO_DE_ROL` en `backend/apps/chat/models.py`; el `id` al
    final solo hace la consulta estable."
    """

    return list(
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(
                    ChatMessage.created_at.asc(),
                    PESO_DE_ROL,
                    ChatMessage.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )


async def borrar_conversacion(session: AsyncSession, conversacion: ChatConversation) -> None:
    """Borra el hilo. Los mensajes caen en cascada en la base.

    No se borran a mano. La FK es `ON DELETE CASCADE`, y borrarlos desde Python haria que
    quedaran si el borrado del padre fallara a mitad, que es justo el estado que deja mensajes
    huerfanos sin ninguna relacion que los proteja.
    """

    await session.delete(conversacion)


# --------------------------------------------------------------------------- #
# El turno
# --------------------------------------------------------------------------- #


async def enviar_mensaje(
    session: AsyncSession,
    *,
    conversation: ChatConversation,
    content: str,
    opciones: OpcionesDeContexto,
    client: httpx.AsyncClient | None = None,
) -> tuple[ChatMessage, ChatMessage, PasoDeChat]:
    """Ejecuta un turno completo: guarda, llama al proveedor, liquida y guarda la respuesta.

    Devuelve **los dos** mensajes y el paso, y no solo el del asistente. El panel necesita
    confirmar el mensaje del usuario que acaba de enviar —para no duplicarlo en su estado
    local— y la respuesta en el mismo viaje, sin una segunda peticion que podria no coincidir
    con lo que la base tiene.

    ## Por que los identificadores se copian a `UUID` planos antes de la llamada externa

    Porque la llamada al proveedor **no** toca la base pero sí puede tardar, y durante ese
    tiempo el objeto `ChatConversation` sigue ligado a la sesion. Si tras la llamada se leyera
    `conversation.id` para algo, SQLAlchemy lo cargaria de forma perezosa fuera de un contexto
    de sesion y lanzaria `MissingGreenlet`.

    La copia se hace al principio y es de `uuid.UUID`, no de `Mapped`. Un identificador plano no
    tiene sesion, no puede quedar perezoso y no puede lanzar. Cuesta tres lineas y quita una
    clase de fallo que solo aparece en produccion, cuando la llamada tardo mas de lo que se
    tardo en la prueba.
    """

    conversation_id: uuid.UUID = conversation.id
    #: Se genera aqui y no dentro del runner, y la razon esta en su firma: el mismo
    #: identificador es el `reference_id` del asiento y el `id` del mensaje, de modo que el
    #: cobro es idempotente y trazable con una sola clave.
    assistant_message_id: uuid.UUID = uuid.uuid4()

    hay_mensajes = await _tiene_mensajes(session, conversation_id)

    session.add(
        ChatMessage(
            conversation_id=conversation_id,
            role=ChatRoleEnum.USER,
            content=content,
            tokens_in=0,
            tokens_out=0,
            credits_cost=Decimal("0.0000"),
            model_id=None,
        )
    )

    if not hay_mensajes and conversation.title == TITULO_POR_DEFECTO:
        conversation.title = _titulo_desde(content)

    # `now()` de la base, no `datetime.utcnow()` de Python. Con dos procesos —el backend y un
    # `now()` de la base, no `datetime.utcnow()` de Python. Con dos procesos —el
    # backend y un worker— los relojes pueden diferir unos milisegundos, y un
    # `updated_at` escrito desde Python retrocederia respecto al valor guardado,
    # que es un `server_default` real.
    paso = await ejecutar_paso_de_chat(
        session,
        conversation=conversation,
        mensaje_id=assistant_message_id,
        pregunta=content,
        opciones=opciones,
        client=client,
    )

    mensaje_asistente = ChatMessage(
        id=assistant_message_id,
        conversation_id=conversation_id,
        role=ChatRoleEnum.ASSISTANT,
        content=paso.texto,
        tokens_in=paso.tokens_in,
        tokens_out=paso.tokens_out,
        credits_cost=paso.cargo.credits if paso.cargo is not None else Decimal("0.0000"),
        model_id=paso.model_id,
    )
    session.add(mensaje_asistente)

    mensaje_usuario = await _ultimo_del_usuario(session, conversation_id)
    return mensaje_usuario, mensaje_asistente, paso


async def _tiene_mensajes(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    return bool(
        (
            await session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
            )
        ).scalar_one()
    )


async def _ultimo_del_usuario(
    session: AsyncSession, conversation_id: uuid.UUID
) -> ChatMessage:
    """El mensaje del usuario recien anadido, ya con su `id`.

    Se recupera con un `flush` antes de consultar, no con un `refresh`: `flush` escribe lo que
    hay en la sesion y deja que la base asigne los valores por defecto, y `refresh` seria una
    segunda lectura de una fila que el propio proceso acaba de escribir.
    """

    await session.flush()
    return (
        await session.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == ChatRoleEnum.USER,
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()


async def _ahora(session: AsyncSession) -> datetime:
    """La hora de la base, no la de Python.

    Con varios procesos escribiendo, el reloj del proceso puede ir atrasado. Ordenar por un
    `updated_at` puesto desde Python puede entonces **retroceder** una conversacion en el
    listado, y el menu lateral pierde la conversacion que el usuario acaba de usar.
    """

    return (
        await session.execute(select(func.now()))
    ).scalar_one()


__all__ = [
    "TITULO_POR_DEFECTO",
    "ChatNoModelAvailableError",
    "ConversationNotFoundError",
    "LlmClientError",
    "LlmUpstreamError",
    "borrar_conversacion",
    "contar_mensajes",
    "crear_conversacion",
    "enviar_mensaje",
    "listar_conversaciones",
    "listar_mensajes",
    "obtener_conversacion",
]
