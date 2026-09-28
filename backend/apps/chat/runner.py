"""Ensamblado y ejecucion de un paso del chat con agentes.

## Que hace este modulo

Cuatro cosas, en este orden: recupera el contexto de la base de conocimiento, arma el prompt
con las instrucciones de Red Team y ese contexto, llama al proveedor, y liquida lo que se ha
consumido. No decide **si** se cobra: esa politica vive en `billing.py` y aqui solo se le pasan
los numeros.

## Por que la recuperacion es una funcion y no una clase

Porque el punto de extension que importa es el motor de recuperacion, y una funcion es lo mas
facil de sustituir. Cuando `pgvector` este disponible, estas funciones pasan a delegar en una
consulta de similitud coseno y el resto del modulo —ensamblado, inferencia, liquidacion— no
se toca. Una clase con inyeccion de dependencias haria el mismo trabajo con mas codigo y un
punto mas donde el motor equivocado puede colarse sin que nadie lo note.

La API publica devuelve **cadenas ya formateadas** —`retrieve_rag_context`—: el motor decide
como buscar, este modulo decide como se presenta. Si devolviera documentos, cada motor tendria
que conocer el formato del bloque, y bastaria con que uno lo presentara de otra forma para
que el prompt cambiara sin que nadie lo decidiera.

## Por que la ausencia de `usage` NO se cobra y ademas se deja rastro

Porque **no se puede cobrar lo que no se puede medir**. Si el proveedor no publica el consumo,
inventar un numero no es una estimacion: es una cifra sin respaldo en un asiento contable, y
un saldo que baja por una cantidad que nadie puede justificar es exactamente el problema que
R4 existe para evitar.

La politica es, en orden: se guarda la respuesta con `credits_cost=0`, se deja constancia en
el rastro con `UNREPORTED_USAGE_UNBILLED`, y **el mensaje se entrega igual**. No se descarta
una respuesta que ya ha costado trabajo real al proveedor: solo que no se sabe cuanto. Cobrar
cero y anotarlo es la unica opcion que no inventa, no decae y no pierde.

## Por que el historial se carga desde la base y no se pasa desde el panel

Porque el panel no es quien recuerda. Un historial que viaja en cada peticion es un historial
que el cliente puede recortar, reordenar o inventar, y una facturacion que dice cuantos
mensajes se mandaron deja de ser creible. La conversacion vive en `chat_messages`, que es la
que cobra, y el prompt se arma desde ahi.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.chat.billing import (
    ChatStepCharge,
    liquidar_paso_de_chat,
    resolve_chat_model,
)
from backend.apps.chat.models import (
    PESO_DE_ROL,
    ChatConversation,
    ChatMessage,
    ChatRoleEnum,
)
from backend.apps.knowledge.retrieval import RetrievedDocument, recuperar_documentos
from backend.apps.llm_router.client import LlmTurn, complete
from backend.core.config import settings

logger = logging.getLogger(__name__)

#: Cuantos documentos se recuperan cuando quien llama no dice.
#:
#: Coincide con el valor por defecto de `settings.chat_context_documents` y **se declara
#: aparte a proposito**: es el valor de la funcion, que se puede probar sin tocar un `Settings`
#: congelado. Ver la nota de `build_chat_url` en `backend/apps/llm_router/client.py`.
TOP_K_POR_DEFECTO = 4

#: Encabezado de la seccion de contexto en el prompt del sistema.
#:
#: Las lineas de guiones no son adorno: son la convencion que el modelo reconoce como
#: separador de una seccion inyectada. Un encabezado sin separador se lee como parte de las
#: instrucciones de Red Team, que es el defecto que hace que el modelo cite una regla del
#: cliente como si fuera suya.
ENCABEZADO_CONTEXTO = (
    "# REGLAS DE NEGOCIO Y CONTEXTO DEL WORKSPACE (OKF)\n"
    "\n"
    "Lo que sigue son documentos del propio cliente. Sus reglas mandan sobre cualquier\n"
    "suposicion general que puedas tener sobre como funciona su sistema. Cuando una respuesta\n"
    "dependa de una de estas reglas, di de que documento sale. Si el contexto no cubre lo que\n"
    "se te pregunta, dilo: es preferible a inventar una regla que el cliente no ha escrito.\n"
    "\n"
    "---"
)

#: Instrucciones base del asistente. Es identidad y criterio, no configuracion: cambiar el
#: tono del producto es un cambio de copy, no un parametro de despliegue, y por eso se declara
#: en el modulo. Lo que **si** es configurable —temperatura, tope de tokens, cuanto historial,
#: cuantos documentos— vive en `settings`.
INSTRUCCIONES_BASE = """\
Eres Fenix, consultor senior de Red Team. Ayudas a un equipo de seguridad a decidir que
auditar y como, y respondes sobre sus sistemas, sus codigos y sus reglas.

Como respondes:

- **Directo y tecnico.** Vienes del equipo que hace las pruebas, no de un chatbot de
  consumo. Sin introducciones ni resumenes de lo que te han preguntado.
- **Sobre riesgos reales.** Dices que es explotable y por que, o dices que no lo es y por
  que. "Podria ser un problema" sin el camino concreto no es una respuesta.
- **Con el riesgo primero.** Si hay varios hallazgos, el mas grave abre. El orden lo marca
  la gravedad, no el orden en que se te ocurrio.
- **Sin inventar.** No inventas nombres de funciones, de endpoints, de puertos ni de
  identificadores de vulnerabilidad que no aparezcan en el contexto o en lo que el usuario te
  ha dado. Si no lo sabes, dices que no lo sabes. Una suposicion presentada como hecho es el
  peor resultado que puedes dar, porque el usuario la va a verificar.
- **Diciendo de donde sale cada afirmacion.** Cuando un dato viene de un documento del
  cliente, lo dices. Cuando viene de tu criterio general, tambien.
- **Con limites claros.** Si la peticion excede lo que puedes ver, dices que parte se te
  escapa. Un analisis que no se sabe acotado se lee como completo.

Formato: Markdown. Listas para los pasos y los hallazgos, bloques de codigo con el lenguaje
para lo que sea codigo o peticion HTTP, y tablas solo cuando de verdad haya una comparacion
con la misma forma en todas las filas.
"""

#: Lo que se le dice al modelo cuando el workspace no tiene documentos de contexto.
#:
#: No es un silencio. Un encabezado de "REGLAS DE NEGOCIO" seguido de nada deja al modelo
#: esperando documentos que no llegan, y algunos lo explicitan solos con un texto que el
#: usuario no ha escrito. Decirselo aqui convierte la ausencia en informacion: el modelo
#: responde con criterio general y **sabe** que su respuesta es de criterio general.
SIN_CONTEXTO = (
    "\n\nEste workspace no tiene todavia documentos de contexto, asi que no puedes "
    "apoyarte en sus reglas propias. Responde con criterio general y haz explicito cuando "
    "una respuesta dependa de como funcione su sistema.\n"
)


@dataclass(frozen=True)
class OpcionesDeContexto:
    """Lo que el usuario ha adjuntado al paso, ya validado.

    ## Por que esto es un objeto y no un `dict` sin validar

    Porque `context_options` llega del cliente y decide **que se le afirma al modelo sobre lo
    que el usuario ha acotado**. Un `dict` libre convierte el prompt en un canal por el que un
    integrador puede escribir "ignora las reglas anteriores" y el modelo lo seguiria, y ademas
    el tipado no obliga a ningun sitio a manejar las claves. Con un tipo inmutable y campos
    declarados, lo que no existe no se puede citar.
    """

    #: Credenciales **de contexto**, nunca secretos. Es la declaracion de que el usuario ha
    #: pegado material sensible y espera que no se repita en la respuesta.
    credenciales_de_contexto: bool = False
    #: Dominios que el usuario ha acotado. Vacio significa "sin acotar", que es distinto de
    #: "acotado a nada", y por eso es una lista y no un flag: un flag no distinguiria un caso
    #: del otro y el modelo leeria "sin dominios" como "no hay ninguno".
    dominios: tuple[str, ...] = ()
    #: Repositorios que el usuario ha acotado.
    repositorios: tuple[str, ...] = ()

    @property
    def hay_algo(self) -> bool:
        return self.credenciales_de_contexto or bool(self.dominios) or bool(self.repositorios)


@dataclass(frozen=True)
class PasoDeChat:
    """Lo que devolvio un paso: el texto, lo que costo y si se pudo medir.

    `cargo` es `None` cuando el proveedor no publico consumo, y esa ausencia **es** el dato:
    el llamador la usa para no presentar un cero como si fuera un cobro.
    """

    texto: str
    model_id: str
    tokens_in: int
    tokens_out: int
    #: `None` cuando el consumo no se pudo verificar. No es un cargo de cero: es la ausencia
    #: de un cargo, y la interfaz los muestra distinto a proposito.
    cargo: ChatStepCharge | None
    #: Los documentos que se han inyectado, para que la interfaz pueda decir de donde salio
    #: el contexto. Es lo que permite al usuario dudar de una respuesta.
    documentos: tuple[RetrievedDocument, ...] = ()
    #: Que el cobro quedo sin verificar. Se propaga al mensaje para que el rastro y lo que ve
    #: el usuario sean el mismo hecho.
    consumo_no_verificable: bool = False

    @property
    def credits(self) -> str:
        """El coste en creditos, formateado para la interfaz.

        Cuando no se pudo verificar devuelve `"0"`, no `"0.0000"`: un cero con cuatro
        decimales se lee como un cobro medido de cero, que no es lo que ocurrio. La interfaz
        lo distingue con `consumo_no_verificable`, que es donde cabe esa distincion.
        """

        if self.cargo is None:
            return "0"
        return f"{self.cargo.credits.normalize():f}"


# --------------------------------------------------------------------------- #
# La recuperacion
# --------------------------------------------------------------------------- #


def formatear_bloque(documento: RetrievedDocument) -> str:
    """El bloque de contexto tal como viaja en el prompt.

    Delega en el propio documento, que ya sabe presentarse. No se recompone aqui: un segundo
    sitio que compone el bloque es un segundo sitio donde el formato puede cambiar sin que la
    presentacion que el modelo ve y la que el usuario reconoce se separen.
    """

    return documento.as_context_block()


async def recuperar_contexto(
    organization_id: uuid.UUID,
    query: str,
    db: AsyncSession,
    limit: int = TOP_K_POR_DEFECTO,
) -> list[RetrievedDocument]:
    """Los documentos de contexto relevantes para una pregunta.

    ## Por que el filtro por organizacion no se puede relajar aqui

    Porque esta funcion alimenta el prompt, y el prompt es la superficie donde un fallo de
    aislamiento se convierte en contenido. Un tenant que recibiera en su contexto un documento
    de otro no veria un identificador raro en una lista: veria **las reglas de negocio de su
    competidor** respondiendo a su pregunta, sin forma de notar que no son suyas.

    ## Por que degrada a lista vacia y no lanza

    Porque **no tener documentos es el caso normal**, no un error: un workspace recien creado
    no tiene ninguno, y el chat tiene que funcionar igualmente. Lanzar dejaria el chat entero
    sin responder por un documento que no hay, que es la peor relacion entre un problema
    pequeno y una consecuencia grande.

    Degradar tambien es lo correcto: el modelo responde con su criterio general y el usuario ve
    que no habia documentos suyos, en vez de un error que no dice que le falta contexto.
    """

    if not query.strip():
        return []

    try:
        return await recuperar_documentos(db, organization_id, query, top_k=limit)
    except Exception:
        # La recuperacion es un adorno. Si la consulta falla, el chat responde igual sin
        # contexto. Se registra y se sigue: dejar caer la peticion entera por un fallo de
        # busqueda convertiria un problema de la base en una interrupcion del chat, que es
        # una dependencia que el usuario no sabe que tiene.
        #
        # El `except` es amplio a proposito. Cualquier fallo de la base significa lo mismo
        # aqui: no hay contexto. Acotarlo a una lista de excepciones seria escribir dos veces
        # la misma respuesta para el mismo sintoma, y olvidar uno.
        logger.exception(
            "La recuperacion de contexto fallo; el paso continua sin documentos de contexto"
        )
        return []


async def retrieve_rag_context(
    organization_id: uuid.UUID,
    query: str,
    db: AsyncSession,
    limit: int = TOP_K_POR_DEFECTO,
) -> list[str]:
    """Los bloques de contexto, ya formateados para el prompt.

    Esta es la **interfaz del RAG**: lo unico que el ensamblado del prompt conoce. Quien
    implemente la busqueda vectorial devuelve las mismas cadenas desde otro sitio y no toca
    nada mas. El runner no sabe si detras de la lista hay una consulta coseno o un `ILIKE`
    ponderado, y esa es exactamente la que interesa.
    """

    documentos = await recuperar_contexto(organization_id, query, db, limit)
    return [formatear_bloque(documento) for documento in documentos]


# --------------------------------------------------------------------------- #
# El prompt
# --------------------------------------------------------------------------- #


def _seccion_de_alcance(opciones: OpcionesDeContexto) -> str:
    """Lo que el usuario ha acotado, como instruccion explicita.

    Se declara **como instruccion** y no se menciona de pasada. Un "el usuario ha adjuntado
    repositorios" en medio de la prosa es algo que el modelo puede ignorar; "acota el analisis
    a estos repositorios y no propongas vectores fuera de ellos" es una instruccion, y es lo que
    el usuario quiere que ocurra.
    """

    lineas: list[str] = []
    if opciones.credenciales_de_contexto:
        lineas.append(
            "- El usuario ha adjuntado material sensible. Analizalo, pero **no repitas "
            "ningun valor de credencial en la respuesta**: describelo por su tipo y su "
            "alcance, nunca por su contenido literal."
        )
    if opciones.dominios:
        listados = ", ".join(opciones.dominios)
        lineas.append(
            f"- Acota el analisis a estos dominios: {listados}. No propongas vectores de "
            "ataque contra activos que no esten en esa lista."
        )
    if opciones.repositorios:
        listados = ", ".join(opciones.repositorios)
        lineas.append(
            f"- Acota el analisis a estos repositorios: {listados}. No hables de hallazgos "
            "que no puedas situar en su codigo."
        )
    if not lineas:
        return ""
    return "\n\n## ALCANCE DE ESTE PASO\n\n" + "\n".join(lineas) + "\n"


def construir_system_prompt(
    bloques_de_contexto: Sequence[str],
    opciones: OpcionesDeContexto,
) -> str:
    """El prompt del sistema de un paso.

    El orden es el que espera el modelo: **instrucciones, despues contexto, despues
    alcance**. Las instrucciones van primero porque son lo que gobierna la lectura de todo lo
    que viene detras. El alcance va ultimo porque acota lo que las instrucciones producen.
    """

    alcance = _seccion_de_alcance(opciones)

    if not bloques_de_contexto:
        return INSTRUCCIONES_BASE + SIN_CONTEXTO + alcance

    contexto = "\n\n".join(bloques_de_contexto)
    return f"{INSTRUCCIONES_BASE}\n\n{ENCABEZADO_CONTEXTO}\n\n{contexto}\n{alcance}"


# --------------------------------------------------------------------------- #
# El historial
# --------------------------------------------------------------------------- #


async def cargar_historial(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limite: int,
) -> tuple[LlmTurn, ...]:
    """Los ultimos mensajes de la conversacion, en orden cronologico.

    ## Por que se ordena **antes** de recortar y no despues

    Porque el `LIMIT` se aplica sobre lo que el planificador decida, y si el `ORDER BY` va en
    la misma consulta el recorte es correcto. El fallo aparece cuando se recorta por fecha en
    la base y luego se ordena en memoria sin haber garantizado cual era la ventana: se
    recorta el conjunto equivocado y se entrega un historial que salta mensajes al principio y
    repite el final, y parece un fallo del modelo cuando es una consulta mal escrita.

    ## Por que el desempate por rol se invierte tambien

    Porque el `LIMIT` se aplica a la consulta en descendente. Si solo se invirtiera la marca de
    tiempo y no el rol, al dar la vuelta el rol volveria a su orden original y los dos
    mensajes del mismo turno —que comparten `created_at` porque se insertan en la misma
    transaccion— saldrian desordenados otra vez. Ver `PESO_DE_ROL` en `models.py`.

    ## Por que se descartann los mensajes de rol `system`

    Porque las instrucciones del sistema se montan aparte, en su propio turno, y dependen de
    la pregunta actual: cambian con el contexto recuperado y con el alcance. Reenviar un
    `system` guardado seria enviar unas instrucciones viejas junto a las nuevas, y con dos
    system contradictorios el proveedor no tiene forma de saber cual gana.
    """

    if limite <= 0:
        return ()

    filas = (
        (
            await session.execute(
                select(ChatMessage.role, ChatMessage.content)
                .where(
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role != ChatRoleEnum.SYSTEM,
                )
                # Se ordena en descendente para que el `LIMIT` se quede con los mas
                # recientes, y el desempate va **tambien** en descendente para que al dar la
                # vuelta el resultado quede en el orden correcto. Invertir solo la marca y no
                # el rol daria la vuelta al rol y volveria a mezclar los dos mensajes
                # del mismo turno.
                # mismo turno.
                .order_by(ChatMessage.created_at.desc(), PESO_DE_ROL.desc())
                .limit(limite)
            )
        )
        .all()
    )

    # El `LIMIT` se quedo con los mas recientes en orden inverso, que es lo que necesita una
    # consulta descendente: se da la vuelta para devolverlos en orden cronologico, que es
    # como los leera el modelo.
    return tuple(LlmTurn(role=str(rol), content=contenido) for rol, contenido in reversed(filas))


# --------------------------------------------------------------------------- #
# El paso
# --------------------------------------------------------------------------- #


async def ejecutar_paso_de_chat(
    session: AsyncSession,
    *,
    conversation: ChatConversation,
    mensaje_id: uuid.UUID,
    pregunta: str,
    opciones: OpcionesDeContexto,
    client: httpx.AsyncClient | None = None,
) -> PasoDeChat:
    """Ejecuta un paso completo: contexto, inferencia y liquidacion.

    ## Por que `mensaje_id` lo genera quien llama

    Porque es lo que hace que el cobro sea idempotente y trazable a la vez. El `reference_id`
    del asiento es este mismo identificador, y la fila del mensaje se persiste despues **con
    ese identificador**: el asiento y el mensaje comparten clave, de forma que un reintento
    de la misma peticion no puede crear un segundo cobro —el ledger es append-only y solo
    admite `INSERT`- y una disputa de facturacion se resuelve mirando un mensaje concreto,
    no sumando la conversacion entera.

    Si el identificador lo generase el runner, el asiento saldria con el id de la conversacion
    y habria que elegir entre un `uuid4` distinto por paso —que hace el reintento no
    idempotente— o buscar el ultimo asiento, que es una carrera.

    `client` se inyecta para que las pruebas usen `MockTransport` y no una red. En produccion
    se deja a `None` y el cliente abre y cierra su conexion, que es lo correcto para un
    servicio que hace pocas llamadas.
    """

    organization_id = conversation.organization_id

    # El modelo se resuelve **antes** de gastar en la recuperacion. Si no hay ninguno activo la
    # llamada va a fallar, y los documentos recuperados no habrian servido de nada. El orden no
    # es una optimizacion: es la diferencia entre consultar la base para nada y no consultarla.
    modelo = await resolve_chat_model(session)

    documentos = await recuperar_contexto(
        organization_id, pregunta, session, limit=settings.chat_context_documents
    )
    historial = await cargar_historial(
        session, conversation.id, limite=settings.chat_history_messages
    )
    system_prompt = construir_system_prompt(
        [formatear_bloque(documento) for documento in documentos], opciones
    )

    completion = await complete(
        model=modelo.model_id,
        prompt=pregunta,
        system=system_prompt,
        temperature=settings.chat_temperature,
        max_tokens=settings.chat_max_tokens,
        history=historial,
        api_base=settings.llm_api_base,
        api_key=settings.llm_api_key,
        client=client,
    )
    model_id = completion.model or modelo.model_id

    if completion.has_usage:
        cargo: ChatStepCharge | None = await liquidar_paso_de_chat(
            session,
            organization_id=organization_id,
            reference_id=str(mensaje_id),
            model=modelo,
            tokens_in=completion.prompt_tokens,
            tokens_out=completion.completion_tokens,
        )
        consumo_no_verificable = False
    else:
        cargo = None
        consumo_no_verificable = True
        await _registrar_consumo_no_verificable(
            session,
            organization_id=organization_id,
            conversation_id=conversation.id,
            model_id=model_id,
        )

    return PasoDeChat(
        texto=completion.text,
        model_id=model_id,
        tokens_in=completion.prompt_tokens,
        tokens_out=completion.completion_tokens,
        cargo=cargo,
        documentos=tuple(documentos),
        consumo_no_verificable=consumo_no_verificable,
    )


async def _registrar_consumo_no_verificable(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    model_id: str,
) -> None:
    """Deja constancia de un paso que no se pudo cobrar.

    Va a `audit_log` y no a un `log` porque responde a una pregunta de facturacion —"este
    mensaje no se cobro, y por que"— y el rastro append-only es lo unico que se puede citar
    dentro de seis meses. Un `log` se rota, no se filtra por organizacion, y no tiene fila
    propia que consultar.

    `entity_type` es `chat_message` y no `chat_conversation` porque la pregunta se hace de un
    mensaje. Aqui `entity_id` lleva el id de la conversacion porque el mensaje todavia no
    existe cuando se escribe el rastro: se emite antes de persistir la respuesta. El vinculo
    con el mensaje concreto lo da el `reference_id` del asiento cuando lo hubo, y cuando no lo
    hubo, la conversacion y su orden temporal bastan para reconstruirlo.
    """

    session.add(
        AuditLogEntry(
            organization_id=organization_id,
            actor_user_id=None,
            action=AuditActionEnum.UNREPORTED_USAGE_UNBILLED,
            entity_type="chat_message",
            entity_id=conversation_id,
            from_state=None,
            to_state=None,
        )
    )
    logger.warning(
        "Paso de chat sin consumo verificable: no se cobra. conversation=%s model=%s",
        conversation_id,
        model_id,
    )


__all__ = [
    "ENCABEZADO_CONTEXTO",
    "INSTRUCCIONES_BASE",
    "SIN_CONTEXTO",
    "TOP_K_POR_DEFECTO",
    "OpcionesDeContexto",
    "PasoDeChat",
    "cargar_historial",
    "construir_system_prompt",
    "ejecutar_paso_de_chat",
    "formatear_bloque",
    "recuperar_contexto",
    "retrieve_rag_context",
]
