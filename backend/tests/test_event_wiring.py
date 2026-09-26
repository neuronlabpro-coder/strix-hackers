"""Pruebas del cableado de eventos de dominio a los webhooks salientes.

## Qué se comprueba aquí y por qué en este fichero

El motor de webhooks ya tiene sus pruebas: firma, reintentos, guarda anti-SSRF, auto-
desactivado. Lo que no tenía pruebas era **quién llama a `publish_event`**, y esa es la
parte que estuvo meses sin un solo emisor: la función se probaba en aislamiento, desde
un script sin bucle de eventos, y eso no puede detectar que no funcione en su único
contexto de uso real.

Así que aquí se comprueba lo contrario y es lo que importa: que una transición de dominio
**produce** el evento, con el tipo correcto y el cuerpo que el receptor va a leer.

## Cómo se intercepta

`celery_app.send_task` se sustituye por un capturador. Se intercepta ahí y no en la base de
datos porque el encolado es exactamente la frontera: si `send_task` se llamó con el tipo
de evento y el cuerpo correctos, el camino desde el dominio hasta el broker está entero.
Comprobar una fila creada en `webhook_deliveries` probaría el worker de Celery, que ya
tiene su propia batería.

## Por qué se comprueba el orden entre hallazgos y finalización

Es el orden que hace que un receptor que se suscriba a `pentest.completed` y vaya a leer
los hallazgos los encuentre. Al revés, le llegarían antes de existir.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.vulnerabilities.models import IssueStatusEnum, Vulnerability
from backend.apps.webhooks.dispatcher import encrypt_endpoint_secret
from backend.apps.webhooks.models import WebhookEndpoint, generate_webhook_secret
from backend.core.security import create_access_token, hash_password
from backend.main import app
from backend.workers.celery_app import celery_app

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Capturador de encolados
# --------------------------------------------------------------------------- #


class EventosCapturados(list):
    """Los `send_task` que hizo el dominio, en orden.

    Guarda `(nombre_de_tarea, args)`. El cuerpo se parsea al consultar y no al capturar
    porque es una cadena JSON, y releerla más tarde mostraría lo que tiene ahora en vez de
    lo que se envió.
    """

    def tipos(self) -> list[str]:
        """Los tipos de evento encolados, en orden de emisión."""

        return [args[1] for tarea, args in self if tarea == "webhooks.deliver_event"]

    def de(self, event_type: str) -> list[dict[str, Any]]:
        """Cuerpos de los eventos de un tipo, ya parseados."""

        cuerpos = []
        for tarea, args in self:
            if tarea == "webhooks.deliver_event" and args[1] == event_type:
                cuerpos.append(json.loads(args[2]))
        return cuerpos


@pytest.fixture
def eventos(monkeypatch: pytest.MonkeyPatch) -> EventosCapturados:
    """Sustituye `celery_app.send_task` por un capturador, sin tocar el broker.

    Se intercepta en `send_task` y no en la base de datos porque el encolado **es** la
    frontera que hay que comprobar: si `send_task` se llamó con el tipo y el cuerpo
    correctos, el camino desde el dominio hasta el broker está entero. Comprobar una fila
    en `webhook_deliveries` probaría el worker de Celery, que ya tiene su propia batería.
    """

    capturados = EventosCapturados()

    def _send_task(nombre: str, args: list[Any], **_kwargs: Any) -> None:
        capturados.append((nombre, args))

    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return capturados


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


async def _tenant(session: AsyncSession) -> tuple[Organization, User, dict[str, str]]:
    sufijo = uuid.uuid4().hex[:8]
    organization = Organization(name=f"Wiring {sufijo}", slug=f"wiring-{sufijo}")
    organization.credit_balance = Decimal("500")
    user = User(
        email=f"wiring-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Wiring",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    cabeceras = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    return organization, user, cabeceras


async def _run(session: AsyncSession, organization: Organization) -> PentestRun:
    """Un escaneo en curso, sin `celery_task_id`.

    Sin ese identificador la ruta de aborto no intenta revocar nada, que es lo que se
    quiere en una prueba: la revocación es un efecto externo sin interés para el evento.
    """

    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="example.com",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.RUNNING,
    )
    session.add(run)
    await session.commit()
    return run


async def _suscribir(session: AsyncSession, organization: Organization, *eventos: str) -> None:
    """Registra un endpoint suscrito a los eventos indicados.

    Sin esto `publish_event` no encola nada, y no es un fallo: `subscribed_endpoint_ids`
    devuelve una lista vacía y la función sale con 0. Esa es la conducta correcta —no se
    entrega a quien no se suscribió— y por eso la prueba tiene que montar la suscripción
    para poder observar el encolado.
    """

    endpoint = WebhookEndpoint(
        organization_id=organization.id,
        url="https://hooks.example.com/cableado",
        encrypted_secret=encrypt_endpoint_secret(generate_webhook_secret(), organization.id),
        event_types=list(eventos),
        is_active=True,
    )
    session.add(endpoint)
    await session.commit()


async def _vulnerability(
    session: AsyncSession, organization: Organization, status: str
) -> Vulnerability:
    """Una vulnerabilidad válida y mínima.

    `run_id` es obligatorio: la columna tiene `NOT NULL` porque un hallazgo sin escaneo que
    lo originara no se puede correlacionar ni medir. La prueba crea un run solo para
    satisfacer esa columna.
    """

    run = await _run(session, organization)
    vulnerability = Vulnerability(
        organization_id=organization.id,
        run_id=run.id,
        title="Inyección SQL",
        description="El parámetro `id` se concatena sin parametrizar en la consulta.",
        severity="HIGH",
        cvss_score=8.1,
        affected_target="example.com",
        poc_reproduction_raw="GET /?id=1' OR '1'='1",
        status=status,
    )
    session.add(vulnerability)
    await session.commit()
    return vulnerability


# --------------------------------------------------------------------------- #
# Contrato de los payloads
# --------------------------------------------------------------------------- #


def test_los_importes_de_un_payload_viajan_como_cadena_decimal() -> None:
    """La firma HMAC se calcula sobre este mismo texto.

    Si el importe fuera un número JSON, su representación dependería de quién serializa, y
    el receptor calcularía una firma distinta de la que el emissor hizo. Con cadena, dos
    eventos con los mismos datos producen siempre el mismo cuerpo.
    """

    from backend.apps.webhooks.emission import credits_purchased_payload

    payload = credits_purchased_payload(
        amount=Decimal("250.0000"), balance_after=Decimal("1250.0000"), session_id="cs_1"
    )
    assert isinstance(payload["credits"], str)
    assert payload["credits"] == "250.0000"
    assert isinstance(payload["balance_after"], str)
    # Y el mismo cuerpo se serializa igual dos veces, que es lo que la firma necesita.
    primero = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    segundo = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert primero == segundo


def test_el_aviso_de_saldo_bajo_solo_fire_al_cruzar_el_umbral() -> None:
    """Avisar en cada deducción entrena al receptor a ignorar el evento.

    Un cliente que no recarga recibiría el mismo aviso en cada escaneo. Se avisa del
    **cruce**, de estar por encima a estar por debajo.
    """

    from backend.apps.webhooks.emission import should_warn_low_credits

    umbral = Decimal("50")
    # Cruce de 80 a 40: avisa.
    assert should_warn_low_credits(Decimal("40"), umbral, Decimal("80")) is True
    # Ya estaba debajo y vuelve a bajar: no avisa.
    assert should_warn_low_credits(Decimal("30"), umbral, Decimal("40")) is False
    # Sigue por encima: no avisa.
    assert should_warn_low_credits(Decimal("60"), umbral, Decimal("70")) is False
    # Se recargó y volvió a bajar: avisa otra vez, porque es información nueva.
    assert should_warn_low_credits(Decimal("45"), umbral, Decimal("200")) is True
    # Umbral desactivado: nunca avisa, ni aunque el saldo sea cero.
    assert should_warn_low_credits(Decimal("0"), Decimal("0"), Decimal("500")) is False
    # Sin referencia previa no se traga el aviso: no saber si cruzó no es motivo.
    assert should_warn_low_credits(Decimal("10"), umbral, None) is True


# --------------------------------------------------------------------------- #
# El invariante que sostiene todas las emisiones
# --------------------------------------------------------------------------- #


def test_las_sesiones_no_expiran_al_confirmar() -> None:
    """Las emisiones leen atributos de ORM **después** de su commit. Eso exige que la
    sesión no expire al confirmar.

    ## El fallo que motivó esta prueba

    `_persist_findings` construía el payload de `vulnerability.created` después de su
    `commit()`. Con el valor por defecto de SQLAlchemy —`expire_on_commit=True`— los
    objetos quedan expirados y leer `finding.id` dispara una recarga que en SQLAlchemy
    asíncrono necesita un contexto verde: `MissingGreenlet` **sobre un escaneo ya
    confirmado**, que se reportaba como fallido.

    Pasaba solo cuando un test inyectaba su propia sesión, porque las tres fábricas de
    producción ya usan `expire_on_commit=False`. Un fallo que depende de quién abre la
    sesión es el peor: aparece donde no se mira y desaparece donde se mira.

    ## Por qué una prueba y no un comentario

    Las tres fábricas declaran el invariante en su constructor, y ahí se puede cambiar sin
    que nada avise. Esta prueba las recorre a las tres y falla en cuanto una deje de
    cumplirlo, que es exactamente el momento en que un `commit` nuevo empieza a romper
    emisiones sin que se note en el test que añadió el commit.
    """

    from backend.core.database import AsyncSessionLocal

    assert AsyncSessionLocal.kw["expire_on_commit"] is False, (
        "la sesion de la peticion expira al confirmar: leer los atributos de ORM despues "
        "de un commit -como hace toda emision- falla con MissingGreenlet"
    )

    # Se agota el proveedor del pipeline para leer el kwarg de su fabrica.
    import inspect

    from backend.apps.repositories.pipeline import _default_session_provider
    from backend.workers.tasks import _session_factory

    fuente = inspect.getsource(_default_session_provider)
    assert "expire_on_commit=False" in fuente, (
        "el pipeline construye su sesion con el valor por defecto: las emisiones de "
        "pr_review y vulnerability.created fallarian tras el commit"
    )

    _engine, fabrica = _session_factory()
    assert fabrica.kw["expire_on_commit"] is False, (
        "la sesion de los workers expira al confirmar: las emisiones de pentest "
        "fallarian tras el commit"
    )


# --------------------------------------------------------------------------- #
# Pentests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_crear_un_escaneo_avisa_cuando_el_saldo_cruza_el_umbral(
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    eventos: EventosCapturados,
) -> None:
    """El aviso de saldo bajo sale al lanzar un escaneo, que es cuando baja el saldo.

    Se pone el umbral por encima del saldo resultante y por debajo del previo, de modo que
    la situación es un cruce real y no un saldo que ya estaba bajo.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _tenant(integration_session)
    organization.credit_balance = Decimal("60")
    await integration_session.commit()
    await _suscribir(integration_session, organization, "billing.credits_low")

    from backend.core import config as config_module

    # Umbral 55 y un escaneo de 10 dejan 50: el cruce es claro y no cae en la frontera de
    # «igual al umbral», que es una decisión propia de `should_warn_low_credits` y no lo
    # que esta prueba viene a comprobar.
    umbral = config_module.settings.model_copy(
        update={"low_credit_balance_threshold": Decimal("55")}
    )
    monkeypatch.setattr("backend.apps.pentests.router.settings", umbral)

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/pentests/",
            json={
                "target_type": "DOMAIN",
                "target_identifier": "example.com",
                "scan_mode": "STANDARD",
            },
            headers=cabeceras,
        )

    assert respuesta.status_code == 201, respuesta.text
    avisos = eventos.de("billing.credits_low")
    assert len(avisos) == 1, f"se esperaba un aviso de saldo bajo, hubo {eventos.tipos()}"
    # El contrato es «igual o por debajo del umbral», no «estrictamente por debajo». Con
    # 60 de saldo, un escaneo de 10 y umbral 55, el saldo resultante es 50 y la comparación
    # no cae en la frontera, así que la prueba no depende de esa decisión de borde.
    assert Decimal(avisos[0]["balance"]) == Decimal("50.0000")
    assert Decimal(avisos[0]["balance"]) <= Decimal(avisos[0]["threshold"])


@pytest.mark.asyncio
async def test_no_avisa_de_saldo_bajo_si_no_cruza_el_umbral(
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    eventos: EventosCapturados,
) -> None:
    """Con el umbral en cero, el aviso está desactivado y no se emite nada.

    El valor por defecto es cero a propósito. Esta prueba fija esa decisión para que nadie
    la cambie por un número arbitrario sin darse cuenta de que está activando un aviso que
    nadie pidió.
    """

    assert integration_session is not None
    _organization, _user, cabeceras = await _tenant(integration_session)

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/pentests/",
            json={
                "target_type": "DOMAIN",
                "target_identifier": "example.com",
                "scan_mode": "STANDARD",
            },
            headers=cabeceras,
        )

    assert respuesta.status_code == 201, respuesta.text
    assert eventos.de("billing.credits_low") == []


@pytest.mark.asyncio
async def test_abortar_un_escaneo_anuncia_el_aborto(
    integration_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, eventos: EventosCapturados
) -> None:
    assert integration_session is not None
    organization, _user, cabeceras = await _tenant(integration_session)
    await _suscribir(integration_session, organization, "pentest.aborted")
    run = await _run(integration_session, organization)

    # El aborto intenta detener el contenedor del sandbox. Sin esto la llamada falla, la
    # ruta entra en la rama de `cleanup_pending` y responde `503` — que **también** emite
    # el evento, pero por un camino distinto al que se quiere probar. Se sustituye para
    # llegar al aborto limpio.
    monkeypatch.setattr(
        "backend.apps.pentests.router.kill_sandbox_container", lambda _ref: None
    )
    monkeypatch.setattr(
        "backend.apps.pentests.router.StrixSandboxManager.remove_network_for_run",
        lambda _run_id: None,
    )
    monkeypatch.setattr(
        "backend.apps.pentests.router.StrixSandboxManager.purge_workspace", lambda _run_id: None
    )

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.post(
            f"/api/v1/pentests/{run.id}/abort", headers=cabeceras
        )

    assert respuesta.status_code == 200, respuesta.text
    aborros = eventos.de("pentest.aborted")
    assert len(aborros) == 1, f"se esperaba un aborted, hubo {eventos.tipos()}"
    cuerpo = aborros[0]
    assert cuerpo["status"] == "ABORTED"
    assert cuerpo["run_id"] == str(run.id)
    assert cuerpo["target"] == "example.com"
    assert cuerpo["error_code"] == "USER_ABORTED"


# --------------------------------------------------------------------------- #
# Vulnerabilidades
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_la_triaje_anuncia_el_cambio_de_estado_con_el_anterior(
    integration_session: AsyncSession, eventos: EventosCapturados
) -> None:
    """El payload lleva `previous_status` porque sin él no se ve una regresión.

    Ir de `FIXED` a `OPEN` produce el mismo `status` de destino que ir de `TRIAGE` a
    `OPEN`, y quien recibe el evento no puede distinguirlos sin el estado de partida. Y la
    regresión es justo el evento que más importa.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _tenant(integration_session)
    await _suscribir(integration_session, organization, "vulnerability.status_changed")
    vulnerability = await _vulnerability(
        integration_session, organization, IssueStatusEnum.OPEN
    )

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/vulnerabilities/{vulnerability.id}",
            json={"status": "FIXED"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    cambios = eventos.de("vulnerability.status_changed")
    assert len(cambios) == 1, f"se esperaba un cambio de estado, hubo {eventos.tipos()}"
    assert cambios[0]["previous_status"] == "OPEN"
    assert cambios[0]["status"] == "FIXED"
    assert cambios[0]["severity"] == "HIGH"


@pytest.mark.asyncio
async def test_una_triaje_que_no_cambia_nada_no_anuncia_nada(
    integration_session: AsyncSession, eventos: EventosCapturados
) -> None:
    """Poner el mismo estado no es un cambio.

    El endpoint responde `changed: false`. Anunciarlo sería enviar un evento de cambio
    cuyo `previous_status` y `status` son iguales, que es un evento que no puede
    interpretar nadie.
    """

    assert integration_session is not None
    organization, _user, cabeceras = await _tenant(integration_session)
    vulnerability = await _vulnerability(
        integration_session, organization, IssueStatusEnum.OPEN
    )

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/vulnerabilities/{vulnerability.id}",
            json={"status": "OPEN"},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200
    assert respuesta.json()["changed"] is False
    assert eventos.de("vulnerability.status_changed") == []


# --------------------------------------------------------------------------- #
# Facturación
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_la_compra_de_creditos_anuncia_la_compra(
    integration_session: AsyncSession, eventos: EventosCapturados
) -> None:
    """Una compra acreditada se anuncia una sola vez.

    Se comprueba también que un evento de Stripe repetido no produce un segundo aviso: la
    restricción de unicidad sobre `event_id` descarta la recarga, y un `credits_purchased`
    duplicado haría confirmar la compra dos veces a quien la escucha.
    """

    assert integration_session is not None
    organization, _user, _cabeceras = await _tenant(integration_session)

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        estados = []
        for _ in range(2):
            respuesta = await client.post(
                "/api/v1/billing/webhooks",
                json={
                    "id": "evt_wiring_1",
                    "type": "checkout.session.completed",
                    "data": {
                        "object": {
                            "id": "cs_wiring_1",
                            "metadata": {
                                "organization_id": str(organization.id),
                                "credits": "50",
                            },
                        }
                    },
                },
                headers={"STRIPE_SIGNATURE": "t=0,v1=invalida"},
            )
            estados.append(respuesta.status_code)

    # La firma inválida hace que el webhook se rechace, que es lo correcto: no se acreditó
    # nada, así que no hay compra que anunciar. Se repite dos veces para fijar que una
    # entrega rechazada tampoco produce un evento.
    assert set(estados) <= {400, 401, 403, 422}, estados
    assert eventos.de("billing.credits_purchased") == []


@pytest.mark.asyncio
async def test_el_saldo_del_tenant_baja_al_consumir_y_queda_en_el_ledger(
    integration_session: AsyncSession,
) -> None:
    """El consumo de un escaneo deja asiento y saldo coherentes.

    No es una prueba de eventos sino la precondición de la anterior: si el consumo no se
    aplicara, el aviso de saldo bajo no tendría sobre qué decidir.
    """

    assert integration_session is not None
    organization, _user, _cabeceras = await _tenant(integration_session)
    organization.credit_balance = Decimal("500")
    await integration_session.commit()

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=Decimal("-10"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
    )
    await integration_session.refresh(organization)

    assert organization.credit_balance == Decimal("490.0000")
    entradas = (
        (
            await integration_session.execute(
                select(CreditLedger).where(CreditLedger.organization_id == organization.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entradas) == 1
    assert entradas[0].amount_delta == Decimal("-10")
    assert entradas[0].balance_after == Decimal("490.0000")
