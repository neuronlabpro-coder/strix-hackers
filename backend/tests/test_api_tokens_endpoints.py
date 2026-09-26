"""Pruebas de los endpoints de tokens y de la autenticación dual.

La suite del catálogo comprueba que el contrato declarado es el correcto. Esta comprueba
que el contrato se **hace cumplir**: que un token sin scope recibe 403, que uno de otro
tenant recibe 404, y que el mismo endpoint responde distinto según quien pregunte.

Cada prueba usa un token real generado por el servicio y firmado con el hash que la
dependencia va a buscar. Nada se sustituye: si el hash se calculara distinto en las dos
mitades, todas estas pruebas fallarían con un 401 y sería la señal correcta.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.models import ApiToken
from backend.apps.api_access.schemas import ApiTokenCreate
from backend.apps.api_access.scopes import Scope
from backend.apps.api_access.service import create_api_token
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.security import (
    create_access_token,
    hash_api_token,
    hash_password,
)
from backend.main import app

pytestmark = pytest.mark.integration

TOKENS_URL = "/api/v1/auth/tokens"


class Tenant:
    """Un tenant con sus cabeceras de sesión y su contexto ya resuelto."""

    def __init__(self, organization: Organization, user: User, role: RoleEnum) -> None:
        self.organization = organization
        self.user = user
        self.role = role
        self.token = create_access_token({"sub": str(user.id)})
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Organization-Id": str(organization.id),
        }
        self.organization_id = organization.id
        self.user_id = user.id


async def _tenant(
    session: AsyncSession,
    *,
    role: RoleEnum = RoleEnum.ADMIN,
    prefix: str = "tok",
) -> Tenant:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"{prefix} {suffix}", slug=f"{prefix}-{suffix}")
    user = User(
        email=f"{prefix}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsaEnEstaPrueba"),
        full_name=f"{prefix} User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
    await session.commit()
    return Tenant(organization, user, role)


async def _emitir(
    session: AsyncSession, tenant: Tenant, *scopes: Scope, days: int = 90
) -> str:
    """Emite un token real y devuelve su secreto en claro."""

    _token, raw = await create_api_token(
        session,
        tenant.organization_id,
        ApiTokenCreate(
            name="prueba",
            scopes=[scope.value for scope in scopes],
            expires_in_days=days,
        ),
    )
    return raw


def _api(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


# --------------------------------------------------------------------------- #
# Emisión
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_creates_a_token_and_sees_the_secret_exactly_once(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={
                "name": "CI de despliegue",
                "scopes": [Scope.PENTESTS_READ.value, Scope.VULNERABILITIES_READ.value],
                "expires_in_days": 30,
            },
            headers=tenant.headers,
        )
        listado = await client.get(TOKENS_URL, headers=tenant.headers)

    assert response.status_code == 201, response.text
    cuerpo = response.json()
    assert cuerpo["raw_token"].startswith("mgf_live_")
    assert cuerpo["name"] == "CI de despliegue"
    assert cuerpo["token_prefix"] == cuerpo["raw_token"][:13]
    assert cuerpo["scopes"] == ["pentests:read", "vulnerabilities:read"]
    assert cuerpo["expires_at"] is not None

    # Y el listado no lo repite nunca.
    assert "raw_token" not in listado.text
    assert cuerpo["raw_token"] not in listado.text


@pytest.mark.asyncio
async def test_the_secret_is_never_persisted_in_clear(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={"name": "n", "scopes": [Scope.PENTESTS_READ.value]},
            headers=tenant.headers,
        )
    raw = response.json()["raw_token"]

    fila = (
        await integration_session.execute(
            select(ApiToken).where(ApiToken.organization_id == tenant.organization_id)
        )
    ).scalar_one()
    assert fila.token_hash == hash_api_token(raw)
    assert raw not in fila.token_hash
    # Tampoco aparece el hash en ninguna columna de texto.
    assert fila.token_hash not in fila.name
    assert fila.token_hash not in fila.token_prefix


@pytest.mark.asyncio
async def test_unknown_scopes_are_rejected_with_a_422_that_names_them(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={
                "name": "n",
                "scopes": [Scope.PENTESTS_READ.value, "pentests:write"],
            },
            headers=tenant.headers,
        )

    assert response.status_code == 422
    # El detalle nombra el scope rechazado: sin eso el cliente compara listas a mano.
    assert "pentests:write" in response.text
    # El recuento va acotado al tenant de la prueba. Contar filas globales convierte la
    # aserción en dependiente de lo que hubiera en la base: falla en cuanto otro test
    # deja un token, y un test que depende del orden no mide lo que dice medir.
    filas = (
        await integration_session.execute(
            select(ApiToken).where(ApiToken.organization_id == tenant.organization_id)
        )
    ).scalars().all()
    assert len(filas) == 0


@pytest.mark.asyncio
async def test_a_token_without_scopes_cannot_be_created(
    integration_session: AsyncSession,
) -> None:
    """Un token sin permisos es una credencial que no puede hacer nada.

    Se rechaza con `422`. Devolver `201` con un token inservible sería peor: el
    cliente lo guarda, lo despliega y descubre que falla en el primer `GET`, en
    producción y no en el momento de crearlo.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={"name": "n", "scopes": []},
            headers=tenant.headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_expiry_beyond_the_ceiling_is_rejected(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={
                "name": "n",
                "scopes": [Scope.PENTESTS_READ.value],
                "expires_in_days": 3650,
            },
            headers=tenant.headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_member_cannot_issue_tokens(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session, role=RoleEnum.MEMBER)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={"name": "n", "scopes": [Scope.PENTESTS_READ.value]},
            headers=tenant.headers,
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_issuing_tokens_requires_authentication(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL, json={"name": "n", "scopes": [Scope.PENTESTS_READ.value]}
        )

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Autenticación dual: un token de API accede a su propia gestión
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_token_with_tokens_read_can_list_but_never_its_own_hash(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=_api(raw))

    assert response.status_code == 200
    cuerpo = response.json()
    assert cuerpo["total"] == 1
    assert "token_hash" not in response.text
    assert "raw_token" not in response.text


@pytest.mark.asyncio
async def test_a_token_without_tokens_read_is_refused_with_the_exact_detail(
    integration_session: AsyncSession,
) -> None:
    """El 403 dice exactamente lo que promete el contrato, y no dice más.

    Un detalle que enumerara los scopes que el token sí tiene ayudaría al cliente
    legítimo, pero también le diría a alguien con un token robado qué permisos tiene su
    víctima. Un 403 genérico obliga a volver a emitir el token con lo que se necesita.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.PENTESTS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=_api(raw))

    assert response.status_code == 403
    assert response.json()["detail"] == "Insufficient token permissions"
    # No se filtran los scopes que sí tiene.
    assert Scope.PENTESTS_READ.value not in response.text


@pytest.mark.asyncio
async def test_a_token_cannot_issue_tokens_without_the_create_scope(
    integration_session: AsyncSession,
) -> None:
    """Leer tokens y emitirlos son poderes distintos, y el catálogo los separa.

    Si `tokens:read` sirviera para crear, un token de solo lectura pourrait escalar a
    emitir tokens con todos los scopes. Es el salto de privilegio más obvio posible y
    por eso el catálogo no lo permite.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={
                "name": "escalado",
                "scopes": [Scope.ADMIN_MODELS_MANAGE.value],
            },
            headers=_api(raw),
        )

    assert response.status_code == 403
    total = (
        await integration_session.execute(
            select(ApiToken).where(
                ApiToken.organization_id == tenant.organization_id
            )
        )
    ).scalars().all()
    assert len(total) == 1, "se emitió un token sin tener el scope de creacion"


@pytest.mark.asyncio
async def test_a_token_with_create_can_issue_a_token_within_its_own_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_CREATE)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            TOKENS_URL,
            json={"name": "derivado", "scopes": [Scope.PENTESTS_READ.value]},
            headers=_api(raw),
        )

    assert response.status_code == 201, response.text
    assert response.json()["raw_token"].startswith("mgf_live_")


# --------------------------------------------------------------------------- #
# Rechazos
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_unknown_token_is_rejected(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    transport = ASGITransport(app=app)
    falso = "mgf_live_" + "0" * 64

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=_api(falso))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_token_stops_working(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        antes = await client.get(TOKENS_URL, headers=_api(raw))
        token_id = antes.json()["items"][0]["id"]
        revocado = await client.delete(f"{TOKENS_URL}/{token_id}", headers=tenant.headers)
        despues = await client.get(TOKENS_URL, headers=_api(raw))

    assert antes.status_code == 200
    assert revocado.status_code == 200
    assert despues.status_code == 401
    assert despues.json()["detail"] == "Token de API revocado"


@pytest.mark.asyncio
async def test_an_expired_token_stops_working(
    integration_session: AsyncSession,
) -> None:
    """La caducidad se comprueba en la autenticación, no solo al listar.

    Un token caducado que siguiera funcionando sería peor que uno que nunca se emitiera:
    el panel lo muestra en la lista como vigente y el cliente descubriría el problema al
    recibir un 401 en producción.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    fila = (
        await integration_session.execute(
            select(ApiToken).where(ApiToken.organization_id == tenant.organization_id)
        )
    ).scalar_one()
    fila.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=_api(raw))

    assert response.status_code == 401
    assert response.json()["detail"] == "Token de API caducado"


@pytest.mark.asyncio
async def test_revocation_and_expiry_give_the_same_status_but_named_details(
    integration_session: AsyncSession,
) -> None:
    """El status es el mismo a propósito; el detalle nominal sí distingue.

    Un `401` distinto para "revocado" y "caducado" no filtra información útil —el token
    es del cliente—, y en cambio salva una llamada al soporte en el caso más frecuente.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)
    revocation = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    caducidad = await _emitir(integration_session, tenant, Scope.TOKENS_READ)

    # Se localizan por prefijo y no por posición. Con dos tokens, `filas[0]` podría ser
    # cualquiera de los dos según el plan de ejecución, y revocar el caducado en vez del
    # otro haría que la prueba comparase un token revocado y caducado a la vez, que es un
    # estado que ninguna de las dos ramas del `401` cubre.
    filas = (
        await integration_session.execute(
            select(ApiToken).where(
                ApiToken.organization_id == tenant.organization_id,
                ApiToken.token_prefix.in_([revocation[:13], caducidad[:13]]),
            )
        )
    ).scalars().all()
    por_prefijo = {fila.token_prefix: fila for fila in filas}
    caducada = por_prefijo[caducidad[:13]]
    caducada.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await integration_session.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.delete(
            f"{TOKENS_URL}/{por_prefijo[revocation[:13]].id}", headers=tenant.headers
        )
        r_revocado = await client.get(TOKENS_URL, headers=_api(revocation))
        r_caducado = await client.get(TOKENS_URL, headers=_api(caducidad))

    assert r_revocado.status_code == r_caducado.status_code == 401
    assert r_revocado.json()["detail"] != r_caducado.json()["detail"]


# --------------------------------------------------------------------------- #
# Aislamiento multi-tenant
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_token_never_sees_another_tenants_tokens(
    integration_session: AsyncSession,
) -> None:
    """R3: el listado se filtra en la consulta, no en memoria.

    Un filtro en Python sobre una lista que ya trajo las filas ajenas no protege nada:
    basta con que una ruta futura se olvide de filtrar. Por eso la restricción vive en
    el `WHERE` del servicio.
    """

    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="victima")
    atacante = await _tenant(integration_session, prefix="atacante")
    await _emitir(integration_session, victima, Scope.PENTESTS_READ)
    await _emitir(integration_session, atacante, Scope.TOKENS_READ)
    raw_atacante = await _emitir(integration_session, atacante, Scope.TOKENS_READ)
    total_ajeno = (
        await integration_session.execute(
            select(ApiToken).where(
                ApiToken.organization_id == atacante.organization_id
            )
        )
    ).scalars().all()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=_api(raw_atacante))

    assert response.status_code == 200
    # El atacante tiene mas de un token, y solo ve los suyos.
    assert len(total_ajeno) > 1
    assert response.json()["total"] == len(total_ajeno)


@pytest.mark.asyncio
async def test_the_organization_header_cannot_redirect_a_token(
    integration_session: AsyncSession,
) -> None:
    """Un token ignora `X-Organization-Id`.

    Su tenant es el de su fila. Si aceptara la cabecera, cambiar una línea en la petición
    bastaría para actuar sobre otro cliente, y el aislamiento R3 no admite un interruptor.
    """

    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="vheader")
    atacante = await _tenant(integration_session, prefix="ahead")
    await _emitir(integration_session, victima, Scope.TOKENS_READ)
    raw = await _emitir(integration_session, atacante, Scope.TOKENS_READ)
    cabeceras = {**_api(raw), "X-Organization-Id": str(victima.organization_id)}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=cabeceras)

    assert response.status_code == 200
    # Sigue viendo los suyos, no los de la víctima.
    assert response.json()["total"] == 1
    assert victima.organization_id != atacante.organization_id
    prefijos = {item["token_prefix"] for item in response.json()["items"]}
    assert raw[:13] in prefijos


@pytest.mark.asyncio
async def test_revoking_another_tenants_token_returns_404_not_403(
    integration_session: AsyncSession,
) -> None:
    """Un `403` confirmaría que ese identificador existe.

    Con `404`, un token ajeno es indistinguible de uno que nunca se creó, y probar
    identificadores no revela nada. Aquí lo que se prueban son credenciales, así que la
    diferencia importa más que en el resto de la API.
    """

    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="rvictima")
    atacante = await _tenant(integration_session, prefix="ratacante")
    await _emitir(integration_session, victima, Scope.PENTESTS_READ)
    filas = (
        await integration_session.execute(
            select(ApiToken).where(
                ApiToken.organization_id == victima.organization_id
            )
        )
    ).scalars().all()
    objetivo = filas[0].id
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"{TOKENS_URL}/{objetivo}", headers=atacante.headers
        )

    assert response.status_code == 404
    intacto = (
        await integration_session.execute(
            select(ApiToken).where(ApiToken.id == objetivo)
        )
    ).scalar_one()
    assert intacto.revoked_at is None


@pytest.mark.asyncio
async def test_a_token_of_a_deleted_organization_stops_working(
    integration_session: AsyncSession,
) -> None:
    """Borrar lógicamente el tenant apaga sus tokens sin revocar ninguno.

    Es la diferencia entre reír de doscientos tokens a mano y no tener que hacerlo, y no
    puede quedar solo en el `DELETE` de la organización: un token válido sobrevive ahí
    mucho después.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        antes = await client.get(TOKENS_URL, headers=_api(raw))
        borrado = await client.delete(
            f"/api/v1/organizations/{tenant.organization_id}", headers=tenant.headers
        )
        despues = await client.get(TOKENS_URL, headers=_api(raw))

    assert antes.status_code == 200
    assert borrado.status_code == 200
    assert despues.status_code == 401


# --------------------------------------------------------------------------- #
# Sesión web y trazabilidad
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_web_admin_keeps_working_without_holding_scopes(
    integration_session: AsyncSession,
) -> None:
    """Un usuario web no necesita scopes: su autorización es el rol.

    Exigirle un scope a alguien que ha iniciado sesión en su propio workspace sería
    inventar un permiso que el panel no puede asignar, y haría que el panel y la API
    tuvieran modelos de permiso distintos para la misma persona.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, role=RoleEnum.ADMIN)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listado = await client.get(TOKENS_URL, headers=tenant.headers)
        emitido = await client.post(
            TOKENS_URL,
            json={"name": "n", "scopes": [Scope.PENTESTS_READ.value]},
            headers=tenant.headers,
        )

    assert listado.status_code == 200
    assert emitido.status_code == 201


@pytest.mark.asyncio
async def test_authenticating_a_token_records_its_use(
    integration_session: AsyncSession,
) -> None:
    """`last_used_at` es lo que permite distinguir un token en uso de uno olvidado.

    Es también el dato que evita que alguien revoque el token que su CI está usando sin
    saberlo, que es el error de gestión más caro de este tipo de credenciales.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    fila = (
        await integration_session.execute(
            select(ApiToken).where(ApiToken.organization_id == tenant.organization_id)
        )
    ).scalar_one()
    assert fila.last_used_at is None, "un token recien creado no puede figurar como usado"
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(TOKENS_URL, headers=_api(raw))

    assert response.status_code == 200
    refrescado = (
        await integration_session.execute(select(ApiToken).where(ApiToken.id == fila.id))
    ).scalar_one()
    assert refrescado.last_used_at is not None
    assert refrescado.last_used_at > datetime.now(UTC) - timedelta(minutes=1)


@pytest.mark.asyncio
async def test_listing_can_include_revoked_tokens(
    integration_session: AsyncSession,
) -> None:
    """Poder ver los revocados es lo que permite auditar qué credenciales existieron.

    Sin `include_revoked`, un token revocado desaparece del panel y no hay forma de
    responder "¿este token estuvo activo el día del incidente?".
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, Scope.TOKENS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        activo = await client.get(TOKENS_URL, headers=tenant.headers)
        fila_id = activo.json()["items"][0]["id"]
        await client.delete(f"{TOKENS_URL}/{fila_id}", headers=tenant.headers)
        despues = await client.get(TOKENS_URL, headers=tenant.headers)
        con_revocados = await client.get(
            f"{TOKENS_URL}?include_revoked=true", headers=tenant.headers
        )

    assert despues.json()["total"] == 0
    assert con_revocados.json()["total"] == 1
    assert con_revocados.json()["items"][0]["revoked_at"] is not None
    del raw


# --------------------------------------------------------------------------- #
# Autorización administrativa por rol
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_organization_keeps_its_financial_ledger_intact(
    integration_session: AsyncSession,
) -> None:
    """Emitir un token no toca el ledger: R4 sigue valiendo con credenciales de por medio.

    Es una comprobación de que el camino nuevo no pasa por el mismo sitio que el dinero,
    y de que el saldo que muestra el panel sigue siendo el que se calculó.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    assert tenant.organization.credit_balance == Decimal("0")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            TOKENS_URL,
            json={"name": "n", "scopes": [Scope.PENTESTS_READ.value]},
            headers=tenant.headers,
        )

    fila = (
        await integration_session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    assert fila.credit_balance == Decimal("0")
