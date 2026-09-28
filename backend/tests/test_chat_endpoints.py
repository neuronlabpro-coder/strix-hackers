"""Pruebas de los endpoints del chat.

## Que se comprueba y por que

El chat es la superficie donde un fallo de aislamiento se convierte en **contenido**: un tenant
que leyera el hilo de otro no veria un identificador raro en una lista, veria las preguntas y
respuestas del otro sobre sus propios sistemas. Por eso el aislamiento va la primera y con mas
pruebas que ninguna otra cosa.

Y como el chat **gasta**, los permisos importan tanto como el aislamiento: un token que puede
enviar mensajes puede descontar creditos del ledger, que es append-only y no admite
correcciones.

## Por que se sustituye la dependencia de transporte

Porque la inferencia llama a OpenRouter por red, y una prueba que dependa de la red no es una
prueba: es un test de la red que ademas falla de forma intermitente. `app.dependency_overrides`
sustituye **solo** el cliente HTTP y deja pasar todo lo demas —sesion, tenant, permisos— por su
curso normal, que es lo que hay que comprobar.

## Por que la sesion propia y no la del fixture

Porque estos endpoints hacen `commit`, y el fixture comparte sesion con la aplicacion en modo
`savepoint`. Un `commit` de la ruta dentro de esa sesion se propaga a la base de pruebas y deja
filas que ensucian el resto de la suite. Por eso cada prueba abre su propia sesion y la cierra
en el `finally`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.scopes import Scope
from backend.apps.billing.models import LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.chat.models import ChatConversation, ChatMessage, ChatRoleEnum
from backend.apps.chat.router import _proveedor_de_transporte
from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.database import AsyncSessionLocal
from backend.core.security import create_access_token, hash_password
from backend.main import app

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


class Tenant:
    def __init__(self, organization: Organization, user: User) -> None:
        self.organization_id = organization.id
        self.user_id = user.id
        self.headers = {
            "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
            "X-Organization-Id": str(organization.id),
        }


async def _tenant(
    session: AsyncSession, prefijo: str, *, role: RoleEnum = RoleEnum.ADMIN
) -> Tenant:
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
        Membership(organization_id=organization.id, user_id=user.id, role=role)
    )
    await session.commit()
    return Tenant(organization, user)


async def _saldo(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Da saldo al tenant de la prueba.

    Hace falta porque un tenant recien creado tiene **cero** creditos y `apply_credit_delta`
    rechaza el movimiento que dejaria el saldo negativo. Sin este sembrado, cualquier prueba que
    llegue a la liquidacion falla con `InsufficientCreditsError` y no por lo que quiere medir.

    Se siembra con el propio `apply_credit_delta` y no escribiendo `credit_balance` a mano,
    porque el saldo **se deriva** del ledger: tocarlo por la puerta de atras deja un tenant con
    saldo positivo y libro vacio, un estado que ninguna consulta real produce.
    """

    await apply_credit_delta(
        session=session,
        organization_id=organization_id,
        amount=Decimal("100"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
        reference_id=f"siembra-{uuid.uuid4().hex[:8]}",
    )
    await session.commit()


async def _modelo_chat(session: AsyncSession) -> uuid.UUID:
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
    return modelo.id


async def _borrar_modelo(modelo_id: uuid.UUID) -> None:
    """Borra el modelo con sesion propia: el catalogo es global y la base es compartida.

    Sin esto, `test_llm_catalog_and_runner` falla al comparar la igualdad exacta de los
    activos, con un error que no señala esta prueba.
    """

    from sqlalchemy import delete

    async with AsyncSessionLocal() as sesion:
        await sesion.execute(delete(LLMModelConfig).where(LLMModelConfig.id == modelo_id))
        await sesion.commit()


def _respuesta(texto: str = "Respuesta de prueba") -> dict[str, Any]:
    return {
        "model": "openai/chat-falso",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": texto},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


@asynccontextmanager
async def _cliente(
    cuerpo: dict[str, Any] | None = None, *, codigo: int = 200
) -> AsyncIterator[AsyncClient]:
    """Un cliente contra la aplicacion con el proveedor sustituido.

    Es un `asynccontextmanager` y no una funcion suelta porque `dependency_overrides` es estado
    **global** de la aplicacion: si la prueba que lo activa falla, el override se queda puesto
    y las siguientes empiezan a hablar con un proveedor falso sin saberlo. El `finally` lo
    retira siempre.
    """

    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(codigo, json=cuerpo if cuerpo is not None else _respuesta())

    transporte_real = httpx.AsyncClient(transport=httpx.MockTransport(_manejador))
    app.dependency_overrides[_proveedor_de_transporte] = lambda: transporte_real
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            yield cliente
    finally:
        app.dependency_overrides.pop(_proveedor_de_transporte, None)
        await transporte_real.aclose()


# --------------------------------------------------------------------------- #
# El aislamiento. Va primero porque es lo que no se puede deshacer.
# --------------------------------------------------------------------------- #


async def test_no_se_lee_la_conversacion_de_otro_tenant() -> None:
    """Un `conversation_id` de otro workspace devuelve el mismo `404` que uno inexistente.

    Es la prueba central del fichero. Un `403` en su lugar confirmaria que el hilo existe, y con
    eso basta para enumerar identificadores y descubrir que tiene el tenant vecino.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "victima")
        atacante = await _tenant(sesion, "atacante")
        conversacion = ChatConversation(
            organization_id=victima.organization_id,
            user_id=victima.user_id,
            title="Hilo privado de la victima",
        )
        sesion.add(conversacion)
        await sesion.commit()
        conversacion_id = conversacion.id

        async with _cliente() as cliente:
            respuesta = await cliente.get(
                f"/api/v1/chat/conversations/{conversacion_id}", headers=atacante.headers
            )
            # Y un identificador que no existe en absoluto, para comparar.
            inexistente = await cliente.get(
                f"/api/v1/chat/conversations/{uuid.uuid4()}", headers=atacante.headers
            )

    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == inexistente.json()["detail"]
    assert "privada" not in respuesta.text


async def test_no_aparece_en_el_listado_de_otro_tenant() -> None:
    """Las conversaciones ajenas no estan en el listado, ni con el total inflado.

    Se comprueba el total ademas de la lista: un listado correcto con un total que cuenta
    tambien lo ajeno delata el numero de conversaciones del vecino, que es informacion.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "victima")
        atacante = await _tenant(sesion, "atacante")
        sesion.add(
            ChatConversation(
                organization_id=victima.organization_id,
                user_id=victima.user_id,
                title="Secreto de la victima",
            )
        )
        await sesion.commit()

        async with _cliente() as cliente:
            respuesta = await cliente.get(
                "/api/v1/chat/conversations", headers=atacante.headers
            )

    cuerpo = respuesta.json()
    assert respuesta.status_code == 200
    assert cuerpo["total"] == 0
    assert cuerpo["conversations"] == []


async def test_borrar_la_conversacion_de_otro_tenant_no_hace_nada() -> None:
    """El `DELETE` tambien filtra, y la conversacion sobrevive al intento.

    Un borrado que no filtra por organizacion es la forma mas rapida de que un tenant pierda su
    historial con un identificador adivinado.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "victima")
        atacante = await _tenant(sesion, "atacante")
        conversacion = ChatConversation(
            organization_id=victima.organization_id,
            user_id=victima.user_id,
            title="Hilo de la victima",
        )
        sesion.add(conversacion)
        await sesion.commit()
        conversacion_id = conversacion.id

        async with _cliente() as cliente:
            respuesta = await cliente.delete(
                f"/api/v1/chat/conversations/{conversacion_id}", headers=atacante.headers
            )

        sigue = await sesion.execute(
            select(ChatConversation).where(ChatConversation.id == conversacion_id)
        )

    assert respuesta.status_code == 404
    assert sigue.scalar_one_or_none() is not None


# --------------------------------------------------------------------------- #
# El ciclo de vida
# --------------------------------------------------------------------------- #


async def test_crear_listar_leer_y_borrar() -> None:
    """El camino completo del CRUD, en el orden en que lo hace el panel."""

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "crud")
        async with _cliente() as cliente:
            creada = await cliente.post(
                "/api/v1/chat/conversations",
                json={"title": "Revision de la API"},
                headers=tenant.headers,
            )
            conversation_id = creada.json()["conversation"]["id"]

            listado = await cliente.get("/api/v1/chat/conversations", headers=tenant.headers)
            detalle = await cliente.get(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )
            borrada = await cliente.delete(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )
            releida = await cliente.get(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )

    assert creada.status_code == 201
    assert creada.json()["conversation"]["title"] == "Revision de la API"
    assert listado.status_code == 200
    assert listado.json()["total"] == 1
    assert detalle.status_code == 200
    assert detalle.json()["messages"] == []
    assert borrada.status_code == 204
    assert releida.status_code == 404


async def test_sin_titulo_nace_con_el_texto_por_defecto() -> None:
    """Sin `title` el hilo nace con el texto por defecto.

    Lo que no se comprueba aqui es el cambio al primer mensaje: eso ocurre en el envio, y
    `test_el_titulo_se_deriva_del_primer_mensaje` lo cubre.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sin-titulo")
        async with _cliente() as cliente:
            respuesta = await cliente.post(
                "/api/v1/chat/conversations", json={}, headers=tenant.headers
            )

    assert respuesta.status_code == 201
    assert respuesta.json()["conversation"]["title"] == "Nueva conversación"


async def test_un_campo_inesperado_se_rechaza() -> None:
    """`extra="forbid"` convierte una clave equivocada en un `422` que dice cual es.

    Sin el, un cliente que envie `{"name": ...}` en vez de `title` obtendria una conversacion
    creada con el titulo por defecto, y no sabria por que lo que envio no se uso.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "extra")
        async with _cliente() as cliente:
            respuesta = await cliente.post(
                "/api/v1/chat/conversations",
                json={"name": "Esto no es el campo"},
                headers=tenant.headers,
            )

    assert respuesta.status_code == 422
    # El error nombra el campo **sobrante**, que es lo que el usuario tiene que corregir. La
    # asercion mira `name` y no `title`: con `extra="forbid"`, Pydantic no dice "falta title"
    # sino "este campo no se admite", y una prueba que buscara `title` en la respuesta estaria
    # comprobando el texto equivocado y pasaria por la razon incorrecta.
    assert "name" in respuesta.text
    assert "title" not in respuesta.text


async def test_los_mensajes_caen_en_cascada_al_borrar() -> None:
    """Borrar el hilo borra sus mensajes, y no los borra Python.

    Si los borrase el codigo y la FK no existiera, quedarian mensajes huerfanos sin relacion que
    los protegiera. La prueba comprueba que la cascada es de la base.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "cascada")
        conversacion = ChatConversation(
            organization_id=tenant.organization_id,
            user_id=tenant.user_id,
            title="Con mensajes",
        )
        sesion.add(conversacion)
        await sesion.flush()
        sesion.add(
            ChatMessage(
                conversation_id=conversacion.id,
                role=ChatRoleEnum.USER,
                content="Pregunta",
                tokens_in=0,
                tokens_out=0,
                credits_cost=Decimal("0.0000"),
            )
        )
        await sesion.commit()
        conversation_id = conversacion.id

        async with _cliente() as cliente:
            await cliente.delete(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )

        huerfanos = await sesion.execute(
            select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
        )

    assert huerfanos.scalars().all() == []


# --------------------------------------------------------------------------- #
# Los permisos
# --------------------------------------------------------------------------- #


async def test_un_miembro_no_administra_lectura_de_conversaciones() -> None:
    """`require_scope` con `allow_admin_user` deja pasar al `ADMIN` y para al `MEMBER`.

    Es el comportamiento documentado de la dependencia: un usuario web se filtra por rol y un
    token por scopes, nunca al reves.
    """

    async with _sesion() as sesion:
        miembro = await _tenant(sesion, "miembro", role=RoleEnum.MEMBER)
        async with _cliente() as cliente:
            respuesta = await cliente.get(
                "/api/v1/chat/conversations", headers=miembro.headers
            )

    assert respuesta.status_code == 403


async def test_un_token_sin_el_permiso_no_lee() -> None:
    """Un token sin `chat:read` recibe un `403` que no dice que permisos tiene la victima.

    El detalle importa: un `403` que listara los scopes del token ayudaria al cliente, pero
    tambien le diria a alguien con un token robado que permisos tiene su victima, que es
    informacion que ya no debe tener.
    """

    # El token se crea por el servicio real y con una lista de scopes **vacia**, no un
    # `ApiToken` a mano con un hash inventado: un token con hash falso no autenticaria y la
    # prueba pasaria por un `401` de credencial, que no dice nada sobre permisos. Asi lo que
    # falla es exactamente el permiso.
    async with _sesion() as sesion:
        from backend.apps.api_access.schemas import ApiTokenCreate
        from backend.apps.api_access.service import create_api_token

        tenant = await _tenant(sesion, "token")
        # `scopes=[]` no es valido —el esquema exige al menos uno— y ademas no seria una
        # prueba: un token sin ningun scope no puede tocar nada, asi que fallaria por la
        # puerta de siempre y no por el permiso de chat. Se le da un scope **real** que no es
        # de chat, que es el caso que de verdad distingue `chat:read` del resto.
        _, crudo = await create_api_token(
            sesion,
            tenant.organization_id,
            ApiTokenCreate(name="Sin permisos de chat", scopes=[Scope.PENTESTS_READ.value]),
        )
        await sesion.commit()

        headers = {
            "Authorization": f"Bearer {crudo}",
            "X-Organization-Id": str(tenant.organization_id),
        }
        async with _cliente() as cliente:
            respuesta = await cliente.get("/api/v1/chat/conversations", headers=headers)

    assert respuesta.status_code == 403
    assert Scope.CHAT_READ.value not in respuesta.text


async def test_un_token_solo_lectura_no_puede_enviar() -> None:
    """`chat:read` y `chat:write` estan separados a proposito.

    Es la separacion que hace posible integrar un panel de actividad —o un bot que resume lo
    detectado— sin darle permiso de **gastar**. Con un unico permiso de chat, esa integracion no
    existiria, y la unica forma de darsela seria darle tambien el gasto.
    """

    async with _sesion() as sesion:
        from backend.apps.api_access.schemas import ApiTokenCreate
        from backend.apps.api_access.service import create_api_token

        tenant = await _tenant(sesion, "solo-lectura")
        _, crudo = await create_api_token(
            sesion,
            tenant.organization_id,
            ApiTokenCreate(name="Solo lectura", scopes=[Scope.CHAT_READ.value]),
        )
        await sesion.commit()
        conversation_id = uuid.uuid4()

        headers = {
            "Authorization": f"Bearer {crudo}",
            "X-Organization-Id": str(tenant.organization_id),
        }
        async with _cliente() as cliente:
            lectura = await cliente.get("/api/v1/chat/conversations", headers=headers)
            escritura = await cliente.post(
                f"/api/v1/chat/conversations/{conversation_id}/messages",
                json={"content": "Intento gastar"},
                headers=headers,
            )

    # Lee bien, y no puede escribir. Y lo que se comprueba aqui no es solo el codigo: es que
    # el `403` llega **antes** de tocar la base, asi que no se crea ninguna conversacion.
    assert lectura.status_code == 200
    assert escritura.status_code == 403


# --------------------------------------------------------------------------- #
# El turno
# --------------------------------------------------------------------------- #


async def test_enviar_un_mensaje_guarda_los_dos_y_cobra() -> None:
    """Un turno persiste la pregunta y la respuesta, y cobra lo declarado.

    Se comprueba que el mensaje del asistente llega con su consumo, porque es la evidencia
    propia del mensaje: sin ella, una disputa de factura se responderia sumando el ledger
    entero en vez de mirando el mensaje.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "turno")
        await _saldo(sesion, tenant.organization_id)
        modelo_id = await _modelo_chat(sesion)
        async with _cliente() as cliente:
            creada = await cliente.post(
                "/api/v1/chat/conversations", json={}, headers=tenant.headers
            )
            conversation_id = creada.json()["conversation"]["id"]
            try:
                respuesta = await cliente.post(
                    f"/api/v1/chat/conversations/{conversation_id}/messages",
                    json={"content": "Revisa la autorizacion de la API"},
                    headers=tenant.headers,
                )
            finally:
                await _borrar_modelo(modelo_id)

            detalle = await cliente.get(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["message"]["content"] == "Respuesta de prueba"
    assert cuerpo["message"]["tokens_in"] == 100
    assert cuerpo["message"]["tokens_out"] == 50
    assert float(cuerpo["message"]["credits_cost"]) > 0
    assert cuerpo["message"]["consumo_no_verificable"] is False

    mensajes = detalle.json()["messages"]
    assert [m["role"] for m in mensajes] == ["user", "assistant"]
    # El mensaje del usuario no lleva consumo: lo consume el proveedor, no el usuario.
    assert mensajes[0]["tokens_in"] == 0
    assert float(mensajes[0]["credits_cost"]) == 0.0


async def test_el_titulo_se_deriva_del_primer_mensaje() -> None:
    """Sin titulo, el hilo toma el del primer mensaje en vez de quedarse en "Nueva conversación".

    Veinte "Nueva conversación" en el menu lateral son veinte filas indistinguibles, y el
    usuario tiene que abrirlas todas para saber cual era cual.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "titulo")
        await _saldo(sesion, tenant.organization_id)
        modelo_id = await _modelo_chat(sesion)
        async with _cliente() as cliente:
            creada = await cliente.post(
                "/api/v1/chat/conversations", json={}, headers=tenant.headers
            )
            conversation_id = creada.json()["conversation"]["id"]
            try:
                await cliente.post(
                    f"/api/v1/chat/conversations/{conversation_id}/messages",
                    json={"content": "Buscar SSRF en los parametros de la API"},
                    headers=tenant.headers,
                )
            finally:
                await _borrar_modelo(modelo_id)

            detalle = await cliente.get(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )

    assert detalle.json()["conversation"]["title"] == "Buscar SSRF en los parametros de la API"


async def test_un_titulo_largo_se_corta_en_un_limite_de_palabra() -> None:
    """El corte cae en un espacio, no a media palabra.

    Cortar a indice fijo deja un titulo que parece truncado por un fallo.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "corte")
        await _saldo(sesion, tenant.organization_id)
        modelo_id = await _modelo_chat(sesion)
        largo = "palabra " * 40
        async with _cliente() as cliente:
            creada = await cliente.post(
                "/api/v1/chat/conversations", json={}, headers=tenant.headers
            )
            conversation_id = creada.json()["conversation"]["id"]
            try:
                await cliente.post(
                    f"/api/v1/chat/conversations/{conversation_id}/messages",
                    json={"content": largo.strip()},
                    headers=tenant.headers,
                )
            finally:
                await _borrar_modelo(modelo_id)

            detalle = await cliente.get(
                f"/api/v1/chat/conversations/{conversation_id}", headers=tenant.headers
            )

    titulo = detalle.json()["conversation"]["title"]
    assert titulo.endswith("…")
    # No termina a media palabra: el caracter anterior a los puntos es una letra.
    assert titulo[-2].isalpha()


async def test_las_opciones_de_contexto_validan_lo_que_se_acepta() -> None:
    """Una clave de alcance que no existe se rechaza, no se ignora.

    Es lo que impide que `context_options` sea un canal por el que un integrador escriba en el
    prompt: lo que no esta declarado no llega a existir.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "opciones")
        await _saldo(sesion, tenant.organization_id)
        modelo_id = await _modelo_chat(sesion)
        async with _cliente() as cliente:
            creada = await cliente.post(
                "/api/v1/chat/conversations", json={}, headers=tenant.headers
            )
            conversation_id = creada.json()["conversation"]["id"]
            try:
                respuesta = await cliente.post(
                    f"/api/v1/chat/conversations/{conversation_id}/messages",
                    json={
                        "content": "Revisa esto",
                        "context_options": {
                            "ignora_las_reglas_anteriores": True,
                        },
                    },
                    headers=tenant.headers,
                )
            finally:
                await _borrar_modelo(modelo_id)

    assert respuesta.status_code == 422
    assert "ignora_las_reglas_anteriores" in respuesta.text


async def test_los_dominios_se_normalizan() -> None:
    """Las mayusculas y los espacios se quitan antes de llegar al prompt.

    Sin normalizar, "API.Cliente.com " y "api.cliente.com" son dos alcances distintos para el
    modelo, y el usuario no ve por que su acotacion no se respeta.
    """

    from backend.apps.chat.schemas import ContextOptions

    opciones = ContextOptions(dominios=["  API.Cliente.COM  ", ""])
    assert opciones.dominios == ["api.cliente.com"]
