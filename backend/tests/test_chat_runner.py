"""Pruebas del runner del chat: prompt, historial, contexto y politica de cobro.

## Que se comprueba y por que

El runner concentra las tres decisiones que no se pueden deshacer:

- **Que entra en el prompt.** Un documento de otro tenant, una instruccion que el cliente no
  escribio o un historial desordenado no se ven en ningun sitio: se ven en lo que el modelo
  responde. Por eso la prueba de aislamiento va la primera.
- **Que se cobra.** El ledger es append-only y no admite correcciones, asi que un cobro
  equivocado no se puede arreglar despues: hay que acertar la primera vez.
- **Que ocurre cuando no se puede medir.** Es el camino que solo se ve cuando el proveedor se
  rompe, que es cuando hay que acertar mas.

## Por que el modelo se borra al terminar

Porque el catalogo de modelos LLM es **global**, no por tenant. Una prueba que inserta un
modelo y no lo borra deja un activo mas en la base compartida, y `test_llm_catalog_and_runner`
afirma la igualdad exacta de los activos: falla por un `assert` de una prueba anterior, con un
mensaje que no señala esta. El `finally` con `commit` no es opcional.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.chat.models import ChatConversation, ChatMessage, ChatRoleEnum
from backend.apps.chat.runner import (
    OpcionesDeContexto,
    construir_system_prompt,
    ejecutar_paso_de_chat,
    recuperar_contexto,
    retrieve_rag_context,
)
from backend.apps.knowledge.documents import KnowledgeDocTypeEnum, WorkspaceKnowledgeDocument
from backend.apps.knowledge.okf import parse_okf
from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.core.config import settings
from backend.core.database import AsyncSessionLocal
from backend.core.security import hash_password

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Andamiaje
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    sesion = AsyncSessionLocal()
    try:
        yield sesion
    finally:
        await sesion.close()


async def _tenant(session: AsyncSession, prefijo: str) -> tuple[uuid.UUID, uuid.UUID]:
    from backend.apps.organizations.models import Membership, Organization, RoleEnum, User

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"{prefijo} {suffix}", slug=f"{prefijo}-{suffix}")
    user = User(
        email=f"{prefijo}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Cliente",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization.id, user.id


async def _saldo(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Da saldo al tenant de la prueba.

    Hace falta porque un tenant recien creado tiene **cero** creditos, y `apply_credit_delta`
    rechaza el movimiento que dejaria el saldo negativo. Es decir: sin este sembrado, cualquier
    prueba que llegue a la liquidacion falla con `InsufficientCreditsError` y no por lo que
    quiere comprobar.

    Se siembra con el propio `apply_credit_delta` y no escribiendo `credit_balance` a mano,
    porque el saldo **se deriva** del ledger —es un `SUM`— y tocarlo por la puerta de atras
    dejaria un tenant con saldo positivo y ledger vacio, que es un estado que ninguna consulta
    real produce y que haria pasar una prueba de saldo por algo que el sistema no permite.
    """

    await apply_credit_delta(
        session=session,
        organization_id=organization_id,
        amount=Decimal("100"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
        reference_id=f"siembra-{uuid.uuid4().hex[:8]}",
    )
    await session.commit()


async def _modelo_chat(session: AsyncSession) -> LLMModelConfig:
    """Un modelo activo de caso de uso `CHAT`, y el `finally` que lo borra.

    El `finally` va **adentro** de la funcion a proposito: si el `commit` del borrado se
    ejecutara al final de la prueba, una prueba que lanzara antes de llegar a el dejaria el
    modelo en la base. Se devuelve el `id` y el llamador lo borra con su propio `finally`, que
    es el unico sitio donde se sabe que la prueba ha terminado de verdad.
    """

    modelo = LLMModelConfig(
        model_id=f"openai/chat-falso-{uuid.uuid4().hex[:8]}",
        display_name="Modelo de prueba",
        base_cost_input_m=Decimal("1.00"),
        base_cost_output_m=Decimal("2.00"),
        markup_pct=100,
        priority_order=1,
        is_active=True,
        use_case=LLMUseCaseEnum.CHAT,
    )
    session.add(modelo)
    await session.commit()
    return modelo


async def _borrar_modelo(session: AsyncSession, modelo_id: uuid.UUID) -> None:
    """Borra el modelo de la prueba con `commit`, que es lo que la base compartida necesita."""

    async with AsyncSessionLocal() as propia:
        await propia.execute(
            delete(LLMModelConfig).where(LLMModelConfig.id == modelo_id)
        )
        await propia.commit()


def _respuesta(
    texto: str = "Respuesta de prueba",
    *,
    con_usage: bool = True,
    model: str = "openai/chat-falso",
) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "id": "gen-falso",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": texto},
                "finish_reason": "stop",
            }
        ],
    }
    if con_usage:
        cuerpo["usage"] = {"prompt_tokens": 1_000, "completion_tokens": 500}
    return cuerpo


def _proveedor(
    cuerpo: dict[str, Any], *, codigo: int = 200, capturado: list[httpx.Request] | None = None
) -> httpx.AsyncClient:
    """Un cliente con `MockTransport` que responde el cuerpo dado.

    `capturado` recibe las peticiones para poder inspeccionar cabeceras y cuerpo. Existe
    porque la atribucion y el orden del historial **no se ven en la respuesta**: hay que mirar
    lo que salio.
    """

    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        if capturado is not None:
            capturado.append(peticion)
        return httpx.Response(codigo, json=cuerpo)

    return httpx.AsyncClient(transport=httpx.MockTransport(_manejador))


async def _conversacion(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> ChatConversation:
    conversacion = ChatConversation(
        organization_id=organization_id, user_id=user_id, title="Prueba"
    )
    session.add(conversacion)
    await session.commit()
    return conversacion


def _okf(*, tipo: str, titulo: str, descripcion: str, cuerpo: str) -> str:
    return f'---\ntype: {tipo}\ntitle: "{titulo}"\ndescription: "{descripcion}"\n---\n\n{cuerpo}\n'


# --------------------------------------------------------------------------- #
# El prompt
# --------------------------------------------------------------------------- #


async def test_el_limite_por_defecto_de_la_recuperacion_es_el_de_la_configuracion() -> None:
    """La firma de la funcion y la configuracion dicen el mismo numero.

    Es una invariante pequena y facil de romper: alguien sube `CHAT_CONTEXT_DOCUMENTS` en el
    `.env` para meter mas contexto y actualiza solo la configuracion, y a partir de ahi la
    funcion pura y lo que hace el runner devuelven cosas distintas. No hay ningun sintoma
    visible —las dos funcionan— y el que llama a la funcion esperando el valor por defecto
    recibe otra cosa.
    """

    from backend.apps.knowledge.retrieval import TOP_K_POR_DEFECTO

    assert settings.chat_context_documents == TOP_K_POR_DEFECTO


# --------------------------------------------------------------------------- #
# El prompt
# --------------------------------------------------------------------------- #


async def test_sin_documentos_el_prompt_lo_dice_explicitamente() -> None:
    """La ausencia de contexto es informacion, no un hueco silencioso.

    Sin este aviso, el modelo no sabe que no hay reglas del cliente y responde con la misma
    seguridad que si las hubiera. El usuario no tiene forma de notar la diferencia, y por eso
    es el prompt el que tiene que avisar.
    """

    prompt = construir_system_prompt([], OpcionesDeContexto())
    assert "no tiene todavia documentos de contexto" in prompt


async def test_con_documentos_lleva_el_encabezado_y_el_cuerpo() -> None:
    """Los documentos van bajo su encabezado, con su contenido y su descripcion."""

    documento = parse_okf(
        _okf(
            tipo="business_rule",
            titulo="Politica de sesiones",
            descripcion="Como se validan las sesiones.",
            cuerpo="Las sesiones duran 30 minutos.",
        )
    )
    from backend.apps.knowledge.retrieval import RetrievedDocument

    bloque = RetrievedDocument(
        document_id=uuid.uuid4(),
        title=documento.title,
        doc_type=documento.doc_type.value,
        description=documento.description,
        body=documento.body,
        score=1.0,
        matched_terms=("sesion",),
    ).as_context_block()

    prompt = construir_system_prompt([bloque], OpcionesDeContexto())
    assert "REGLAS DE NEGOCIO Y CONTEXTO DEL WORKSPACE (OKF)" in prompt
    assert "Politica de sesiones" in prompt
    assert "Las sesiones duran 30 minutos." in prompt
    # Sin contexto, la seccion no debe aparecer.
    assert "no tiene todavia documentos" not in prompt


async def test_el_alcance_de_dominios_llega_como_instruccion() -> None:
    """Lo que el usuario acota se declara como orden, no como nota.

    Un "el usuario ha adjuntado repositorios" en medio de la prosa es algo que el modelo puede
    ignorar. "Acota el analisis a estos repositorios" es una instruccion.
    """

    prompt = construir_system_prompt(
        [], OpcionesDeContexto(dominios=("api.cliente.com",), repositorios=("backend",))
    )
    assert "api.cliente.com" in prompt
    assert "Acota el analisis a estos dominios" in prompt
    assert "Acota el analisis a estos repositorios" in prompt


async def test_las_credenciales_no_se_piden_por_contenido() -> None:
    """La instruccion de credenciales prohibe **repetir el valor**, no solo citarla.

    Un modelo al que se le da un material sensible tiende a resumirlo, y un resumen de una
    credencial es la credencial. La instruccion tiene que decir "no repitas ningun valor".
    """

    prompt = construir_system_prompt([], OpcionesDeContexto(credenciales_de_contexto=True))
    assert "no repitas" in prompt
    assert "valor de credencial" in prompt


async def test_sin_alcance_no_hay_seccion_de_alcance() -> None:
    """Sin nada acotado no se emite un encabezado de alcance vacío.

    Un "## ALCANCE DE ESTE PASO" seguido de nada deja al modelo esperando una restriccion que
    no existe, y algunos la inventan.
    """

    assert "ALCANCE DE ESTE PASO" not in construir_system_prompt([], OpcionesDeContexto())


# --------------------------------------------------------------------------- #
# La recuperacion
# --------------------------------------------------------------------------- #


async def test_recuperar_contexto_no_trae_documentos_de_otro_tenant() -> None:
    """Las reglas de un tenant no llegan al prompt de otro.

    Es la prueba mas importante del fichero. Un fallo aqui no se ve como un identificador raro
    en una lista: el modelo responderia usando las reglas de negocio de otro cliente, y ni el
    usuario ni el operador tendrian forma de notarlo.
    """

    async with _sesion() as session:
        ajeno, _ = await _tenant(session, "ajeno")
        propio, _ = await _tenant(session, "propio")
        session.add(
            WorkspaceKnowledgeDocument(
                organization_id=ajeno,
                title="Precios del competidor",
                doc_type=KnowledgeDocTypeEnum.BUSINESS_RULE,
                content=_okf(
                    tipo="business_rule",
                    titulo="Precios del competidor",
                    descripcion="Lo que cobra la competencia.",
                    cuerpo="Cobra 12 al mes sin soporte.",
                ),
            )
        )
        await session.commit()

        documentos = await recuperar_contexto(
            propio, "cuales son los precios de la competencia", session
        )
        bloques = await retrieve_rag_context(
            propio, "cuales son los precios de la competencia", session
        )

    assert documentos == []
    assert bloques == []


async def test_una_consulta_vacia_no_consulta_nada() -> None:
    """Una pregunta en blanco no recupera, y no lanza.

    El panel manda el texto tal cual y una cadena vacia es un estado legitimo del formulario,
    no un caso que el backend tenga que tratar como valido.
    """

    async with _sesion() as session:
        organization_id, _ = await _tenant(session, "vacio")
        assert await recuperar_contexto(organization_id, "   ", session) == []


# --------------------------------------------------------------------------- #
# El cobro
# --------------------------------------------------------------------------- #


async def test_con_usage_se_cobra_y_se_guarda_el_consumo() -> None:
    """El camino normal: hay `usage`, hay cobro y el mensaje lo lleva consigo.

    Se comprueban las tres cosas a la vez porque son las tres caras de un mismo hecho. Si el
    asiento existe y el mensaje no lo declara, una disputa de factura se responde leyendo el
    ledger entero; si el mensaje lo declara y el asiento no existe, hay un consumo que no se
    puede facturar.
    """

    async with _sesion() as session:
        organization_id, user_id = await _tenant(session, "cobro")
        await _saldo(session, organization_id)
        conversacion = await _conversacion(session, organization_id, user_id)
        modelo = await _modelo_chat(session)
        message_id = uuid.uuid4()

        try:
            paso = await ejecutar_paso_de_chat(
                session,
                conversation=conversacion,
                mensaje_id=message_id,
                pregunta="Revisa la autenticacion",
                opciones=OpcionesDeContexto(),
                client=_proveedor(_respuesta()),
            )
            await session.commit()
        finally:
            await _borrar_modelo(session, modelo.id)

        asientos = (
            (
                await session.execute(
                    select(CreditLedger).where(
                        CreditLedger.reference_id == str(message_id)
                    )
                )
            )
            .scalars()
            .all()
        )

    assert paso.cargo is not None
    assert paso.consumo_no_verificable is False
    assert paso.tokens_in == 1_000
    assert paso.tokens_out == 500
    assert paso.cargo.credits > 0
    # El asiento y el mensaje comparten identificador: es lo que hace el cobro idempotente.
    assert len(asientos) == 1
    assert asientos[0].reason == LedgerReasonEnum.CHAT_STEP_CONSUMPTION
    assert asientos[0].reference_id == str(message_id)


async def test_sin_usage_no_se_cobra_y_se_deja_rastro() -> None:
    """Sin `usage` no se inventa un cobro, y queda constancia de por que.

    Es la politica acordada y la que mas merece una prueba: el camino alternativo —cobrar una
    estimacion— seria **tambien** codigo que compila y que nadie detectaria, porque el
    `credit_ledger` aceptaria el asiento y el saldo cuadraria. Solo el rastro delata el problema.
    """

    async with _sesion() as session:
        organization_id, user_id = await _tenant(session, "sinusage")
        conversacion = await _conversacion(session, organization_id, user_id)
        modelo = await _modelo_chat(session)
        message_id = uuid.uuid4()

        try:
            paso = await ejecutar_paso_de_chat(
                session,
                conversation=conversacion,
                mensaje_id=message_id,
                pregunta="Hola",
                opciones=OpcionesDeContexto(),
                client=_proveedor(_respuesta(con_usage=False)),
            )
            await session.commit()
        finally:
            await _borrar_modelo(session, modelo.id)

        asientos = (
            (
                await session.execute(
                    select(CreditLedger).where(
                        CreditLedger.reference_id == str(message_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        # El rastro se filtra **por organizacion**, no se cuenta en global. La base es
        # compartida entre pruebas, y contar filas globales hace que esta falle o pase segun
        # lo que hayan dejado las pruebas anteriores: un `assert len(rastros) == 1` aqui
        # estaria midiendo el orden de ejecucion de pytest, no el runner.
        rastros = (
            (
                await session.execute(
                    select(AuditLogEntry).where(
                        AuditLogEntry.organization_id == organization_id,
                        AuditLogEntry.action
                        == AuditActionEnum.UNREPORTED_USAGE_UNBILLED,
                    )
                )
            )
            .scalars()
            .all()
        )

    # El mensaje se entrega igual: el trabajo ya se hizo y ya se gasto.
    assert paso.texto == "Respuesta de prueba"
    assert paso.cargo is None
    assert paso.consumo_no_verificable is True
    assert paso.tokens_in == 0
    assert paso.tokens_out == 0
    # Y no se toca el ledger.
    assert asientos == []
    assert len(rastros) == 1
    assert rastros[0].entity_type == "chat_message"


async def test_usage_a_ceros_tambien_es_consumo_no_verificable() -> None:
    """Un `usage` con los dos numeros a cero **no** es un paso gratis: es una medicion ausente.

    `has_usage` mira que alguno de los dos sea mayor que cero, y es deliberado: un proveedor
    puede devolver el bloque `usage` presente y con ceros cuando no lo ha medido. Confundir
    eso con un paso de coste cero haria que un fallo de medicion pareciera un ahorro, y el
    ledger registraria un cobro de cero que el usuario nunca hizo.
    """

    async with _sesion() as session:
        organization_id, user_id = await _tenant(session, "usage0")
        conversacion = await _conversacion(session, organization_id, user_id)
        modelo = await _modelo_chat(session)

        try:
            paso = await ejecutar_paso_de_chat(
                session,
                conversation=conversacion,
                mensaje_id=uuid.uuid4(),
                pregunta="Hola",
                opciones=OpcionesDeContexto(),
                client=_proveedor(_respuesta(con_usage=False)),
            )
            await session.commit()
        finally:
            await _borrar_modelo(session, modelo.id)

    assert paso.consumo_no_verificable is True
    assert paso.credits == "0"


# --------------------------------------------------------------------------- #
# Lo que sale hacia el proveedor
# --------------------------------------------------------------------------- #


async def test_una_peticion_lleva_las_cabeceras_de_atribucion() -> None:
    """La atribucion viaja en **toda** peticion al proveedor.

    Es lo que hace que la aplicacion aparezca en el catalogo publico de OpenRouter. Y es una
    invariante del cliente, no una preferencia de este endpoint: por eso se comprueba sobre la
    peticion que sale y no sobre el prompt que se arma.
    """

    capturadas: list[httpx.Request] = []
    async with _sesion() as session:
        organization_id, user_id = await _tenant(session, "atr")
        await _saldo(session, organization_id)
        conversacion = await _conversacion(session, organization_id, user_id)
        modelo = await _modelo_chat(session)

        try:
            await ejecutar_paso_de_chat(
                session,
                conversation=conversacion,
                mensaje_id=uuid.uuid4(),
                pregunta="Hola",
                opciones=OpcionesDeContexto(),
                client=_proveedor(_respuesta(), capturado=capturadas),
            )
            await session.commit()
        finally:
            await _borrar_modelo(session, modelo.id)

    cabeceras = capturadas[0].headers
    assert cabeceras["HTTP-Referer"] == "https://mindguardredteam.com"
    assert cabeceras["X-Title"] == "Mind Guard Fenix"


async def test_el_historial_llega_cronologico_y_antes_de_la_ultima_pregunta() -> None:
    """El historial viaja como mensajes, en orden, y la pregunta nueva va la ultima.

    Es la prueba que justifica el `history` del cliente. Un historial concatenado dentro del
    prompt llega como un bloque de prosa en el que la pregunta es la ultima linea, y el
    modelo responde a la prosa. Se comprueba la **posicion** en la lista de `messages` y no
    solo que las palabras aparezcan, porque aparecer en cualquier sitio tambien es el
    sintoma del fallo.
    """

    capturadas: list[httpx.Request] = []
    async with _sesion() as session:
        organization_id, user_id = await _tenant(session, "hist")
        await _saldo(session, organization_id)
        conversacion = await _conversacion(session, organization_id, user_id)
        session.add_all(
            [
                ChatMessage(
                    conversation_id=conversacion.id,
                    role=ChatRoleEnum.USER,
                    content="Primera pregunta sobre sesiones",
                    tokens_in=0,
                    tokens_out=0,
                    credits_cost=Decimal("0.0000"),
                ),
                ChatMessage(
                    conversation_id=conversacion.id,
                    role=ChatRoleEnum.ASSISTANT,
                    content="Primera respuesta sobre sesiones",
                    tokens_in=0,
                    tokens_out=0,
                    credits_cost=Decimal("0.0000"),
                ),
            ]
        )
        await session.commit()
        modelo = await _modelo_chat(session)

        try:
            await ejecutar_paso_de_chat(
                session,
                conversation=conversacion,
                mensaje_id=uuid.uuid4(),
                pregunta="Segunda pregunta sobre tokens",
                opciones=OpcionesDeContexto(),
                client=_proveedor(_respuesta(), capturado=capturadas),
            )
            await session.commit()
        finally:
            await _borrar_modelo(session, modelo.id)

    cuerpo = json.loads(capturadas[0].content)
    mensajes = cuerpo["messages"]

    # El system va primero, despues los dos del historial en orden, y la pregunta al final.
    roles = [m["role"] for m in mensajes]
    assert roles == ["system", "user", "assistant", "user"]
    assert "Primera pregunta" in mensajes[1]["content"]
    assert "Primera respuesta" in mensajes[2]["content"]
    assert mensajes[-1]["content"] == "Segunda pregunta sobre tokens"


async def test_el_tope_de_tokens_llega_en_el_cuerpo() -> None:
    """El tope configurado viaja en la peticion.

    Acota lo que un paso puede costar **antes** de que el proveedor decida pararlo, que es la
    diferencia entre un cobro acotado y uno que se descubre en la factura.
    """

    capturadas: list[httpx.Request] = []
    async with _sesion() as session:
        organization_id, user_id = await _tenant(session, "tope")
        await _saldo(session, organization_id)
        conversacion = await _conversacion(session, organization_id, user_id)
        modelo = await _modelo_chat(session)

        try:
            await ejecutar_paso_de_chat(
                session,
                conversation=conversacion,
                mensaje_id=uuid.uuid4(),
                pregunta="Hola",
                opciones=OpcionesDeContexto(),
                client=_proveedor(_respuesta(), capturado=capturadas),
            )
            await session.commit()
        finally:
            await _borrar_modelo(session, modelo.id)

    cuerpo = json.loads(capturadas[0].content)
    assert cuerpo["max_tokens"] == settings.chat_max_tokens
    assert cuerpo["temperature"] == settings.chat_temperature
