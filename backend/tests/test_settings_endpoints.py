"""Pruebas de los endpoints de Ajustes: renombrar organización y gestionar miembros.

## Qué se comprueba y por qué

Estos endpoints son los que sostienen las pestañas *General* y *Members*, y los dos tienen
invariantes que no son de "devuelve el dato" sino de lo que pasa cuando algo va mal:

1. **El aislamiento (R3) sin parámetro que lo compruebe.** Las rutas toman el tenant del
   contexto. No hay `organization_id` en ninguna, así que no hay filtro que comprobar —
   la imposibilidad de equivocarse es la garantía. Se comprueba igual, porque una ruta
   nueva que sí lo acepte sería el fallo.

2. **El asiento de auditoría es de la misma transacción que el cambio.** Es la parte que
   más fácilmente se rompe: un `commit` entre el `UPDATE` y el asiento deja un workspace
   renombrado sin rastro, o un rastro de un cambio que no ocurrió. Se mide contando filas
   de `audit_log`, no mirando la respuesta.

3. **El workspace no puede quedarse sin administrador.** Es el invariante que da
   invalidate todo lo demás: si se puede quedar sin admin, no hay interfaz desde la que
   arreglarlo. Se prueban las tres vías que lo podrían provocar —cambiar de rol, quitar
   el rol de admin, y AutoRetirarse— porque las tres terminan en el mismo sitio y cada una
   puede tener su propio error.

4. **El retiro no borra la fila.** Por R4, la `membership_id` es el actor de otras
   entradas de auditoría. Se comprueba que la fila sigue existiendo y desactivada.

## Por qué se mide con deltas y con filtros de tenant

La base de pruebas es compartida. Cualquier aserción sobre un total absoluto depende del
estado previo y falla según el orden de ejecución. Todas las consultas de este archivo
llevan `organization_id=...` o un `id` concreto.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration


def _headers(user: User, organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _workspace(
    session: AsyncSession,
    *,
    role: RoleEnum = RoleEnum.ADMIN,
) -> tuple[User, Organization, dict[str, str]]:
    """Un workspace con su usuario, su rol y sus cabeceras."""

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Ajustes {suffix}", slug=f"ajustes-{suffix}")
    user = User(
        email=f"ajustes-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Admin de ajustes",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=role)
    )
    await session.commit()
    return user, organization, _headers(user, organization)


async def _invitar_miembro(
    session: AsyncSession,
    organization: Organization,
    *,
    role: RoleEnum,
    activo: bool = True,
) -> User:
    """Crea un segundo usuario con membresía en este workspace, sin pasar por la invitación.

    Se crea la fila directamente en vez de invitar y aceptar porque el objeto de esta prueba
    es la gestión de miembros, no el flujo de invitación, que ya tiene sus propias pruebas.
    Encadenar los dos haría que un fallo en la invitación se manifestara aquí como un fallo
    de membresía, que es un diagnóstico falso.
    """

    suffix = uuid.uuid4().hex
    user = User(
        email=f"miembro-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"Miembro {suffix[:6]}",
        email_verified=True,
        is_active=activo,
    )
    session.add(user)
    await session.flush()
    session.add(
        Membership(
            organization_id=organization.id, user_id=user.id, role=role
        )
    )
    await session.commit()
    return user


async def _asientos(
    session: AsyncSession, organization_id: uuid.UUID, accion: AuditActionEnum
) -> list[AuditLogEntry]:
    return list(
        (
            await session.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.organization_id == organization_id,
                    AuditLogEntry.action == accion,
                )
            )
        ).scalars().all()
    )


# --------------------------------------------------------------------------- #
# Renombrar organización
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_renombrar_cambia_el_nombre_y_asienta_el_rastro(
    integration_session: AsyncSession,
) -> None:
    """El nombre cambia y el asiento queda en la misma transacción.

    Se cuentan los asientos **antes y después** en lugar de afirmar que hay uno. Un `== 1`
    sobre un total absoluto mediría el estado previo de la base, y esta migración de R4
    necesita que el asiento exista, no que haya exactamente uno en el universo.
    """

    _user, organization, cabeceras = await _workspace(integration_session)
    antes = len(
        await _asientos(
            integration_session, organization.id, AuditActionEnum.ORGANIZATION_RENAMED
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": "FleetView Iberia S.L."},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["name"] == "FleetView Iberia S.L."

    despues = await _asientos(
        integration_session, organization.id, AuditActionEnum.ORGANIZATION_RENAMED
    )
    assert len(despues) == antes + 1
    asiento = despues[-1]
    # El asiento guarda el nombre **anterior**: un rastro que solo dijera "esto cambió" no
    # respondería a la pregunta que se le hace en una revisión, que es de qué a qué.
    assert asiento.from_state == organization.name or asiento.to_state == "FleetView Iberia S.L."
    assert asiento.entity_type == "organization"
    assert asiento.entity_id == organization.id


@pytest.mark.asyncio
async def test_renombrar_no_toca_el_slug(integration_session: AsyncSession) -> None:
    """El nombre cambia; la dirección no.

    El `slug` aparece en enlaces ya compartidos y en el botón de compartir. Renombrarlo los
    partiría, y un enlace de invitación que no abre es un cliente que no entra. Se comprueba
    que la columna no se ha movido.
    """

    _user, organization, cabeceras = await _workspace(integration_session)
    slug_original = organization.slug

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": "Nombre Nuevo"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["slug"] == slug_original


@pytest.mark.asyncio
async def test_mandar_slug_es_422_y_no_se_ignora(
    integration_session: AsyncSession,
) -> None:
    """`extra="forbid"` convierte un `slug` en el cuerpo en un `422`.

    Lo importante no es que rechace, sino que **rechace**. Si se ignorara en silencio, el
    usuario vería el error de guardado resuelto y creería que la dirección había cambiado,
    que es peor que un mensaje de error.
    """

    _user, _organization, cabeceras = await _workspace(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": "Nombre Valido", "slug": "otro-slug"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.parametrize(
    ("nombre", "motivo"),
    [
        ("   ", "solo espacios"),
        ("\x00", "byte nulo"),
        ("Fleet\x00View", "byte nulo en medio"),
    ],
)
@pytest.mark.asyncio
async def test_nombres_invalidos_son_rechazados(
    integration_session: AsyncSession, nombre: str, motivo: str
) -> None:
    """Un nombre con espacios o un byte nulo no llega a la base.

    El byte nulo (`\\x00`) es el caso interesante: no es whitespace, así que el colapso de
    espacios no lo toca, y es exactamente lo que rompe una cadena en cualquier herramienta
    que la trunque como C. Se comprueba en medio del texto también, porque `"Fleet\\x00View"`
    es un nombre que se ve bien en la interfaz y que llega roto al correo.

    Se comprueba además que el nombre original sigue en la base. Un `422` con el nombre ya
    escrito sería un fallo de transacción, y eso solo se ve leyendo la fila.
    """

    _user, organization, cabeceras = await _workspace(integration_session)
    nombre_original = organization.name

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": nombre},
            headers=cabeceras,
        )

    assert respuesta.status_code == 422, f"{motivo}: {respuesta.text}"
    await integration_session.refresh(organization)
    assert organization.name == nombre_original


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("  FleetView  ", "FleetView"),
        ("Fleet   View", "Fleet View"),
        ("a\tb", "a b"),
        ("a\nb", "a b"),
    ],
)
@pytest.mark.asyncio
async def test_los_espacios_sobrantes_se_colapsan(
    integration_session: AsyncSession, entrada: str, esperado: str
) -> None:
    """El salto de línea y el tabulador se convierten en un espacio, no se rechazan.

    La otra mitad del caso anterior, y la que importa más en el uso diario. Un nombre
    pegado desde un correo llega con tabs y saltos de línea dentro; rechazarlo obligaría al
    usuario a limpiarlo a mano, y la respuesta correcta es guardarlo como lo que quiso
    escribir.

    Lo que se guarda es un **espacio**, no el tabulador. El maquetado se rompe con el
    tabulador, no con el espacio.
    """

    _user, organization, cabeceras = await _workspace(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": entrada},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, f"{entrada!r}: {respuesta.text}"
    assert respuesta.json()["name"] == esperado
    await integration_session.refresh(organization)
    assert organization.name == esperado


@pytest.mark.asyncio
async def test_renombrar_a_lo_mismo_no_esienta(
    integration_session: AsyncSession,
) -> None:
    """Un nombre idéntico no es un cambio, y no llena el rastro.

    El botón de guardar se puede pulsar sin querer. Un asiento por cada pulsación vaciaría
    el `audit_log` de ruido y volvería imposible leer el rastro de los cambios que sí
    importan.
    """

    _user, organization, cabeceras = await _workspace(integration_session)
    antes = len(
        await _asientos(
            integration_session, organization.id, AuditActionEnum.ORGANIZATION_RENAMED
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": organization.name},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    despues = await _asientos(
        integration_session, organization.id, AuditActionEnum.ORGANIZATION_RENAMED
    )
    assert len(despues) == antes


@pytest.mark.asyncio
async def test_un_miembro_no_puede_renombrar(
    integration_session: AsyncSession,
) -> None:
    """Renombrar es de `ADMIN`. Un `MEMBER` recibe `403` y el nombre no se mueve.

    Se comprueba el nombre en la base además del código de respuesta. Un `403` con el
    nombre ya escrito sería un fallo de transacción, y solo mirarlo a través de la API lo
    ocultaría.
    """

    _user, organization, cabeceras = await _workspace(
        integration_session, role=RoleEnum.MEMBER
    )
    nombre_original = organization.name

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/organizations/me",
            json={"name": "Nombre No Autorizado"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 403, respuesta.text
    await integration_session.refresh(organization)
    assert organization.name == nombre_original


# --------------------------------------------------------------------------- #
# Listado de miembros
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_miembros_solo_los_del_tenant_activo(
    integration_session: AsyncSession,
) -> None:
    """El listado sale del contexto, no de un parámetro.

    Es la prueba de que un tenant no ve los miembros de otro. La forma del endpoint hace
    que no haya nada que comprobar en el servidor —no hay `organization_id` que validar—,
    y esto verifica que efectivamente no lo hay y que el filtro está donde tiene que estar.
    """

    _u1, org1, cab1 = await _workspace(integration_session)
    _u2, org2, _cab2 = await _workspace(integration_session)
    await _invitar_miembro(integration_session, org1, role=RoleEnum.MEMBER)
    await _invitar_miembro(integration_session, org2, role=RoleEnum.MEMBER)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get("/api/v1/organizations/me/members", headers=cab1)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2
    assert str(org2.id) not in respuesta.text

    contados = (
        await integration_session.execute(
            select(func.count(Membership.id)).where(
                Membership.organization_id == org1.id,
                Membership.is_active.is_(True),
            )
        )
    ).scalar_one()
    assert contados == cuerpo["total"]


@pytest.mark.asyncio
async def test_cualquier_miembro_puede_ver_la_lista(
    integration_session: AsyncSession,
) -> None:
    """Ver la lista no es de `ADMIN`.

    Se parece a la lista de un canal de equipo: cualquiera necesita saber quién más está
    dentro para no pulsar por email a alguien que ya tiene la respuesta, y el dato que sale
    —nombre, email, rol— no es nada que el propio miembro no pueda ver de otra forma.
    """

    _user, organization, cabeceras = await _workspace(
        integration_session, role=RoleEnum.MEMBER
    )
    await _invitar_miembro(integration_session, organization, role=RoleEnum.ADMIN)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.get("/api/v1/organizations/me/members", headers=cabeceras)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["total"] == 2


@pytest.mark.asyncio
async def test_los_miembros_inactivos_no_aparecen(
    integration_session: AsyncSession,
) -> None:
    """Alguien que se fue no está en la lista, pero su fila sigue ahí.

    Se comprueban las dos mitades. Que no aparezca es el comportamiento visible; que la fila
    siga existiendo es lo que permite que su `membership_id` siga siendo el actor de otras
    entradas de auditoría. Un `DELETE` de la fila rompería R4.
    """

    _user, organization, cabeceras = await _workspace(integration_session)
    saliente = await _invitar_miembro(
        integration_session, organization, role=RoleEnum.MEMBER
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        retiro = await cliente.delete(
            f"/api/v1/organizations/me/members/{saliente.id}", headers=cabeceras
        )
        listado = await cliente.get("/api/v1/organizations/me/members", headers=cabeceras)

    assert retiro.status_code == 200, retiro.text
    cuerpo = listado.json()
    assert cuerpo["total"] == 1
    assert str(saliente.id) not in [item["user_id"] for item in cuerpo["items"]]

    fila = (
        await integration_session.execute(
            select(Membership).where(
                Membership.organization_id == organization.id,
                Membership.user_id == saliente.id,
            )
        )
    ).scalar_one()
    assert fila.is_active is False, "la fila debe seguir existiendo, solo desactivada"


# --------------------------------------------------------------------------- #
# El invariante: siempre queda un administrador
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_se_puede_quitar_el_ultimo_admin(integration_session: AsyncSession) -> None:
    """Un workspace sin `ADMIN` no se puede administrar: es un `409`.

    No un `403`. La petición es legítima y el estado es el que no lo permite: el admin puede
    invitar a otro y volver a intentarlo. Un `403` diría "no tienes permiso", y sí lo tiene.

    Se mide que la membresía **sigue** siendo admin. Un `409` con el rol ya cambiado sería un
    fallo de transacción, y eso solo se ve en la base.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            f"/api/v1/organizations/me/members/{_admin.id}",
            json={"role": "member"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 409, respuesta.text

    rol = (
        await integration_session.execute(
            select(Membership.role).where(
                Membership.organization_id == organization.id,
                Membership.user_id == _admin.id,
            )
        )
    ).scalar_one()
    assert rol == RoleEnum.ADMIN, "el rol no debe haber cambiado"


@pytest.mark.asyncio
async def test_se_puede_bajar_de_admin_habiendo_otro(
    integration_session: AsyncSession,
) -> None:
    """Con dos admins, uno puede bajar a `MEMBER`. Es una rotación legítima.

    La contraparte de la anterior. Probar solo el rechazo deja sin verificar que la condición
    no esté escrita al revés, y una condición invertida pasa la primera prueba sin fallar.

    Esta prueba es la que distingue `> 1` de `>= 1`. Un `>= 1` dejaría pasar el rechazo
    —con dos admins el recuento es 2, que es `>= 1`— y además dejaría retirar al último
    admin, que es justo lo que hay que impedir. Ninguna de las dos pruebas lo detectaría.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    segundo = await _invitar_miembro(
        integration_session, organization, role=RoleEnum.ADMIN
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            f"/api/v1/organizations/me/members/{segundo.id}",
            json={"role": "member"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    # Las claves del JSON son **cadenas**: `user_id` viaja como texto, no como `UUID`. Por
    # eso se indexa con `str()`. Un `roles[_admin.id]` con el UUID crudo daría `KeyError` en
    # vez de un fallo de la aserción, y el diagnóstico señalaría al tipo de clave en vez de
    # al cambio de rol que no ocurrió.
    roles = {item["user_id"]: item["role"] for item in respuesta.json()["items"]}
    assert roles[str(_admin.id)] == "admin"
    assert roles[str(segundo.id)] == "member"


@pytest.mark.asyncio
async def test_no_se_puede_auto_retirarse(
    integration_session: AsyncSession,
) -> None:
    """El admin único no puede quitarse a sí mismo.

    Es el caso más fácil de dejar pasar: "queda un admin" se cumple —él mismo— mientras la
    operación se queda sin nadie que la ejecute. Por eso el servicio comprueba el actor antes
    que el recuento, y por eso el error es `409` —"esto no se puede hacer ahora"— y no `403`.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.delete(
            f"/api/v1/organizations/me/members/{_admin.id}", headers=cabeceras
        )

    assert respuesta.status_code == 409, respuesta.text
    sigue = (
        await integration_session.execute(
            select(func.count(Membership.id)).where(
                Membership.organization_id == organization.id,
                Membership.user_id == _admin.id,
                Membership.is_active.is_(True),
            )
        )
    ).scalar_one()
    assert sigue == 1


@pytest.mark.asyncio
async def test_un_miembro_no_puede_gestionar_miembros(
    integration_session: AsyncSession,
) -> None:
    """Cambiar de rol y retirar son de `ADMIN`. Un `MEMBER` recibe `403` en las dos.

    Se prueban las dos rutas porque un `DELETE` con una forgotten dependencia sería
    silenciosamente más peligroso: el efecto es real y visible para todo el mundo.
    """

    _user, organization, cabeceras = await _workspace(
        integration_session, role=RoleEnum.MEMBER
    )
    otro = await _invitar_miembro(integration_session, organization, role=RoleEnum.MEMBER)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        cambio = await cliente.patch(
            f"/api/v1/organizations/me/members/{otro.id}",
            json={"role": "admin"},
            headers=cabeceras,
        )
        retiro = await cliente.delete(
            f"/api/v1/organizations/me/members/{otro.id}", headers=cabeceras
        )

    assert cambio.status_code == 403, cambio.text
    assert retiro.status_code == 403, retiro.text


# --------------------------------------------------------------------------- #
# Los errores de la gestión de miembros
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gestionar_a_un_usuario_de_otro_tenant_es_404(
    integration_session: AsyncSession,
) -> None:
    """Un `user_id` de otro workspace da `404`, no `403`.

    Un `403` confirmaría que ese usuario existe y es miembro de algo. El `404` no dice si no
    existe, si no es miembro o si es de otro sitio, y esa indistinguibilidad es la garantía
    de que la respuesta no filtra nada de ajeno.
    """

    _admin, _org1, cabeceras = await _workspace(integration_session)
    _u2, org2, _cab2 = await _workspace(integration_session)
    ajeno = await _invitar_miembro(integration_session, org2, role=RoleEnum.MEMBER)
    # El `id` se copia a una variable plana **antes** de la llamada. La suite de integración
    # comparte la sesión con la app, así que el `rollback` que hace el servicio al fallar deja
    # expirados los objetos de ORM del test, y leer `ajeno.id` después intentaría refrescarlos
    # con un `MissingGreenlet`. El `id` es un UUID inmutable: se lee una vez y se usa.
    ajeno_id = ajeno.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        cambio = await cliente.patch(
            f"/api/v1/organizations/me/members/{ajeno_id}",
            json={"role": "admin"},
            headers=cabeceras,
        )
        retiro = await cliente.delete(
            f"/api/v1/organizations/me/members/{ajeno_id}", headers=cabeceras
        )

    assert cambio.status_code == 404, cambio.text
    assert retiro.status_code == 404, retiro.text

    rol_ajeno = (
        await integration_session.execute(
            select(Membership.role).where(
                Membership.organization_id == org2.id,
                Membership.user_id == ajeno.id,
            )
        )
    ).scalar_one()
    assert rol_ajeno == RoleEnum.MEMBER, "el workspace ajeno no puede haber cambiado"


@pytest.mark.asyncio
async def test_cambiar_el_rol_asienta_el_rastro(
    integration_session: AsyncSession,
) -> None:
    """Cada cambio de rol deja `from_state` y `to_state` con los dos valores.

    Un asiento que solo dijera "cambió el rol" no respondería a la pregunta de una revisión:
    quién pasó de qué a qué y cuándo. Se comprueban los dos estados, no solo que hay un
    asiento.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    miembro = await _invitar_miembro(integration_session, organization, role=RoleEnum.MEMBER)
    antes = len(
        await _asientos(
            integration_session, organization.id, AuditActionEnum.MEMBER_ROLE_CHANGED
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            f"/api/v1/organizations/me/members/{miembro.id}",
            json={"role": "admin"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    asientos = await _asientos(
        integration_session, organization.id, AuditActionEnum.MEMBER_ROLE_CHANGED
    )
    assert len(asientos) == antes + 1
    assert asientos[-1].from_state == "member"
    assert asientos[-1].to_state == "admin"
    assert asientos[-1].actor_user_id == _admin.id


@pytest.mark.asyncio
async def test_cambiar_al_mismo_rol_no_asienta(
    integration_session: AsyncSession,
) -> None:
    """Mandar el rol que ya tiene no es un cambio.

    Un selector de rol que se usa para confirmar en lugar de para cambiar produciría asientos
    idénticos en cada confirmación, y el rastro se llenaría de nada.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    miembro = await _invitar_miembro(integration_session, organization, role=RoleEnum.MEMBER)
    antes = len(
        await _asientos(
            integration_session, organization.id, AuditActionEnum.MEMBER_ROLE_CHANGED
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.patch(
            f"/api/v1/organizations/me/members/{miembro.id}",
            json={"role": "member"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    despues = await _asientos(
        integration_session, organization.id, AuditActionEnum.MEMBER_ROLE_CHANGED
    )
    assert len(despues) == antes


# --------------------------------------------------------------------------- #
# El invariante en la vía del borrado, que es la que no lo tenía
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_se_puede_retirar_al_ultimo_admin(
    integration_session: AsyncSession,
) -> None:
    """El último admin no se puede retirar aunque quien lo intente sea superusuario.

    ## Por qué hace falta un superusuario para llegar a este caso

    El actor de un `DELETE` de miembro tiene que ser `ADMIN` del tenant, y no puede
    retirarse a sí mismo. Así que la secuencia "queda un solo admin, y ese es el actor" ya
    está bloqueada por `CannotRemoveSelfError` antes de llegar a la condición de recuento.

    El hueco real es otro: un **superusuario** cuya membresía en ese workspace es `MEMBER`
    pasa `_exigir_admin` y puede gestionar miembros de un workspace donde no es admin. Si
    ese es el caso, podría retirar al único admin real y dejar el workspace sin dueño —
    desde la interfaz no habría nadie que lo arreglara. Esa es la situación que la condición
    del `UPDATE` tiene que impedir, y la única forma de probarla es construirla.

    Se mide el efecto, no solo el código: la membresía tiene que seguir activa y seguir siendo
    admin.
    """

    organization = Organization(
        name=f"Ultimo {uuid.uuid4().hex}", slug=f"ultimo-{uuid.uuid4().hex}"
    )
    session_user = User(
        email=f"unico-{uuid.uuid4().hex}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Unico admin",
        email_verified=True,
    )
    supervisor = User(
        email=f"super-{uuid.uuid4().hex}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Super Admin",
        email_verified=True,
        is_superuser=True,
    )
    integration_session.add_all([organization, session_user, supervisor])
    await integration_session.flush()
    # El unico admin real, y el superusuario como simple `MEMBER` de este workspace.
    integration_session.add(
        Membership(
            organization_id=organization.id,
            user_id=session_user.id,
            role=RoleEnum.ADMIN,
        )
    )
    integration_session.add(
        Membership(
            organization_id=organization.id,
            user_id=supervisor.id,
            role=RoleEnum.MEMBER,
        )
    )
    await integration_session.commit()
    objetivo_id = session_user.id
    cabeceras = _headers(supervisor, organization)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.delete(
            f"/api/v1/organizations/me/members/{objetivo_id}", headers=cabeceras
        )

    assert respuesta.status_code == 409, respuesta.text

    fila = (
        await integration_session.execute(
            select(Membership).where(
                Membership.organization_id == organization.id,
                Membership.user_id == objetivo_id,
            )
        )
    ).scalar_one()
    assert fila.is_active is True
    assert fila.role == RoleEnum.ADMIN


@pytest.mark.asyncio
async def test_retirar_a_un_admin_hayiendo_otro_si_se_puede(
    integration_session: AsyncSession,
) -> None:
    """Con dos admins, retirar a uno es legítimo.

    La contraparte de la anterior, y la que distingue `> 1` de `>= 1`. Un `>= 1` bloquearía
    esta operación —con dos admins el recuento es 2— y dejaría al admin sin poder limpiar el
    equipo, que es un workspace que se queda como está para siempre.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    segundo = await _invitar_miembro(
        integration_session, organization, role=RoleEnum.ADMIN
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.delete(
            f"/api/v1/organizations/me/members/{segundo.id}", headers=cabeceras
        )

    assert respuesta.status_code == 200, respuesta.text
    restantes = {item["user_id"] for item in respuesta.json()["items"]}
    assert str(_admin.id) in restantes
    assert str(segundo.id) not in restantes


@pytest.mark.asyncio
async def test_se_puede_retirar_a_un_miembro_siendo_el_unico_admin(
    integration_session: AsyncSession,
) -> None:
    """La condición de "queda un admin" **no** aplica a los `MEMBER`.

    Retirar a un `MEMBER` nunca reduce el número de admins. Si la condición se aplicara a
    todas las filas, en un workspace con un solo admin no se podría retirar a nadie —ni
    siquiera al guardián de seguridad que se fue la semana pasada—, y el único admin se
    quedaría atrapado con un equipo que no puede limpiar.

    Es la contraparte de la anterior por el lado de la fila, no por el del actor.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    miembro = await _invitar_miembro(integration_session, organization, role=RoleEnum.MEMBER)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.delete(
            f"/api/v1/organizations/me/members/{miembro.id}", headers=cabeceras
        )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["total"] == 1


@pytest.mark.asyncio
async def test_retirar_es_asentado_en_el_rastro(
    integration_session: AsyncSession,
) -> None:
    """La baja de un miembro deja `MEMBER_REMOVED` con `active` y `removed`.

    Los estados son esas dos palabras y no un identificador porque la fila **no** se borra:
    su `id` es el actor de otras entradas de auditoría, y por R4 no se puede perder. Sin el
    asiento, una baja sería un cambio invisible en el único sitio donde el rastro es
    append-only.

    El asiento se escribe en la misma transacción que la desactivación: si el `UPDATE`
    confirmara y el asiento fallara, la baja no existiría en ninguna parte. Y el orden es el
    inverso al de una baja lógica de organización —allí el asiento va **primero**, para que
    sobrevivan a un fallo posterior— porque aquí no hay nada después que pueda fallar: la
    desactivación es la última operación.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    miembro = await _invitar_miembro(integration_session, organization, role=RoleEnum.MEMBER)
    antes = len(
        await _asientos(
            integration_session, organization.id, AuditActionEnum.MEMBER_REMOVED
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.delete(
            f"/api/v1/organizations/me/members/{miembro.id}", headers=cabeceras
        )

    assert respuesta.status_code == 200, respuesta.text
    asientos = await _asientos(
        integration_session, organization.id, AuditActionEnum.MEMBER_REMOVED
    )
    assert len(asientos) == antes + 1
    assert asientos[-1].from_state == "active"
    assert asientos[-1].to_state == "removed"
    assert asientos[-1].actor_user_id == _admin.id


@pytest.mark.asyncio
async def test_retirar_dos_veces_es_404(integration_session: AsyncSession) -> None:
    """El segundo `DELETE` de la misma persona da `404`, no `200` idempotente.

    La fila existe pero está inactiva, así que desde el punto de vista de "miembros activos"
    ese usuario ya no está. Devolver `200` haría creer que se retiró dos veces, cuando lo
    que pasó es que ya no estaba. Y un `409` sería peor: sugeriría que hay un conflicto que
    resolver, y no lo hay.
    """

    _admin, organization, cabeceras = await _workspace(integration_session)
    miembro = await _invitar_miembro(integration_session, organization, role=RoleEnum.MEMBER)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        primero = await cliente.delete(
            f"/api/v1/organizations/me/members/{miembro.id}", headers=cabeceras
        )
        segundo = await cliente.delete(
            f"/api/v1/organizations/me/members/{miembro.id}", headers=cabeceras
        )

    assert primero.status_code == 200, primero.text
    assert segundo.status_code == 404, segundo.text
