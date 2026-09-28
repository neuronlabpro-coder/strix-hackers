"""Conversaciones y mensajes del chat con agentes.

## Por que `organization_id` va **denominado** y no deducido

Porque una conversación no pertenece a una persona: pertenece a un workspace y la ve quien
esté dentro. Si el aislamiento se dedujera del `user_id` del autor, el resto de miembros no
la verían, y un hallazgo que un analista comparte con su equipo se quedaría en una
conversación privada. Por eso la columna existe aunque se pueda derivar: el aislamiento se
comprueba contra ella, que es la que se indexa, en vez de contra un join de dos tablas en cada
lectura.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, case, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class ChatRoleEnum(StrEnum):
    """Quién escribió el mensaje.

    `system` está aunque hoy no se guarde ninguno: el papel existe desde el primer despliegue
    porque las instrucciones del sistema son parte de la conversación y duplicarlas en el
    prompt de cada llamada es la forma de que se desincronicen del historial.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatConversation(Base):
    """Un hilo de conversación del workspace."""

    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_organization_id", "organization_id"),
        # El listado sale ordenado por `updated_at` descendente y siempre filtrado por
        # organización. El indice es compuesto y en ese orden a proposito: uno solo sobre
        # `organization_id` obliga a ordenar en memoria un conjunto que crece con el uso.
        Index("ix_chat_conversations_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: `RESTRICT` y no `CASCADE`, a diferencia de los documentos de conocimiento. Una
    #: conversación **cuesta dinero**: tiene asientos en `credit_ledger` con
    #: `CHAT_STEP_CONSUMPTION` que son registro contable. Borrarla en cascada al dar de baja el
    #: tenant dejaria asientos apuntando a mensajes que ya no existen, y el libro contable
    #: tiene que poder reconstruirse. Por eso la baja exige borrar antes las conversaciones, y
    #: el servicio dice exactamente eso cuando falta alguna.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Quien abrió el hilo. `RESTRICT` por la misma razon: es la segunda mitad del aislamiento,
    #: y un usuario borrado no puede dejar conversaciones huerfanas que otro tenant vea.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Titulo legible. Lo escribe el usuario o se deriva del primer mensaje; en ambos casos es
    #: texto libre, no se usa para buscar ni para decidir nada.
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ChatMessage(Base):
    """Un mensaje dentro de una conversación, con su consumo ya liquidado.

    ## Por que los tokens y el coste se guardan en la fila

    Porque `credit_ledger` es **append-only**: un asiento no se corrige ni se borra. Si el coste
    viviera solo en el ledger, no habria forma de saber cuantos tokens consumio un mensaje
    concreto, y una disputa de facturacion se responderia con "el total cuadra" en lugar de con
    el numero. Guardar los tres aqui hace que cada mensaje sea su propia evidencia, y que el
    asiento del ledger se pueda cotejar contra ella.

    `credits_cost` es `numeric` y no `float` por la misma razon que el saldo: una coma flotante
    acumula error entre miles de mensajes y el total deja de cuadrar con el ledger.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_conversation_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: `CASCADE` y no `RESTRICT`, al contrario que en la conversacion. Aqui no hay nada que
    #: preservar fuera: el asiento contable ya esta en el ledger y se queda ahi. La
    #: conversacion es lo que protege el libro, y por eso es ella la que restringe.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[ChatRoleEnum] = mapped_column(
        String(16), nullable=False
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: Tokens de entrada y salida consumidos por este mensaje. `0` en los mensajes de usuario:
    #: los consumir el proveedor de inferencia, no el usuario, y poner el numero real de sus
    #: palabras ahi haria creer que la facturacion incluye lo que el usuario escribe.
    #:
    #: Lleva `default` **y** `server_default`. El primero es lo que usa el ORM; el segundo, lo
    #: que hace que un `INSERT` hecho fuera de Python —una carga, unacorreccion manual, un
    #: script de soporte— no deje la columna a `NULL`. Con solo el `default` de Python, la
    #: columna es `NOT NULL` sin default en la base y ese `INSERT` falla con un error que no
    #: señala la causa. Ademas, `alembic check` compara los dos: si no coinciden, declara deriva
    #: sobre un esquema que es correcto.
    tokens_in: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    tokens_out: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )

    #: Creditos cobrados por este mensaje, ya redondeados a cuatro decimales como el saldo.
    credits_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 4),
        nullable=False,
        default=Decimal("0.0000"),
        server_default="0.0000",
    )

    #: Que modelo lo produjo. Va en la fila y no solo en el ledger porque el precio del modelo
    #: cambia con el tiempo: sin la referencia, un mes despues no se puede explicar por que un
    #: mensaje con los mismos tokens costo mas o menos.
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


#: Peso de cada rol para desempatar una misma marca de tiempo. Vive aqui y no en el servicio
#: porque el servicio y el runner **consumen** el orden y no lo deciden: los dos lo necesitan y
#: ninguno puede ser el duenno, porque el servicio importa al runner y al revés no, y ponerlo
#: en cualquiera de los dos crea un ciclo de importacion.
#:
#: ## Por que el rol y no el `id`
#:
#: Porque `now()` de PostgreSQL es la hora de **inicio de la transaccion**, no la de cada
#: sentencia. Los dos mensajes de un turno —la pregunta y la respuesta— se insertan en la
#: misma transaccion, asi que comparten `created_at` **exactamente**, y un desempate por `id`
#: no los ordena: un UUID es aleatorio por construccion, y dos filas con la misma marca quedan
#: en orden aleatorio.
#:
#: El sintoma es visible: el hilo muestra la respuesta del asistente **antes** de la pregunta que
#: la provoque, y el mismo turno aparece en orden distinto en cada recarga. Es el unico punto
#: donde el orden se puede decidir de forma determinista hoy, porque la marca de tiempo no.
#:
#: El desempate por rol es correcto **y no es un truco**, porque el orden dentro de un turno es
#: una invariante del dominio, no una preferencia: la pregunta del usuario precede siempre a su
#: respuesta. Y como cada turno es su propia transaccion, los turnos quedan separados por
#: `created_at` sin ambiguedad, de forma que (marca, rol) ordena el hilo entero bien.
#:
#: Compara la **columna** contra el valor, no el enum contra si mismo: un `case` con un
#: predicado constante devuelve siempre el mismo numero, y eso parece un desempate sin serlo.
PESO_DE_ROL = case(
    (ChatMessage.role == ChatRoleEnum.USER, 0),
    (ChatMessage.role == ChatRoleEnum.ASSISTANT, 1),
    else_=2,
)
