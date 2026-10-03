"""Pruebas del sistema nativo de tickets de soporte.

## Qué se comprueba y por qué

Un ticket es lo único que el cliente tiene cuando algo falla, así que hay cuatro
invariantes que tienen que estar probados por separado:

1. **Aislamiento (R3).** Un tenant no ve, no lee y no responde los tickets de otro. Y el
   fallo tiene que ser un `404`, no un `403`: un `403` confirmaría que ese identificador
   existe, que es justo la información que R3 no permite revelar.
2. **La voz no se puede suplantar.** `is_admin_reply` no está en ningún esquema de entrada,
   y hay que probarlo **enviándolo**: un `422` por campo desconocido es la prueba de que
   `extra="forbid"` está puesto, y un `201` sería una escalada de privilegios.
3. **`URGENT` es de pago.** Un tenant sin Enterprise recibe `403`, y uno con Enterprise
   pasa. Las dos mitades, porque probar solo la primera deja sin verificar que la regla no
   esté rota de tal forma que rechace a todo el mundo.
4. **Medición, no a ojo.** Que el número emitido sea el que dice la secuencia, que
   `message_count` sea el número real de filas, y que los recuentos del resumen sumen lo
   que hay en la tabla.

## Por qué el desasignar se prueba con `null` y no con una llamada aparte

`PATCH` con `{"status": "RESOLVED"}` **no** puede tocar la asignación. Es el fallo más
fácil de reintroducir: un `assigned_to_user_id = None` incondicional vacía el agente de
todos los tickets cada vez que se cambia el estado, y no se ve en ninguna prueba que solo
mire el estado.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.apps.support.models import (
    SupportCategoryEnum,
    SupportTicket,
    TicketMessage,
    TicketPriorityEnum,
    TicketStatusEnum,
)
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

#: Las rutas de la consola de soporte. Igual que en el resto de la consola: se prueban una
#: a una para que una que se quede sin cubrir falle sola.
ADMIN_TICKET_RUTAS = [
    ("get", "/api/v1/admin/tickets"),
    ("get", "/api/v1/admin/tickets/00000000-0000-0000-0000-000000000000"),
    ("patch", "/api/v1/admin/tickets/00000000-0000-0000-0000-000000000000"),
    ("post", "/api/v1/admin/tickets/00000000-0000-0000-0000-000000000000/messages"),
]


def _headers(user: User, organization: Organization | None = None) -> dict[str, str]:
    cabeceras = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    if organization is not None:
        cabeceras["X-Organization-Id"] = str(organization.id)
    return cabeceras


async def _tenant(
    session: AsyncSession,
    *,
    plan: PlanTierEnum = PlanTierEnum.FREE,
    is_superuser: bool = False,
) -> tuple[User, Organization, dict[str, str]]:
    """Un workspace con su admin y sus cabeceras de autenticación.

    `is_superuser` se puede pedir aquí porque el superusuario necesita poder abrir tickets
    urgentes **aunque** su workspace no tenga plan Enterprise: es el puente para probar la
    rama de éxito sin tener que sembrar un plan de pago en cada test.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Tickets {suffix}",
        slug=f"tickets-{suffix}",
        plan_tier=plan,
    )
    user = User(
        email=f"tickets-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Cliente de tickets",
        email_verified=True,
        is_superuser=is_superuser,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return user, organization, _headers(user, organization)


async def _abrir_ticket(
    session: AsyncSession, cabeceras: dict[str, str], **extra: object
) -> dict[str, object]:
    """Abre un ticket por HTTP y devuelve su cuerpo ya parseado.

    Se abre por HTTP y no llamando al servicio a propósito: lo que se prueba es la ruta
    completa, incluida la numeración por secuencia, que solo existe si pasa por la base.
    """

    cuerpo: dict[str, object] = {
        "subject": "El escaneo se queda parado en fase de analisis",
        "category": "TECHNICAL",
        "message": "El escaneo lleva veinte minutos en estado SCANNING y no avanza.",
    }
    cuerpo.update(extra)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post("/api/v1/support/tickets", json=cuerpo, headers=cabeceras)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


# --------------------------------------------------------------------------- #
# Apertura, numeración y forma del detalle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_abrir_ticket_devuelve_numero_de_secuencia(integration_session: AsyncSession) -> None:
    """El `ticket_number` lo emite la secuencia y no viene de la aplicación.

    Se comprueba contra la secuencia y **no** contra un número fijo. Un `1001` exacto
    dependería del estado previo de la base: la secuencia es global y no se reinicia entre
    tests, así que el valor crece. Comparar con `nextval` mide lo que importa —que el número
    sale de la secuencia y con el prefijo correcto— y sobrevive a la base sucia.
    """

    from sqlalchemy import text

    _user, _org, cabeceras = await _tenant(integration_session)
    cuerpo = await _abrir_ticket(integration_session, cabeceras)

    siguiente = (
        await integration_session.execute(
            text("SELECT nextval('support_ticket_number_seq')")
        )
    ).scalar_one()
    numero = str(cuerpo["ticket_number"])

    # El `nextval` de arriba ya consumió un número, así que el emitido es el inmediatamente
    # anterior al que la sonda acaba de pedir.
    assert numero == f"TK-{int(siguiente) - 1}"
    assert numero.startswith("TK-")


@pytest.mark.asyncio
async def test_dos_tickets_no_comparten_numero(integration_session: AsyncSession) -> None:
    """La secuencia es atómica: dos altas seguidas dan dos números distintos.

    Esto es lo que `MAX(id) + 1` no garantiza, y por eso la numeración vive en la base.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    primero = await _abrir_ticket(integration_session, cabeceras)
    segundo = await _abrir_ticket(integration_session, cabeceras)

    assert primero["ticket_number"] != segundo["ticket_number"]


@pytest.mark.asyncio
async def test_el_ticket_nace_con_su_primer_mensaje(integration_session: AsyncSession) -> None:
    """Un ticket no existe sin texto: el primer mensaje va en la misma petición.

    Se cuenta en la base, no en la respuesta. Que el detalle devuelva un mensaje solo prueba
    que el endpoint pinta lo que hay; lo que importa es que **no pueda haber un ticket con
    cero mensajes**, y eso solo se mide contando filas.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    cuerpo = await _abrir_ticket(integration_session, cabeceras)

    filas = (
        await integration_session.execute(
            select(func.count(TicketMessage.id)).where(
                TicketMessage.ticket_id == cuerpo["id"]
            )
        )
    ).scalar_one()
    assert filas == 1
    assert cuerpo["message_count"] == 1
    mensajes = cuerpo["messages"]
    assert isinstance(mensajes, list)
    assert mensajes[0]["is_admin_reply"] is False


@pytest.mark.asyncio
async def test_el_asunto_se_normaliza(integration_session: AsyncSession) -> None:
    """Los espacios sobrantes no se guardan: dos tickets idénticos se leen iguales.

    Sin esto, `"error   de  login"` y `"error de login"` son dos filas distintas y el
    buscador tiene que tolerar espacios que nadie escribió.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    cuerpo = await _abrir_ticket(integration_session, cabeceras, subject="  error   de  login  ")

    assert cuerpo["subject"] == "error de login"


@pytest.mark.asyncio
async def test_el_asunto_todo_espacios_es_rechazado(integration_session: AsyncSession) -> None:
    """Un asunto de tres espacios cumple `min_length=3` pero no es un asunto.

    El validador comprueba la longitud **después** de colapsar. Sin esa segunda
    comprobación, `"   "` pasaría el `min_length` de Pydantic y llegaría a la base como un
    ticket sin título visible en ninguna columna.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "     ",
                "category": "TECHNICAL",
                "message": "Mensaje de longitud suficiente para pasar la validacion.",
            },
            headers=cabeceras,
        )
    assert respuesta.status_code == 422, respuesta.text


# --------------------------------------------------------------------------- #
# Aislamiento (R3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_un_tenant_no_ve_los_tickets_de_otro(integration_session: AsyncSession) -> None:
    """El listado de un tenant sale solo del contexto, sin filtro del cliente.

    Se mide **contando filas en la base**, no mirando la respuesta. Un listado que por
    cualquier razón devolviera los del otro workspace lo delata un `total` mayor que uno;
    comprobarlo por la respuesta assumes que el endpoint cuenta bien.
    """

    _u1, org1, cab1 = await _tenant(integration_session)
    _u2, org2, cab2 = await _tenant(integration_session)
    await _abrir_ticket(integration_session, cab1)
    await _abrir_ticket(integration_session, cab2)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get("/api/v1/support/tickets", headers=cab1)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert {item["organization_id"] for item in cuerpo["items"]} == {str(org1.id)}
    assert str(org2.id) not in respuesta.text


@pytest.mark.asyncio
async def test_leer_el_ticket_de_otro_es_404_y_no_403(integration_session: AsyncSession) -> None:
    """El identificador ajeno da `404`, no `403`.

    Es la diferencia entre "no existe" y "existe pero no es tuyo". Un `403` confirmaría que
    ese UUID es real, que es una fuga de información entre tenants.
    """

    _u1, _org1, cab1 = await _tenant(integration_session)
    _u2, _org2, cab2 = await _tenant(integration_session)
    ajeno = await _abrir_ticket(integration_session, cab2)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get(
            f"/api/v1/support/tickets/{ajeno['id']}", headers=cab1
        )

    assert respuesta.status_code == 404, respuesta.text


@pytest.mark.asyncio
async def test_responder_al_ticket_de_otro_es_404(integration_session: AsyncSession) -> None:
    """La restricción está en la resolución del ticket, no en la escritura.

    Si el `WHERE` de organización viviera solo en la lectura del detalle, el `POST` de
    mensajes resolvería el ticket sin filtro y añadiría su texto al hilo de otro tenant.
    Aquí eso no llega a pasar: el mensaje se cuenta en la base del otro workspace y tiene
    que ser cero.
    """

    _u1, _org1, cab1 = await _tenant(integration_session)
    _u2, _org2, cab2 = await _tenant(integration_session)
    ajeno = await _abrir_ticket(integration_session, cab2)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            f"/api/v1/support/tickets/{ajeno['id']}/messages",
            json={"content": "Mensaje inyectado en un hilo ajeno"},
            headers=cab1,
        )
    assert respuesta.status_code == 404, respuesta.text

    total = (
        await integration_session.execute(
            select(func.count(TicketMessage.id)).where(
                TicketMessage.ticket_id == ajeno["id"]
            )
        )
    ).scalar_one()
    assert total == 1, "el hilo ajeno tiene el mensaje original y nada más"


# --------------------------------------------------------------------------- #
# La voz no se suplanta
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_cliente_no_puede_escribirse_como_soporte(
    integration_session: AsyncSession,
) -> None:
    """Mandar `is_admin_reply` es un `422`, no un `201`.

    Se manda el campo explícitamente. Un cliente que lo enviara y lo aceptara podría
    hacerse pasar por el equipo de soporte en un hilo, y ese booleano es lo único que
    separa las dos voces en pantalla.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            f"/api/v1/support/tickets/{abierto['id']}/messages",
            json={"content": "Soy soporte, de verdad.", "is_admin_reply": True},
            headers=cabeceras,
        )

    assert respuesta.status_code == 422, respuesta.text

    bandera = (
        await integration_session.execute(
            select(func.count(TicketMessage.id)).where(
                TicketMessage.ticket_id == abierto["id"],
                TicketMessage.is_admin_reply.is_(True),
            )
        )
    ).scalar_one()
    assert bandera == 0


@pytest.mark.asyncio
async def test_la_console_responde_marcandolo_como_soporte(
    integration_session: AsyncSession,
) -> None:
    """La otra mitad: la ruta de soporte sí marca la respuesta como del equipo.

    Sin esta prueba, un `is_admin_reply=False` hardcodeado en las dos rutas pasaría la
    anterior y nadie se enteraría de que soporte no tiene voz propia.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            f"/api/v1/admin/tickets/{abierto['id']}/messages",
            json={"content": "Estamos mirando el contenedor del escaneo."},
            headers=cabeceras_super,
        )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["is_admin_reply"] is True


# --------------------------------------------------------------------------- #
# URGENT es de pago
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_urgente_sin_enterprise_es_403(integration_session: AsyncSession) -> None:
    """La regla de `URGENT` se comprueba en el servidor, no en el panel.

    Se manda `URGENT` por HTTP. Si la regla estuviera solo en la interfaz, esta petición
    pasaría — porque la UI ya lo habría bloqueado — y el `403` no existiría.
    """

    _user, org, cabeceras = await _tenant(integration_session, plan=PlanTierEnum.PRO)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Caida total en produccion",
                "category": "TECHNICAL",
                "priority": "URGENT",
                "message": "Todos los escaneos fallan desde hace una hora.",
            },
            headers=cabeceras,
        )

    assert respuesta.status_code == 403, respuesta.text
    assert "Enterprise" in respuesta.json()["detail"]

    # El recuento va **acotado a este tenant**, y no sobre la tabla entera. Una asercion
    # `count(*) == 0` global mide el estado previo de la base compartida, no esta operacion:
    # fallo aqui porque una prueba anterior —un sondeo manual de la API— habia dejado un
    # ticket, y no porque el `403` hiciera otra cosa que rechazar. Es el mismo error que ya
    # aparecio dos veces en esta suite, y la tercera vez estaba en el fichero que mas
    # precisamente lo documenta.
    creados = (
        await integration_session.execute(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.organization_id == org.id
            )
        )
    ).scalar_one()
    assert creados == 0, "un 403 no puede dejar un ticket a medias"


@pytest.mark.asyncio
async def test_urgente_con_enterprise_pasa(integration_session: AsyncSession) -> None:
    """Con plan Enterprise el `URGENT` se acepta.

    La otra mitad de la regla. Probar solo el rechazo deja sin verificar que la condición no
    esté escrita al revés, y una condición invertida pasa la primera prueba sin fallar.
    """

    _user, _org, cabeceras = await _tenant(
        integration_session, plan=PlanTierEnum.ENTERPRISE
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Caida total en produccion",
                "category": "TECHNICAL",
                "priority": "URGENT",
                "message": "Todos los escaneos fallan desde hace una hora.",
            },
            headers=cabeceras,
        )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["priority"] == "URGENT"


@pytest.mark.asyncio
async def test_el_resumen_dice_si_se_puede_pedir_urgente(integration_session: AsyncSession) -> None:
    """El panel sabe la regla antes de que el cliente intente.

    Se prueban los dos planes a la vez porque el `can_request_urgent` es lo que decide si el
    selector muestra `URGENT` activo, y un `False` para Enterprise haría que el cliente de
    pago no pueda usar lo que pagó.
    """

    _u_free, _o_free, cab_free = await _tenant(integration_session, plan=PlanTierEnum.FREE)
    _u_ent, _o_ent, cab_ent = await _tenant(
        integration_session, plan=PlanTierEnum.ENTERPRISE
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        resumen_free = (await cliente.get("/api/v1/support/summary", headers=cab_free)).json()
        resumen_ent = (await cliente.get("/api/v1/support/summary", headers=cab_ent)).json()

    assert resumen_free["can_request_urgent"] is False
    assert resumen_ent["can_request_urgent"] is True
    assert resumen_free["plan_tier"] == "FREE"
    assert resumen_ent["plan_tier"] == "ENTERPRISE"


@pytest.mark.asyncio
async def test_el_superusuario_puede_urgente_sin_plan(integration_session: AsyncSession) -> None:
    """El superusuario puede abrir tickets urgentes aunque su workspace sea `FREE`.

    La razón es práctica: sin esa puerta, probar la rama de éxito de `URGENT` exigiría un
    tenant de pago en cada test, y la regla se quedaría sin verificar donde se escribe el
    código.
    """

    _user, _org, cabeceras = await _tenant(
        integration_session, plan=PlanTierEnum.FREE, is_superuser=True
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Prueba de la rama urgente",
                "category": "TECHNICAL",
                "priority": "URGENT",
                "message": "Necesito verificar que la puerta de urgencia se abre.",
            },
            headers=cabeceras,
        )

    assert respuesta.status_code == 201, respuesta.text


# --------------------------------------------------------------------------- #
# El hilo se cierra
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_responder_a_un_ticket_resuelto_es_409(integration_session: AsyncSession) -> None:
    """Un ticket resuelto no admite mensajes del cliente.

    Escribir ahí reabre una conversación que el soporte dio por buena sin que el agente se
    entere, y el cliente se queda esperando una respuesta que no va a llegar. Un `409` lo
    dice y además indica que la vía es abrir otro.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"status": "RESOLVED"},
            headers=cabeceras_super,
        )
        respuesta = await cliente.post(
            f"/api/v1/support/tickets/{abierto['id']}/messages",
            json={"content": "Sigue roto, abro esto otra vez por si acaso."},
            headers=cabeceras,
        )

    assert respuesta.status_code == 409, respuesta.text
    del org


@pytest.mark.asyncio
async def test_el_soporte_si_puede_responder_a_un_resuelto(
    integration_session: AsyncSession,
) -> None:
    """La simetría que hace que la regla tenga sentido.

    Si soporte tampoco pudiera, el `409` del cliente sería "este hilo está muerto" y no
    "este hilo está cerrado para ti". Con esta prueba se ve que el cierre es del lado del
    cliente y no del hilo.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"status": "CLOSED"},
            headers=cabeceras_super,
        )
        respuesta = await cliente.post(
            f"/api/v1/admin/tickets/{abierto['id']}/messages",
            json={"content": "Apunte interno: cerrado por duplicado, reabrir si vuelve."},
            headers=cabeceras_super,
        )

    assert respuesta.status_code == 201, respuesta.text


# --------------------------------------------------------------------------- #
# La asignación: la parte que se rompe en silencio
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cambiar_el_estado_no_desasigna(integration_session: AsyncSession) -> None:
    """Un `PATCH` de estado **no** toca la asignación aunque no la mencione.

    Este es el fallo que no se ve: si el servicio escribiera `assigned_to_user_id = None`
    siempre, cada cambio de estado devolvería el ticket a la cola y el agente se quedaría
    sin los suyos sin ningún aviso. Aquí el agente sobrevive a un cambio de estado.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        asignado = await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"assigned_to_user_id": str(_super.id)},
            headers=cabeceras_super,
        )
        assert asignado.status_code == 200, asignado.text
        assert asignado.json()["assigned_to_user_id"] == str(_super.id)

        cambiado = await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"status": "IN_PROGRESS"},
            headers=cabeceras_super,
        )

    assert cambiado.status_code == 200, cambiado.text
    assert cambiado.json()["assigned_to_user_id"] == str(_super.id)


@pytest.mark.asyncio
async def test_desasignar_exige_un_null_explicito(integration_session: AsyncSession) -> None:
    """Mandar `null` **sí** desasigna, y lo dice en la respuesta.

    La contraparte de la anterior. Si `null` no desasignara, devolver un ticket a la cola
    sería imposible y los tickets de un agente que deja la empresa se quedarían parados
    para siempre.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"assigned_to_user_id": str(_super.id)},
            headers=cabeceras_super,
        )
        liberado = await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"assigned_to_user_id": None},
            headers=cabeceras_super,
        )

    assert liberado.status_code == 200, liberado.text
    assert liberado.json()["assigned_to_user_id"] is None
    assert liberado.json()["assigned_to_email"] is None


@pytest.mark.asyncio
async def test_asignar_a_un_usuario_desactivado_es_422(integration_session: AsyncSession) -> None:
    """No se puede asignar trabajo a quien no puede entrar a recogerlo.

    Un `422` y no un `404`: lo que falla es que ese usuario no sea asignable, no que exista.
    Dejar el ticket en manos de un usuario desactivado lo manda a la cola sin que nadie lo
    note, y el cliente espera una respuesta que no llega.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)
    _inactivo, _org_inactiva, _cab_inactiva = await _tenant(integration_session)

    _inactivo.is_active = False
    await integration_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            f"/api/v1/admin/tickets/{abierto['id']}",
            json={"assigned_to_user_id": str(_inactivo.id)},
            headers=cabeceras_super,
        )

    assert respuesta.status_code == 422, respuesta.text


# --------------------------------------------------------------------------- #
# La consola de soporte
# --------------------------------------------------------------------------- #


async def _superuser(session: AsyncSession) -> tuple[User, dict[str, str]]:
    """Superusuario **sin** `X-Organization-Id`.

    A propósito. Las rutas de la consola cruzan tenants, y mandarle una cabecera de
    organización sugeriría que la petición está filtrada por algo cuando no lo está. La
    frontera real es `is_superuser`.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Super {suffix}", slug=f"super-{suffix}")
    user = User(
        email=f"super-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Super Admin",
        email_verified=True,
        is_superuser=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return user, _headers(user)


@pytest.mark.parametrize(("metodo", "ruta"), ADMIN_TICKET_RUTAS)
@pytest.mark.asyncio
async def test_un_usuario_normal_no_alcanza_la_consola_de_soporte(
    integration_session: AsyncSession, metodo: str, ruta: str
) -> None:
    """Cada ruta de la consola de soporte exige `is_superuser`, sin excepción.

    Se prueban todas por separado y no confiando en que el router padre lo imponga: una
    ruta nueva que olvide la dependencia quedaría accesible a cualquier usuario con sesión,
    y esto es lo que lo detecta.
    """
    _user, _org, cabeceras = await _tenant(integration_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.request(metodo, ruta, headers=cabeceras, json={})

    assert respuesta.status_code == 403, respuesta.text


@pytest.mark.asyncio
async def test_la_consola_ve_los_tickets_de_todos_los_tenants(
    integration_session: AsyncSession,
) -> None:
    """La consola cruza tenants: es el motivo de que exista.

    La contraparte de la prueba de aislamiento. Si el listado global filtrara por el
    `X-Organization-Id` ausente o por una organización inventada, devolvería cero filas y la
    consola sería inútil sin que ninguna otra prueba se enterara.
    """

    _u1, org1, cab1 = await _tenant(integration_session)
    _u2, org2, cab2 = await _tenant(integration_session)
    await _abrir_ticket(integration_session, cab1)
    await _abrir_ticket(integration_session, cab2)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get("/api/v1/admin/tickets", headers=cabeceras_super)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] >= 2
    assert {str(org1.id), str(org2.id)}.issubset(
        {item["organization_id"] for item in cuerpo["items"]}
    )


@pytest.mark.asyncio
async def test_el_buscador_encuentra_por_numero_de_ticket(
    integration_session: AsyncSession,
) -> None:
    """El agente busca por `#TK-1001`, que es lo que el cliente dice por teléfono.

    Se busca con el **número completo**, con la almohadilla incluida, porque es así
    literalmente como aparece en pantalla. Si la búsqueda no tolerara el `#`, el agente
    tendría que saber que el prefijo es `TK-` sin almohadilla, y eso es conocimiento que no
    está en ningún sitio.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)
    _super, cabeceras_super = await _superuser(integration_session)
    numero = str(abierto["ticket_number"])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get(
            "/api/v1/admin/tickets", params={"search": f"#{numero}"}, headers=cabeceras_super
        )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["total"] == 1
    assert respuesta.json()["items"][0]["id"] == abierto["id"]


@pytest.mark.asyncio
async def test_la_consola_ordena_por_actualizacion_y_no_por_prioridad(
    integration_session: AsyncSession,
) -> None:
    """Lo primero que se ve es lo último que pasó, no lo más urgente de siempre.

    Un ticket `URGENT` de hace un mes no es más urgente que uno `LOW` de hace una hora.
    Ordenar por prioridad escondería los recientes detrás de los que llevan semanas sin
    tocarse, que es exactamente lo contrario de lo que necesita la cola.
    """

    # Los tickets se abren por la **ruta de cliente**, con la cabecera del tenant. La ruta de
    # administración no abre tickets, y abrir por la del superusuario sin
    # `X-Organization-Id` daría `403`: la ruta de cliente exige tenant incluso para un
    # superusuario, porque sin él no hay `organization_id` al que colgar el ticket.
    #
    # El tenant es Enterprise porque uno de los dos tickets es `URGENT`, y esa prioridad está
    # reservada a ese plan. No es un detalle del test: es la razón por la que hace falta un
    # plan de pago para tener un ticket urgente en la cola.
    _user, _org, cabeceras = await _tenant(
        integration_session, plan=PlanTierEnum.ENTERPRISE
    )
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        urgente = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Urgente viejo",
                "category": "TECHNICAL",
                "priority": "URGENT",
                "message": "Peticion urgente de hace tiempo que sigue abierta.",
            },
            headers=cabeceras,
        )
        assert urgente.status_code == 201, urgente.text
        normal = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Normal nuevo",
                "category": "TECHNICAL",
                "priority": "NORMAL",
                "message": "Peticion normal de ahora mismo que se acaba de abrir.",
            },
            headers=cabeceras,
        )
        assert normal.status_code == 201, normal.text
        # El segundo mensaje es lo que cambia `updated_at`. Sin él, los dos se abrieron en el
        # mismo lote y el orden sería el del `id`, que no dice nada de la urgencia real.
        await cliente.post(
            f"/api/v1/support/tickets/{urgente.json()['id']}/messages",
            json={"content": "Anotado, seguimos mirando."},
            headers=cabeceras,
        )
        listing = await cliente.get("/api/v1/admin/tickets", headers=cabeceras_super)

    posiciones = {
        item["id"]: indice for indice, item in enumerate(listing.json()["items"])
    }
    assert posiciones[urgente.json()["id"]] < posiciones[normal.json()["id"]]


# --------------------------------------------------------------------------- #
# Medición: el resumen cuadra con la tabla
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_resumen_cuadra_con_lo_que_hay_en_la_tabla(
    integration_session: AsyncSession,
) -> None:
    """Los recuentos del resumen se comparan **por delta** contra lo que se creó aquí.

    Se mide la diferencia entre antes y después, nunca un absoluto. La base de pruebas es
    compartida y otros tests dejan tickets de su propio tenant; un `== 3` sobre el total
    global pasa o falla según el orden de ejecución, que es el modo de fallo que ya ha
    aparecido más de una vez en esta suite.

    Se limpian los tickets creados al final. No es higiene, es lo que hace que el delta sea
    interpretable: si este test dejara tres tickets detrás, el `antes` del siguiente test
    que se mida sobre el mismo tenant empezaría en un número distinto.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    _super, cabeceras_super = await _superuser(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        antes = (await cliente.get("/api/v1/support/summary", headers=cabeceras)).json()

        await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Primer ticket abierto",
                "category": "TECHNICAL",
                "message": "Este es el primero de los tres que se abren aqui.",
            },
            headers=cabeceras,
        )
        segundo = await cliente.post(
            "/api/v1/support/tickets",
            json={
                "subject": "Segundo ticket abierto",
                "category": "BILLING",
                "message": "Este es el segundo de los tres que se abren aqui.",
            },
            headers=cabeceras,
        )
        await cliente.patch(
            f"/api/v1/admin/tickets/{segundo.json()['id']}",
            json={"status": "IN_PROGRESS"},
            headers=cabeceras_super,
        )
        await cliente.patch(
            f"/api/v1/admin/tickets/{segundo.json()['id']}",
            json={"status": "RESOLVED"},
            headers=cabeceras_super,
        )
        despues = (await cliente.get("/api/v1/support/summary", headers=cabeceras)).json()

        # Recuento independiente de este tenant, hecho a mano con un `GROUP BY` sobre el
        # estado. Si el resumen mentiría, esta consulta lo diría.
        conteo = {
            estado: int(cuenta)
            for estado, cuenta in (
                await integration_session.execute(
                    select(SupportTicket.status, func.count(SupportTicket.id))
                    .where(SupportTicket.organization_id == org.id)
                    .group_by(SupportTicket.status)
                )
            ).all()
        }

        await integration_session.execute(
            delete(SupportTicket).where(SupportTicket.organization_id == org.id)
        )
        await integration_session.commit()

    # Dos tickets abiertos, uno se resuelve: una tarjeta `open_count` sube en 1,
    # `resolved_count` sube en 1 y `waiting_count` no se mueve porque el segundo pasó por
    # `IN_PROGRESS` y ya no está allí. Y el resumen tiene que **medir lo que hay**, no
    # contar lo que hizo este test.
    assert despues["open_count"] == antes["open_count"] + 1
    assert despues["resolved_count"] == antes["resolved_count"] + 1
    assert despues["waiting_count"] == antes["waiting_count"]
    assert conteo.get(TicketStatusEnum.OPEN, 0) == 1
    assert conteo.get(TicketStatusEnum.RESOLVED, 0) == 1
    assert conteo.get(TicketStatusEnum.IN_PROGRESS, 0) == 0


# --------------------------------------------------------------------------- #
# El borrado del workspace no se lleva los tickets
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_borrar_el_workspace_falla_por_restrict(
    integration_session: AsyncSession,
) -> None:
    """Un ticket impide borrar su workspace, y eso es deliberado.

    Con `CASCADE` se perdería la conversación entera —la prueba de qué se le dijo a ese
    cliente— al dar de baja el workspace. `RESTRICT` obliga a usar el borrado lógico, que
    es la vía correcta.

    Se mide que el `DELETE` revienta **y que el ticket sigue ahí**: un `RESTRICT` que
    borrara el ticket y luego fallara también sería peor que `CASCADE`.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    abierto = await _abrir_ticket(integration_session, cabeceras)

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await integration_session.execute(
            delete(Organization).where(Organization.id == org.id)
        )
        await integration_session.commit()
    await integration_session.rollback()

    sigue = (
        await integration_session.execute(
            select(func.count(SupportTicket.id)).where(SupportTicket.id == abierto["id"])
        )
    ).scalar_one()
    assert sigue == 1

    # El ticket bloqueó el borrado, y la sesión sigue siendo utilizable después del
    # `rollback`. Una sesión envenenada aquí significaría que el `RESTRICT` se ha comido la
    # conexión.
    total = (
        await integration_session.execute(select(func.count(SupportTicket.id)))
    ).scalar_one()
    assert total >= 1


@pytest.mark.asyncio
async def test_categorias_y_prioridades_validas(integration_session: AsyncSession) -> None:
    """Las cuatro categorías y las tres prioridades del enum se aceptan.

    Un enum mal declarado deja fuera una categoría que la interfaz ofrece, y el `422` sale
    en producción. Se prueban todas porque el coste es un bucle y el olvido es real.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        for categoria in SupportCategoryEnum:
            respuesta = await cliente.post(
                "/api/v1/support/tickets",
                json={
                    "subject": f"Prueba de la categoria {categoria.value}",
                    "category": categoria.value,
                    "message": "Mensaje de longitud suficiente para la categoria probada.",
                },
                headers=cabeceras,
            )
            assert respuesta.status_code == 201, f"{categoria.value}: {respuesta.text}"
        for prioridad in (TicketPriorityEnum.LOW, TicketPriorityEnum.NORMAL):
            respuesta = await cliente.post(
                "/api/v1/support/tickets",
                json={
                    "subject": f"Prueba de la prioridad {prioridad.value}",
                    "category": "FEATURE_REQUEST",
                    "priority": prioridad.value,
                    "message": "Mensaje de longitud suficiente para la prioridad probada.",
                },
                headers=cabeceras,
            )
            assert respuesta.status_code == 201, f"{prioridad.value}: {respuesta.text}"


# --------------------------------------------------------------------------- #
# Filtros de la vista de cliente: texto, estado y rango de alta
# --------------------------------------------------------------------------- #
#
# ## Por qué estas pruebas miden **todas** las filas y no solo `items[0]`
#
# Porque un filtro mal escrito no falla de forma visible: devuelve la tabla entera, o se
# lleva por delante filas que no tocan. Un `assert` sobre la primera fila las dejaría pasar.


async def _abrir_ticket_como_otro_miembro(
    session: AsyncSession,
    cabeceras: dict[str, str],
    organization: Organization,
    *,
    subject: str,
) -> dict[str, object]:
    """Abre un ticket con **otro** usuario del mismo workspace.

    Hace falta para probar la búsqueda por autor: si todos los tickets de la prueba los abre la
    misma persona —que es lo que pasa por defecto—, su correo coincide en todos y buscarlo no
    distingue nada de no filtrar.
    """

    suffix = uuid.uuid4().hex
    otro = User(
        email=f"colega-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Colega del workspace",
        email_verified=True,
    )
    session.add(otro)
    await session.flush()
    session.add(
        Membership(
            organization_id=organization.id, user_id=otro.id, role=RoleEnum.MEMBER
        )
    )
    await session.commit()
    return await _abrir_ticket(session, _headers(otro, organization), subject=subject)


async def _abrir_con_fecha(
    session: AsyncSession,
    cabeceras: dict[str, str],
    *,
    subject: str,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Abre un ticket y le fija la fecha de alta si se le pasa.

    `created_at` se puede escribir a mano porque la columna solo declara `server_default`: el
    valor explícito se acepta, y es lo único que permite medir el rango sin depender del reloj.
    """

    cuerpo = await _abrir_ticket(session, cabeceras, subject=subject)
    if created_at is not None:
        ticket = await session.get(SupportTicket, cuerpo["id"])
        assert ticket is not None
        ticket.created_at = created_at
        ticket.updated_at = created_at
        await session.commit()
    return cuerpo


async def _listar(cabeceras: dict[str, str], parametros: str) -> Response:
    """La **respuesta**, no el cuerpo ya parseado.

    Se devuelve el `Response` y el cuerpo se lee con `.json()` en cada aserción por la misma
    razón que en `test_pr_reviews_api.py`: `json()` devuelve `Any`, y con un `dict[str, object]`
    el tipado estricto se queja de cada `cuerpo["items"][0]` —que es ruido en una prueba— mientras
    que con `Any` el fallo real, que es una clave que no existe, sale en el `KeyError`.
    """

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get(
            f"/api/v1/support/tickets{parametros}", headers=cabeceras
        )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta


@pytest.mark.asyncio
async def test_el_buscador_de_tickets_encuentra_por_asunto_numero_y_autor(
    integration_session: AsyncSession,
) -> None:
    """Las tres cosas con las que se recuerda un ticket, y no una sola.

    Con una sola columna, dos de cada tres búsquedas darían página vacía y el buscador
    parecería roto. El número se busca **como se ve en pantalla**, con la almohadilla, que es
    lo natural: la columna guarda `TK-1005` y la pinta como `#TK-1005`.
    """

    _user, org, cabeceras = await _tenant(integration_session)
    por_asunto = await _abrir_ticket(
        integration_session,
        cabeceras,
        subject="El escaneo se queda parado en fase de analisis",
    )
    por_numero = await _abrir_ticket(
        integration_session,
        cabeceras,
        subject="Asunto que no tiene nada que ver con la busqueda",
    )
    por_autor = await _abrir_ticket_como_otro_miembro(
        integration_session,
        cabeceras,
        org,
        subject="Otro asunto que tampoco aparece en la busqueda",
    )
    correo_autor = str(por_autor["created_by_email"])

    encontrado_asunto = (await _listar(cabeceras, "?query=fase de analisis")).json()
    encontrado_numero = (
        await _listar(cabeceras, f"?query=%23{por_numero['ticket_number']}")
    ).json()
    encontrado_autor = (await _listar(cabeceras, f"?query={correo_autor}")).json()
    sin_coincidencia = (await _listar(cabeceras, "?query=esta-frase-no-existe")).json()

    assert encontrado_asunto["total"] == 1
    assert encontrado_asunto["items"][0]["id"] == por_asunto["id"]
    assert encontrado_numero["total"] == 1
    assert encontrado_numero["items"][0]["id"] == por_numero["id"]
    # El correo del colega solo aparece en **su** ticket, así que un filtro que no buscara por
    # autor devolvería los tres y la prueba lo vería.
    assert encontrado_autor["total"] == 1
    assert encontrado_autor["items"][0]["id"] == por_autor["id"]
    assert sin_coincidencia["total"] == 0
    assert sin_coincidencia["items"] == []


@pytest.mark.asyncio
async def test_el_buscador_de_tickets_trata_los_comodines_como_literales(
    integration_session: AsyncSession,
) -> None:
    """`%` y `_` se buscan literales.

    Sin escapar, `?query=%` devuelve **la tabla entera**: el comodín va en los dos extremos, así
    que el patrón `%\\%%` casa con cualquier valor. Es el peor tipo de fallo de un filtro, porque
    la pantalla «funciona» —sale una tabla, solo que entera— y no hay nada que reportar.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    await _abrir_ticket(
        integration_session, cabeceras, subject="Descuento del 100%off en el plan anual"
    )
    await _abrir_ticket(
        integration_session, cabeceras, subject="Descuento del 1000off en el plan anual"
    )
    await _abrir_ticket(
        integration_session, cabeceras, subject="Ticket con web_app en el asunto"
    )
    await _abrir_ticket(
        integration_session, cabeceras, subject="Ticket con webXapp en el asunto"
    )

    con_porcentaje = (await _listar(cabeceras, "?query=100%25off")).json()
    con_guion_bajo = (await _listar(cabeceras, "?query=web_app")).json()
    solo_porcentaje = (await _listar(cabeceras, "?query=%25")).json()

    # Solo el que lleva el símbolo literal. El otro tiene `1000off`, que `%` también casaría.
    assert con_porcentaje["total"] == 1
    assert "100%off" in con_porcentaje["items"][0]["subject"]
    assert con_guion_bajo["total"] == 1
    assert "web_app" in con_guion_bajo["items"][0]["subject"]
    # `%` a secas devuelve **una** fila —la que de verdad lleva el símbolo en el asunto— y no
    # las cuatro. Sin escapar serían las cuatro, que es exactamente el fallo que se comprueba.
    assert solo_porcentaje["total"] == 1
    assert "100%off" in solo_porcentaje["items"][0]["subject"]


@pytest.mark.asyncio
async def test_el_buscador_de_tickets_no_devuelve_la_tabla_por_un_termino_vacio(
    integration_session: AsyncSession,
) -> None:
    """Un término vacío es un filtro vacío, no «casa con todo».

    La diferencia entre las dos cosas es la diferencia entre una lista vacía y una pantalla
    llena de tickets que el usuario no ha pedido. Y el botón de limpiar compara contra este
    mismo criterio: si el filtro vacío se aplicara, el botón no aparecería nunca.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    await _abrir_ticket(integration_session, cabeceras, subject="Un ticket de verdad")

    solo_espacios = (await _listar(cabeceras, "?query=%20%20%20")).json()
    solo_almohadilla = (await _listar(cabeceras, "?query=%23")).json()

    assert solo_espacios["total"] == 1
    assert solo_espacios["items"][0]["subject"] == "Un ticket de verdad"
    assert solo_almohadilla["total"] == 1


@pytest.mark.asyncio
async def test_el_buscador_de_tickets_no_busca_por_el_nombre_del_tenant(
    integration_session: AsyncSession,
) -> None:
    """En la vista de cliente el nombre del workspace no es una columna donde buscar.

    Es siempre el mismo, así que incluirlo convertiría cualquier término que saliera en él —y el
    nombre de una empresa suele ser una palabra suelta— en «devuélveme todos». En la consola sí
    se busca, y esa mitad también se comprueba: si el filtro no se distinguiera, la consola
    habría perdido la búsqueda por tenant y el fallo sería igual de silencioso.
    """

    _user, org, cabeceras = await _tenant(integration_session, is_superuser=True)
    await _abrir_ticket(integration_session, cabeceras, subject="Un ticket de verdad")
    # El nombre del workspace se pone a una palabra reconocible para poder buscarlo.
    org.name = "Zetaunico"
    await integration_session.commit()

    por_nombre_del_cliente = (await _listar(cabeceras, "?query=zetaunico")).json()

    assert por_nombre_del_cliente["total"] == 0
    assert por_nombre_del_cliente["items"] == []

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        por_nombre_de_consola = await cliente.get(
            "/api/v1/admin/tickets?search=zetaunico",
            headers={"Authorization": cabeceras["Authorization"]},
        )

    assert por_nombre_de_consola.status_code == 200, por_nombre_de_consola.text
    assert por_nombre_de_consola.json()["total"] == 1


@pytest.mark.asyncio
async def test_el_rango_de_tickets_usa_la_fecha_de_alta_y_el_ultimo_dia_entra_entero(
    integration_session: AsyncSession,
) -> None:
    """Los cuatro bordes del rango, con el día final **completo**.

    Se comprueban los cuatro: la medianoche previa se queda fuera, la inicial entra, el último
    segundo del día final entra y la medianoche siguiente se queda fuera. Con un `<=` sobre la
    medianoche del `created_to` dejaría pasar solo el último.

    Y va sobre `created_at`, no sobre `updated_at` —que es lo que ordena la tabla y se mueve con
    cada mensaje—, porque un rango sobre una columna que cambia daría un resultado distinto a
    los diez minutos.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    ayer = await _abrir_con_fecha(
        integration_session,
        cabeceras,
        subject="Ticket del dia anterior",
        created_at=datetime(2026, 3, 9, 23, 59, tzinfo=UTC),
    )
    primer_dia = await _abrir_con_fecha(
        integration_session,
        cabeceras,
        subject="Ticket del primer dia del rango",
        created_at=datetime(2026, 3, 10, 0, 0, tzinfo=UTC),
    )
    ultimo_dia = await _abrir_con_fecha(
        integration_session,
        cabeceras,
        subject="Ticket del ultimo dia del rango",
        created_at=datetime(2026, 3, 12, 23, 59, 59, tzinfo=UTC),
    )
    dia_siguiente = await _abrir_con_fecha(
        integration_session,
        cabeceras,
        subject="Ticket del dia siguiente",
        created_at=datetime(2026, 3, 13, 0, 0, tzinfo=UTC),
    )

    rango = (await _listar(cabeceras, "?created_from=2026-03-10&created_to=2026-03-12")).json()
    solo_desde = (await _listar(cabeceras, "?created_from=2026-03-12")).json()
    invertido = (await _listar(cabeceras, "?created_from=2026-03-12&created_to=2026-03-10")).json()

    assert rango["total"] == 2
    assert {item["id"] for item in rango["items"]} == {primer_dia["id"], ultimo_dia["id"]}
    assert solo_desde["total"] == 2
    # Un rango invertido no es un `422`: las dos condiciones son incompatibles por
    # construcción, así que la lista vacía ya es la respuesta que corresponde.
    assert invertido["total"] == 0
    assert invertido["items"] == []
    # Los dos extremos, explícitamente, para que un recorte de un día no pase por bueno.
    assert ayer["id"] not in {item["id"] for item in rango["items"]}
    assert dia_siguiente["id"] not in {item["id"] for item in rango["items"]}


@pytest.mark.asyncio
async def test_los_filtros_de_tickets_se_quedan_dentro_del_tenant(
    integration_session: AsyncSession,
) -> None:
    """R3 también con los filtros nuevos: el buscador no amplía la vista.

    Un buscador es la forma más fácil de meter en la consulta una condición que el llamador
    elige, así que la prueba mira lo que **no** devuelve tanto como lo que sí.
    """

    _u1, _org1, cab1 = await _tenant(integration_session)
    _u2, _org2, cab2 = await _tenant(integration_session)
    await _abrir_ticket(integration_session, cab1, subject="Titulo del tenant alfa")
    await _abrir_ticket(integration_session, cab2, subject="Titulo del tenant beta")

    ajeno = (await _listar(cab1, "?query=beta")).json()
    propio = (await _listar(cab1, "?query=alfa")).json()

    assert ajeno["total"] == 0
    assert ajeno["items"] == []
    assert propio["total"] == 1


@pytest.mark.asyncio
async def test_los_filtros_de_tickets_cuentan_antes_de_paginar(
    integration_session: AsyncSession,
) -> None:
    """`total` cuenta lo que coincide con los filtros, no lo que hay en el workspace.

    Es lo que hace honesta la barra de paginación: si `total` saliera sin filtrar, el resumen
    «1-25 de 300» prometería páginas que al pulsarlas saldrían vacías.
    """

    _user, _org, cabeceras = await _tenant(integration_session)
    for indice in range(3):
        await _abrir_ticket(
            integration_session, cabeceras, subject=f"Ticket que coincide {indice}"
        )
    await _abrir_ticket(
        integration_session, cabeceras, subject="Ticket ajeno a la busqueda de esta prueba"
    )

    primera = (await _listar(cabeceras, "?query=coincide&limit=2&offset=0")).json()
    segunda = (await _listar(cabeceras, "?query=coincide&limit=2&offset=2")).json()

    assert primera["total"] == 3
    assert len(primera["items"]) == 2
    assert primera["limit"] == 2
    assert primera["offset"] == 0
    assert segunda["offset"] == 2
    assert len(segunda["items"]) == 1
    assert all(
        "coincide" in item["subject"]
        for item in list(primera["items"]) + list(segunda["items"])
    )


@pytest.mark.asyncio
async def test_el_filtro_por_estado_de_ticket_recorta_dentro_del_mismo_texto(
    integration_session: AsyncSession,
) -> None:
    """El estado **recorta** entre tickets que el buscador devuelve a los dos.

    Es donde un filtro que se ignorara pasaría más desapercibido: si `status` no se aplicara,
    las dos consultas devolverían la misma lista y la pantalla no avisaría de nada. Por eso los
    tres tickets coinciden en el texto y en el rango, y lo único que cambia es el estado.
    """

    _user, _org, cabeceras = await _tenant(integration_session, is_superuser=True)
    for indice in range(2):
        await _abrir_ticket(
            integration_session,
            cabeceras,
            subject=f"Fallo de replicacion numero {indice}",
        )
    resuelto = await _abrir_ticket(
        integration_session, cabeceras, subject="Fallo de replicacion ya resuelto"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        cambio = await cliente.patch(
            f"/api/v1/admin/tickets/{resuelto['id']}",
            json={"status": "RESOLVED"},
            headers={"Authorization": cabeceras["Authorization"]},
        )
    assert cambio.status_code == 200, cambio.text

    sin_filtro = (await _listar(cabeceras, "?query=replicacion")).json()
    abiertos = (await _listar(cabeceras, "?query=replicacion&status=OPEN")).json()
    resueltos = (await _listar(cabeceras, "?query=replicacion&status=RESOLVED")).json()

    assert sin_filtro["total"] == 3
    assert abiertos["total"] == 2
    assert resueltos["total"] == 1
    assert resueltos["items"][0]["id"] == resuelto["id"]
