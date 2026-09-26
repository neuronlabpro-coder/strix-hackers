"""Prueba de extremo a extremo del cobro: evento firmado → ledger → saldo.

Esta capa cubre lo que `test_billing_stripe.py` no cubre: aquel verifica que el
verificador de firma *rechaza* un payload manipulado, y este verifica que el endpoint
completo, con su firma válida, **acredita los créditos exactamente una vez**.

El payload se firma con el secreto real y se entrega al endpoint real. Lo que no se
ejercita es la entrega por parte de Stripe, que necesita su página de Checkout
alojada y por tanto un navegador; esa parte se comprobó aparte con un evento real de
`stripe trigger`, que llegó al backend y fue rechazado con `422` por no identificar
ninguna organización. Sustituir la entrega de Stripe por una firma local es lo que
permite que esta prueba corra en CI sin credenciales y sin red.

## Por qué la idempotencia se prueba aquí y no con un doble

La garantía que importa es que dos entregas del mismo `event_id` acrediten una sola
vez. Eso depende de la restricción única de `stripe_events.event_id`, no de una
comprobación previa en Python, y solo se puede demostrar empujando dos veces el mismo
evento contra la base de datos real. Un doble de la sesión lo aceptaría todo.
"""

import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum, StripeEvent
from backend.apps.billing.stripe import StripeSDKClient
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

CREDITS = 500
# El mismo secreto que usaría `stripe listen`. La prueba lo declara en vez de leerlo
# de `settings` a propósito: si leyera el valor real, la prueba dejaría de ser
# autosuficiente el día que `.env` no lo tenga, que es justo cuando se ejecuta en CI.
WEBHOOK_SECRET = "whsec_test_e2e_00000000000000000000000000000000000000000000"


def _signature_header(body: bytes, secret: str, timestamp: int) -> str:
    """Reproduce el esquema de firma de Stripe: `t=<ts>,v1=<hmac>`."""

    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _checkout_completed_event(
    organization_id: uuid.UUID,
    *,
    event_id: str,
    session_id: str,
    credits: int = CREDITS,
    payment_status: str = "paid",
    amount_cents: int = 1900,
) -> dict[str, object]:
    """Evento con la misma forma que emite Stripe para un pago completado."""

    return {
        "id": event_id,
        "object": "event",
        "api_version": "2026-04-22.dahlia",
        "created": int(time.time()),
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "object": "checkout.session",
                "amount_total": amount_cents,
                "amount_subtotal": amount_cents,
                "currency": "usd",
                "customer": "cus_test_e2e",
                "customer_creation": "always",
                "livemode": False,
                "mode": "payment",
                "payment_status": payment_status,
                "status": "complete",
                "client_reference_id": str(organization_id),
                "metadata": {
                    "organization_id": str(organization_id),
                    "credits": str(credits),
                },
            }
        },
    }


async def _tenant(session: AsyncSession, *, balance: str = "0") -> tuple[Organization, dict]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Cobro {suffix}", slug=f"cobro-{suffix}")
    user = User(
        email=f"cobro-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsaEnEstaPrueba"),
        full_name="Cobro User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    if Decimal(balance) > 0:
        session.add(
            CreditLedger(
                organization_id=organization.id,
                amount_delta=Decimal(balance),
                balance_after=Decimal(balance),
                reason=LedgerReasonEnum.SIGNUP_BONUS,
            )
        )
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _post_event(event: dict[str, object]) -> Response:
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    header = _signature_header(body, WEBHOOK_SECRET, int(time.time()))
    transport = ASGITransport(app=app)
    # El router importa `get_stripe_client` directamente, así que el punto de corte es
    # el módulo del router y no el de origen. Se sustituye por un cliente real
    # (`StripeSDKClient`) con el secreto de la prueba: la verificación de firma sigue
    # siendo la del SDK y el HMAC es el auténtico, lo único que se controla es de qué
    # secreto se firman los eventos. Así la prueba es autosuficiente en CI, donde no
    # hay credenciales de Stripe.
    client_sdk = StripeSDKClient("sk_test_no_se_usa", WEBHOOK_SECRET)
    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=client_sdk
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/v1/billing/webhooks",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": header,
                },
            )


@pytest.mark.asyncio
async def test_paid_checkout_credits_the_organization_once(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _headers = await _tenant(integration_session, balance="100")
    before = organization.credit_balance
    event = _checkout_completed_event(
        organization.id, event_id="evt_e2e_paid_1", session_id="cs_test_e2e_paid_1"
    )

    response = await _post_event(event)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "processed"
    assert payload["duplicate"] is False
    assert payload["event_id"] == "evt_e2e_paid_1"

    reloaded = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert reloaded.credit_balance == before + Decimal(CREDITS)

    entries = (
        await integration_session.execute(
            select(CreditLedger).where(
                CreditLedger.organization_id == organization.id,
                CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE,
            )
        )
    ).scalars().all()
    assert len(entries) == 1
    assert entries[0].amount_delta == Decimal(CREDITS)
    # El asiento apunta a la sesión de Stripe, que es como se reconcilia un cobro.
    assert entries[0].reference_id == "cs_test_e2e_paid_1"
    assert entries[0].balance_after == before + Decimal(CREDITS)


@pytest.mark.asyncio
async def test_redelivery_of_the_same_event_does_not_credit_twice(
    integration_session: AsyncSession,
) -> None:
    """Reentrega de Stripe: mismo `event_id`, mismo cuerpo, cero abonos nuevos.

    Stripe reintenta un webhook cuando la respuesta no es 2xx o cuando hay un fallo de
    red, y no es raro ver dos entregas del mismo evento. La garantía la da la
    restricción única de `stripe_events.event_id`, no una comprobación previa.
    """

    assert integration_session is not None
    organization, _headers = await _tenant(integration_session, balance="100")
    before = organization.credit_balance
    event = _checkout_completed_event(
        organization.id, event_id="evt_e2e_dupe", session_id="cs_test_e2e_dupe"
    )

    first = await _post_event(event)
    second = await _post_event(event)
    third = await _post_event(event)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert third.json()["duplicate"] is True

    reloaded = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert reloaded.credit_balance == before + Decimal(CREDITS)

    purchases = (
        await integration_session.execute(
            select(func.count(CreditLedger.id)).where(
                CreditLedger.organization_id == organization.id,
                CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE,
            )
        )
    ).scalar_one()
    assert purchases == 1
    events = (
        await integration_session.execute(
            select(func.count(StripeEvent.id)).where(
                StripeEvent.event_id == "evt_e2e_dupe"
            )
        )
    ).scalar_one()
    assert events == 1


@pytest.mark.asyncio
async def test_unpaid_checkout_is_acknowledged_but_credits_nothing(
    integration_session: AsyncSession,
) -> None:
    """Una sesión sin pagar se acusa con `200` y no mueve el saldo.

    Se responde `200` y no `4xx` a propósito: el pago puede completarse más tarde y
    llegará su propio evento. Devolver un error haría que Stripe reintentara un
    evento que no va a cambiar nunca.
    """

    assert integration_session is not None
    organization, _headers = await _tenant(integration_session, balance="100")
    before = organization.credit_balance
    event = _checkout_completed_event(
        organization.id,
        event_id="evt_e2e_unpaid",
        session_id="cs_test_e2e_unpaid",
        payment_status="unpaid",
    )

    response = await _post_event(event)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    reloaded = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert reloaded.credit_balance == before
    purchases = (
        await integration_session.execute(
            select(func.count(CreditLedger.id)).where(
                CreditLedger.organization_id == organization.id,
                CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE,
            )
        )
    ).scalar_one()
    assert purchases == 0


@pytest.mark.asyncio
async def test_event_without_organization_is_rejected_and_credits_nothing(
    integration_session: AsyncSession,
) -> None:
    """Un evento sin tenant identificable no mueve el saldo de nadie.

    Es lo que devuelve un `stripe trigger`: su fixture no lleva `metadata`, y el
    endpoint responde `422` en lugar de acreditar a una organización arbitraria.
    """

    assert integration_session is not None
    organization, _headers = await _tenant(integration_session, balance="100")
    before = organization.credit_balance
    orphan = uuid.uuid4()
    event = _checkout_completed_event(
        orphan, event_id="evt_e2e_orphan", session_id="cs_test_e2e_orphan"
    )

    response = await _post_event(event)

    assert response.status_code == 422
    reloaded = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert reloaded.credit_balance == before


@pytest.mark.asyncio
async def test_tampered_body_is_rejected_before_any_credit(
    integration_session: AsyncSession,
) -> None:
    """La firma cubre el cuerpo: alterar el importe anula la firma y no acredita nada."""

    assert integration_session is not None
    organization, _headers = await _tenant(integration_session, balance="100")
    before = organization.credit_balance
    event = _checkout_completed_event(
        organization.id, event_id="evt_e2e_tamper", session_id="cs_test_e2e_tamper"
    )
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    header = _signature_header(body, WEBHOOK_SECRET, int(time.time()))

    # Se envía el mismo cuerpo pero con el importe triplicado.
    tampered = json.loads(body)
    tampered["data"]["object"]["amount_total"] = 100000
    tampered_body = json.dumps(tampered, separators=(",", ":")).encode("utf-8")

    transport = ASGITransport(app=app)
    client_sdk = StripeSDKClient("sk_test_no_se_usa", WEBHOOK_SECRET)
    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=client_sdk
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/billing/webhooks",
                content=tampered_body,
                headers={"Content-Type": "application/json", "Stripe-Signature": header},
            )

    assert response.status_code == 400
    reloaded = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert reloaded.credit_balance == before
    events = (
        await integration_session.execute(
            select(func.count(StripeEvent.id)).where(
                StripeEvent.event_id == "evt_e2e_tamper"
            )
        )
    ).scalar_one()
    assert events == 0, "un evento con firma invalida llego a registrarse"
