"""Esquemas del chat: lo que entra y lo que sale por la API.

## Por que `context_options` es un modelo y no un `dict`

Porque decide **que se le afirma al modelo sobre lo que el usuario ha acotado**. Un `dict` libre
convierte el prompt en un canal por el que un integrador puede escribir "ignora las reglas
anteriores" y el modelo lo seguiria. Con un modelo Pydantic de campos declarados y
`extra="forbid"`, lo que no existe **no llega a existir**: se rechaza con un `422` que nombra
el campo sobrante, en vez de llegar al prompt como una instruccion que el usuario no escribio.

## Por que los valores de las listas de alcance son `str` y no `UUID`

Porque el panel puede acortar a repositorios y dominios que **todavia no estan connected**. Si
fueran `UUID`, el usuario no podria acotar a un repositorio que todavia no ha conectado, que es
precisamente el momento en que quiere escribir la pregunta. El identificador real lo resuelve el
modelo contra los datos del workspace, y si no encuentra ninguno simplemente no aporta contexto
para ese nombre, que es el comportamiento correcto: no es un error pedir sobre algo que aun no
existe.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.apps.chat.models import ChatRoleEnum
from backend.apps.chat.runner import OpcionesDeContexto
from backend.core.config import settings

# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #


class ContextOptions(BaseModel):
    """Lo que el usuario ha acotado para este paso.

    Se declara aqui y no se reutiliza el `OpcionesDeContexto` del runner porque los dos viven
    en capas distintas: este es el **contrato de la API**, y el del runner es el dato interno
    ya normalizado. Compartiendo el tipo, una clave anadida al contrato de la API llegaria al
    prompt sin pasar por la traduccion, que es justo el punto donde se decide que se afirma.
    """

    model_config = ConfigDict(extra="forbid")

    #: Que el usuario ha pegado material sensible. No transporta el material: solo declara
    #: que lo hay, para que el modelo no lo repita. Los secretos viajan dentro de `content` y
    #: no en un campo con nombre propio, porque un campo llamado `credentials` en un esquema
    #: invita a rellenarlo y acabaria en los logs de peticion.
    credenciales_de_contexto: bool = False
    #: Dominios a los que acota el analisis.
    dominios: list[str] = Field(default_factory=list, max_length=20)
    #: Repositorios a los que acota el analisis.
    repositorios: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("dominios", "repositorios")
    @classmethod
    def _sin_vacios(cls, valores: list[str]) -> list[str]:
        """Quita las entradas vacias y normaliza a minuscula.

        Una lista con una cadena vacia no acota nada y si se colara en el prompt produciria
        "acota el analisis a estos dominios: " con nada detras, que es una instruccion sin
        contenido que el modelo tendria que interpretar.
        """

        return [v.strip().lower() for v in valores if v.strip()]

    def a_opciones(self) -> OpcionesDeContexto:
        return OpcionesDeContexto(
            credenciales_de_contexto=self.credenciales_de_contexto,
            dominios=tuple(self.dominios),
            repositorios=tuple(self.repositorios),
        )


class ChatConversationCreate(BaseModel):
    """Alta de una conversacion.

    `extra="forbid"` y `str_strip_whitespace`: un cliente que envie `{"name": ...}` en vez de
    `title` recibe un `422` que dice cual es el campo, en lugar de una conversacion creada con
    el titulo por defecto que el usuario no eligio.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: Opcional. Sin el, la conversacion nace con el texto por defecto y **cambia al primer
    #: mensaje**: un titulo de "Nueva conversacion" en el historial de una conversacion de
    #: veinte mensajes es ruido que el usuario tiene que distinguir de verdad.
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ChatMessageCreate(BaseModel):
    """Un turno del usuario."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(
        min_length=1,
        max_length=settings.chat_max_content_chars,
    )
    context_options: ContextOptions | None = None

    def a_opciones(self) -> OpcionesDeContexto:
        if self.context_options is None:
            return OpcionesDeContexto()
        return self.context_options.a_opciones()


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #


class ChatMessageItem(BaseModel):
    """Un mensaje tal como lo ve el panel.

    ## Por que lleva los tres numeros de consumo y no solo el texto

    Porque son la **evidencia propia del mensaje**, y el asiento del ledger puede cotejarse
    contra ellos. Si el panel solo recibiera el texto, una disputa de facturacion se
    responderia con "el total cuadra", que es exactamente lo que R4 quiere evitar.

    `consumo_no_verificable` distingue **cobro cero** de **cobro no verificable**. Los dos
    muestran `credits_cost = 0` y son cosas distintas: el primero es un paso que de verdad no
    costo nada, y el segundo es un paso que costo trabajo y no se pudo medir. Presentarlos
    igual haria que un fallo de medicion del proveedor pareciera un ahorro.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: ChatRoleEnum
    content: str
    tokens_in: int
    tokens_out: int
    credits_cost: Decimal
    model_id: str | None
    created_at: datetime
    consumo_no_verificable: bool = False


class ChatContextSource(BaseModel):
    """Un documento que se ha inyectado en el prompt de este paso.

    Se devuelve para que la interfaz pueda **decir de donde sale el contexto**. Un RAG que no
    explica sus fuentes es un RAG del que el usuario no puede dudar, y un usuario que no puede
    dudar de la respuesta la acaba aceptando aunque sea falsa.
    """

    id: uuid.UUID
    title: str
    doc_type: str
    description: str
    score: float


class ChatStepResponse(BaseModel):
    """El resultado de un paso: el mensaje del asistente y lo que costo."""

    message: ChatMessageItem
    #: Documentos que se han inyectado en este paso, no en toda la conversacion. Vacio
    #: significa que no habia contexto del workspace, que es un dato que el usuario quiere
    #: saber antes de confiar en la respuesta.
    context_sources: list[ChatContextSource] = Field(default_factory=list)


class ChatConversationItem(BaseModel):
    """Una conversacion en el listado lateral."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    #: Cuantos mensajes tiene. El panel lo muestra junto a la fecha y evita el brillo de un
    #: `N+1`: sin el, la lista de conversaciones haria una consulta por fila para contar.
    message_count: int = 0


class ChatConversationDetail(BaseModel):
    """Una conversacion con todos sus mensajes, en orden cronologico."""

    conversation: ChatConversationItem
    messages: list[ChatMessageItem]


class ChatConversationPage(BaseModel):
    conversations: list[ChatConversationItem]
    total: int = Field(ge=0)
