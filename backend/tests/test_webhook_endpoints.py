"""Pruebas de los endpoints de webhooks.

Comprueban las tres cosas que un endpoint de webhook puede salir mal de forma distinta:
la autorización por scope, el aislamiento entre tenants, y que el secreto no se exponga
por la puerta de atrás. Las tres se prueban por HTTP contra la aplicación completa, con el
resolutor de DNS sustituido para que la guarda anti-SSRF deje pasar un destino público.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.webhooks.models import WebhookDelivery, WebhookEndpoint
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

DESTINO = "https://hooks.example.com/entrada"


@pytest.fixture(autouse=True)
def _destino_resoluble() -> Any:
    """Hace que el host del endpoint resuelva a una IP pública.

    Se sustituye el **resolutor**, no la guarda: la validación anti-SSRF sigue ejecutándose
    entera en cada petición, que es justo lo que una prueba de endpoints no debe desactivar
    para poder probar lo de más arriba.
    """

    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    ):
        yield


class Tenant:
    def __init__(self, organization: Organization, user: User, role: RoleEnum) -> None:
        self.organization_id = organization.id
        self.role = role
        self.token = create_access_token({"sub": str(user.id)})
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Organization-Id": str(organization.id),
        }


async def _tenant(
    session: AsyncSession, *, role: RoleEnum = RoleEnum.ADMIN, prefix: str = "hook"
) -> Tenant:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"{prefix} {suffix}", slug=f"{prefix}-{suffix}")
    user = User(
        email=f"{prefix}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"{prefix} User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
    await session.commit()
    return Tenant(organization, user, role)


def _crear(tenant: Tenant, **extra: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "url": DESTINO,
        "event_types": ["pentest.completed"],
    }
    cuerpo.update(extra)
    return cuerpo


# --------------------------------------------------------------------------- #
# Alta
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_alta_devuelve_el_secreto_una_sola_vez(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )
        listado = await client.get("/api/v1/webhooks", headers=tenant.headers)

    assert response.status_code == 201, response.text
    cuerpo = response.json()
    assert cuerpo["signing_secret"].startswith("whsec_")
    assert len(cuerpo["signing_secret"]) == 70
    # Y el listado no lo repite, ni la fila de la base.
    assert "signing_secret" not in listado.text
    assert "encrypted_secret" not in listado.text
    assert cuerpo["signing_secret"] not in listado.text


@pytest.mark.asyncio
async def test_el_secreto_se_guarda_cifrado(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )

    secreto = response.json()["signing_secret"]
    fila = (
        await integration_session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == tenant.organization_id
            )
        )
    ).scalar_one()
    assert secreto not in fila.encrypted_secret
    assert fila.encrypted_secret.startswith("v1.")


@pytest.mark.asyncio
async def test_un_evento_desconocido_se_rechaza_nombrandolo(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks",
            json=_crear(tenant, event_types=["pentest.explosionado"]),
            headers=tenant.headers,
        )

    assert response.status_code == 422
    assert "pentest.explosionado" in response.text
    # El recuento va acotado al tenant de la prueba. Contar filas globales convierte la
    # aserción en dependiente de lo que hubiera en la base: falla en cuanto otra prueba o
    # una verificación manual deja un endpoint, y un test que depende del estado previo
    # no mide lo que dice medir.
    filas = (
        await integration_session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == tenant.organization_id
            )
        )
    ).scalars().all()
    assert len(filas) == 0


@pytest.mark.asyncio
async def test_la_lista_de_eventos_vacia_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks", json=_crear(tenant, event_types=[]), headers=tenant.headers
        )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# SSRF a traves de la API
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "motivo"),
    [
        ("http://169.254.169.254/latest/meta-data", "metadatos"),
        ("https://127.0.0.1:8080/hook", "bucle local"),
        ("https://10.0.0.5/internal", "privada"),
        ("https://192.168.1.1/hook", "privada"),
        ("https://[::1]/hook", "bucle local"),
        ("https://[fd00::1]/hook", "privada"),
        ("https://localhost/hook", "localhost"),
        ("file:///etc/passwd", "http"),
        ("gopher://127.0.0.1:11211/", "http"),
    ],
)
@pytest.mark.asyncio
async def test_la_url_bloqueada_se_rechaza_con_422(
    integration_session: AsyncSession, url: str, motivo: str
) -> None:
    """La guarda se aplica en la API, no solo en la función.

    Un validador que solo se llama desde el servicio dejaría abierta la puerta a que un
    endpoint nuevo se saltara la comprobación. Se prueba por HTTP para que la garantía
    cubra el camino completo.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks", json=_crear(tenant, url=url), headers=tenant.headers
        )

    assert response.status_code == 422, response.text
    # El detalle dice por qué, para que el usuario sepa si corregir la URL o el entorno.
    assert motivo in response.text
    filas = (
        await integration_session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == tenant.organization_id
            )
        )
    ).scalars().all()
    assert len(filas) == 0, "un endpoint bloqueado llego a guardarse"


@pytest.mark.asyncio
async def test_un_nombre_que_resuelve_a_la_red_interna_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.5.5", 0))],
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/webhooks",
                json=_crear(tenant, url="https://mi-empresa.example.com/hook"),
                headers=tenant.headers,
            )

    assert response.status_code == 422
    assert "192.168.5.5" in response.text


@pytest.mark.asyncio
async def test_publicar_un_secreto_de_firma_en_la_url_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """Un `whsec_` en la URL se revelaría al receptor en cada entrega."""

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    fugas = "whsec_" + "ab" * 32
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks",
            json=_crear(tenant, url=f"https://hooks.example.com/?clave={fugas}"),
            headers=tenant.headers,
        )

    assert response.status_code == 422
    assert "secreto de firma" in response.text


# --------------------------------------------------------------------------- #
# Listado y aislamiento
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_listado_solo_muestra_lo_que_el_contrato_promete(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/webhooks",
            json=_crear(tenant, description="mi endpoint"),
            headers=tenant.headers,
        )
        response = await client.get("/api/v1/webhooks", headers=tenant.headers)

    assert response.status_code == 200
    cuerpo = response.json()
    assert cuerpo["total"] == 1
    fila = cuerpo["items"][0]
    assert set(fila) == {
        "id",
        "url",
        "description",
        "event_types",
        "is_active",
        "consecutive_failures",
        "created_at",
        "updated_at",
    }
    assert fila["is_active"] is True
    assert fila["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_otro_tenant_no_aparece_en_el_listado(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="wvictima")
    atacante = await _tenant(integration_session, prefix="watacante")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/webhooks", json=_crear(victima), headers=victima.headers)
        response = await client.get("/api/v1/webhooks", headers=atacante.headers)

    assert response.status_code == 200
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_modificar_o_borrar_un_endpoint_ajeno_responde_404(
    integration_session: AsyncSession,
) -> None:
    """Un `403` confirmaría que ese identificador existe.

    Con `404`, un endpoint de otro tenant es indistinguible de uno que no se creó. Aquí lo
    que se prueban son credenciales de firma, así que la diferencia importa más que en el
    resto de la API.
    """

    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="w2victima")
    atacante = await _tenant(integration_session, prefix="w2atacante")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(victima), headers=victima.headers
        )
        objetivo = creado.json()["id"]
        parche = await client.patch(
            f"/api/v1/webhooks/{objetivo}",
            json={"description": "secuestrado"},
            headers=atacante.headers,
        )
        borrado = await client.delete(
            f"/api/v1/webhooks/{objetivo}", headers=atacante.headers
        )

    assert parche.status_code == 404
    assert borrado.status_code == 404
    intacto = (
        await integration_session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == objetivo)
        )
    ).scalar_one()
    assert intacto.description is None
    assert intacto.is_active is True


@pytest.mark.asyncio
async def test_el_tenant_se_toma_del_contexto_y_no_de_la_url(
    integration_session: AsyncSession,
) -> None:
    """La cabecera manda, y la URL no puede redirigir la operación.

    Con el aislamiento puesto en la cabecera, mandarla con la del atacante sería la forma
    más obvia de cruzar tenants. Se manda con la víctima y se pide el endpoint del
    atacante: tiene que fallar.
    """

    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="w3victima")
    atacante = await _tenant(integration_session, prefix="w3atacante")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        suyo = await client.post(
            "/api/v1/webhooks", json=_crear(atacante), headers=atacante.headers
        )
        response = await client.delete(
            f"/api/v1/webhooks/{suyo.json()['id']}", headers=victima.headers
        )

    assert response.status_code == 404
    sigue = (
        await integration_session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == suyo.json()["id"])
        )
    ).scalar_one()
    assert sigue is not None


# --------------------------------------------------------------------------- #
# Autorización por scope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_un_miembro_no_gestiona_webhooks(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    miembro = await _tenant(integration_session, role=RoleEnum.MEMBER)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        alta = await client.post("/api/v1/webhooks", json=_crear(miembro), headers=miembro.headers)
        listado = await client.get("/api/v1/webhooks", headers=miembro.headers)

    assert alta.status_code == 403
    assert listado.status_code == 403


@pytest.mark.asyncio
async def test_los_endpoints_exigen_autenticacion(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/webhooks")).status_code == 401
        assert (await client.get("/api/v1/webhooks/events")).status_code == 401
        # Con cabecera de sesion pero sin cabecera de organización también se rechaza: la
        # URL se valida contra un tenant que no existe, no contra ninguno.
        sin_tenant = {"Authorization": tenant.headers["Authorization"]}
        assert (await client.get("/api/v1/webhooks", headers=sin_tenant)).status_code == 403


@pytest.mark.asyncio
async def test_leer_no_basta_para_crear(integration_session: AsyncSession) -> None:
    """`webhooks:read` y `webhooks:create` son poderes distintos.

    Si leer sirviera para crear, un token de solo lectura podría registrar un endpoint
    apuntando a un servidor suyo y quedarse con una copia de todos los eventos del tenant.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    raw = await _emitir(integration_session, tenant, "webhooks:read")
    transport = ASGITransport(app=app)
    api = {"Authorization": f"Bearer {raw}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listado = await client.get("/api/v1/webhooks", headers=api)
        alta = await client.post("/api/v1/webhooks", json=_crear(tenant), headers=api)

    assert listado.status_code == 200
    assert alta.status_code == 403
    filas = (
        await integration_session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == tenant.organization_id
            )
        )
    ).scalars().all()
    assert len(filas) == 0


async def _emitir(session: AsyncSession, tenant: Tenant, scope: str) -> str:
    from backend.apps.api_access.schemas import ApiTokenCreate
    from backend.apps.api_access.service import create_api_token

    _token, raw = await create_api_token(
        session,
        tenant.organization_id,
        ApiTokenCreate(name="prueba", scopes=[scope], expires_in_days=30),
    )
    return raw


# --------------------------------------------------------------------------- #
# Patch, ping e historial
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pausar_un_endpoint_no_lo_borra(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )
        objetivo = creado.json()["id"]
        pausado = await client.patch(
            f"/api/v1/webhooks/{objetivo}", json={"is_active": False}, headers=tenant.headers
        )
        reactivado = await client.patch(
            f"/api/v1/webhooks/{objetivo}", json={"is_active": True}, headers=tenant.headers
        )

    assert pausado.json()["is_active"] is False
    assert reactivado.json()["is_active"] is True
    filas = (
        await integration_session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.organization_id == tenant.organization_id)
        )
    ).scalars().all()
    assert len(filas) == 1


@pytest.mark.asyncio
async def test_una_url_bloqueada_no_entra_por_el_patch(
    integration_session: AsyncSession,
) -> None:
    """La guarda se aplica también al cambiar la URL.

    Si el `PATCH` no la validara, un endpoint legítimo se convertiría en un escáner de red
    con un segundo paso.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )
        objetivo = creado.json()["id"]
        response = await client.patch(
            f"/api/v1/webhooks/{objetivo}",
            json={"url": "http://169.254.169.254/"},
            headers=tenant.headers,
        )

    assert response.status_code == 422
    intacta = (
        await integration_session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == objetivo)
        )
    ).scalar_one()
    assert intacta.url == DESTINO


@pytest.mark.asyncio
async def test_el_ping_manda_de_verdad_y_registra_la_entrega(
    integration_session: AsyncSession,
) -> None:
    """El ping sale de verdad. Un `{"ok": true}` local no demostraría nada.

    El usuario necesita saber si su endpoint responde, alcanza la plataforma y acepta el
    cuerpo firmado. Se comprueba la petición que sale y la fila que queda.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)
    peticiones: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        peticiones.append(request)
        return httpx.Response(200, text="pong")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )
        objetivo = creado.json()["id"]
        with patch(
            "backend.apps.webhooks.dispatcher._default_client_factory",
            side_effect=lambda: httpx.Client(
                transport=httpx.MockTransport(handler), follow_redirects=False
            ),
        ):
            ping = await client.post(
                f"/api/v1/webhooks/{objetivo}/ping", headers=tenant.headers
            )

    assert ping.status_code == 200, ping.text
    resultado = ping.json()
    assert resultado["delivered"] is True
    assert resultado["status_code"] == 200
    assert resultado["execution_time_ms"] is not None
    assert len(peticiones) == 1
    cuerpo = json.loads(peticiones[0].content)
    assert cuerpo == {
        "event": "ping",
        "message": "Mind Guard Fenix test delivery",
    }

    entregas = (
        await integration_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.endpoint_id == objetivo)
        )
    ).scalars().all()
    assert len(entregas) == 1
    assert entregas[0].event_type == "ping"


@pytest.mark.asyncio
async def test_el_historial_registra_cada_intento_y_solo_del_propio_endpoint(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="whist")
    otro = await _tenant(integration_session, prefix="whist2")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mio = await client.post("/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers)
        suyo = await client.post("/api/v1/webhooks", json=_crear(otro), headers=otro.headers)

        with patch(
            "backend.apps.webhooks.dispatcher._default_client_factory",
            side_effect=lambda: httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(500)),
                follow_redirects=False,
            ),
        ):
            await client.post(
                f"/api/v1/webhooks/{mio.json()['id']}/ping", headers=tenant.headers
            )

        historial = await client.get(
            f"/api/v1/webhooks/{mio.json()['id']}/deliveries", headers=tenant.headers
        )
        ajenos = await client.get(
            f"/api/v1/webhooks/{suyo.json()['id']}/deliveries", headers=otro.headers
        )

    assert historial.status_code == 200
    cuerpo = historial.json()
    assert cuerpo["total"] == 3, "tres intentos por el 500"
    assert [item["attempt"] for item in cuerpo["items"]] == [3, 2, 1]
    assert all(item["status_code"] == 500 for item in cuerpo["items"])
    assert ajenos.json()["total"] == 0


@pytest.mark.asyncio
async def test_el_historial_de_un_endpoint_ajeno_responde_404(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    victima = await _tenant(integration_session, prefix="wh4v")
    atacante = await _tenant(integration_session, prefix="wh4a")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(victima), headers=victima.headers
        )
        objetivo = creado.json()["id"]
        historial = await client.get(
            f"/api/v1/webhooks/{objetivo}/deliveries", headers=atacante.headers
        )
        ping = await client.post(
            f"/api/v1/webhooks/{objetivo}/ping", headers=atacante.headers
        )

    assert historial.status_code == 404
    assert ping.status_code == 404


@pytest.mark.asyncio
async def test_borrar_un_endpoint_arrastra_su_historial(
    integration_session: AsyncSession,
) -> None:
    """El historial de una operación concreta desaparece con ella.

    Lo que **no** desaparece es el rastro forense: un endpoint auto-desactivado que se
    borra deja su asiento en `audit_log`, y por eso un endpoint que ya no existe puede
    seguir explicándose.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="wh5")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )
        objetivo = creado.json()["id"]
        with patch(
            "backend.apps.webhooks.dispatcher._default_client_factory",
            side_effect=lambda: httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
                follow_redirects=False,
            ),
        ):
            await client.post(
                f"/api/v1/webhooks/{objetivo}/ping", headers=tenant.headers
            )
        borrado = await client.delete(
            f"/api/v1/webhooks/{objetivo}", headers=tenant.headers
        )

    assert borrado.status_code == 204
    assert (
        await integration_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.endpoint_id == objetivo)
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_el_catalogo_de_eventos_se_pide_al_servidor(
    integration_session: AsyncSession,
) -> None:
    """El panel no lleva los eventos escritos: se los piden.

    Un catálogo duplicado en el cliente es un segundo sitio donde un evento puede existir
    sin que el backend lo emita, y el síntoma sería una casilla que se marca y nunca suena.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/webhooks/events", headers=tenant.headers)
        # Y todo lo que ofrece el catálogo se puede usar en el alta. Va **dentro** del
        # `async with`: fuera, el cliente ya está cerrado y la petición falla con un error
        # que no dice nada del catálogo.
        planos = [
            e["event_type"] for g in response.json()["groups"] for e in g["events"]
        ]
        alta = await client.post(
            "/api/v1/webhooks",
            json=_crear(tenant, event_types=planos),
            headers=tenant.headers,
        )

    assert response.status_code == 200
    cuerpo = response.json()
    assert cuerpo["total"] == len(planos)
    assert len(planos) == len(set(planos))
    assert "pentest.completed" in planos
    assert "ping" in planos
    assert alta.status_code == 201, alta.text
    assert sorted(alta.json()["event_types"]) == sorted(planos)


@pytest.mark.asyncio
async def test_el_secreto_de_un_endpoint_no_se_regenera_solo(
    integration_session: AsyncSession,
) -> None:
    """Cambiar la URL no rota el secreto, y eso tiene que ser explícito.

    Quien controle la URL antigua sigue recibiendo. Rotar es una operación aparte que este
    bloque no expone, y la prueba deja constancia para que no se lea como un descuido.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        creado = await client.post(
            "/api/v1/webhooks", json=_crear(tenant), headers=tenant.headers
        )
        objetivo = creado.json()["id"]
        await client.patch(
            f"/api/v1/webhooks/{objetivo}",
            json={"url": "https://otro.example.com/entrada"},
            headers=tenant.headers,
        )
        despues = await client.get("/api/v1/webhooks", headers=tenant.headers)

    fila = despues.json()["items"][0]
    assert fila["url"] == "https://otro.example.com/entrada"
    assert "signing_secret" not in fila
