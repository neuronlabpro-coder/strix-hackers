"""Pruebas de Stripe: firma del webhook, idempotencia y recarga de créditos.

Ninguna prueba toca la red. `stripe` se sustituye por dobles en todas las rutas
que llaman a la API de Stripe, y la firma del webhook se firma con el secreto
local para que la verificación sea real y no un `always True`.
"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import credit_balance_of
from backend.apps.billing.stripe import StripeSDKClient, StripeWebhookSignatureError
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.config import settings
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "whsec_test_secret_para_firma_local"
CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_test_12345"


async def _tenant(
    session: AsyncSession, *, balance: str = "0"
) -> tuple[Organization, User, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Billing {suffix}", slug=f"billing-{suffix}")
    user = User(
        email=f"billing-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Billing User",
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
        organization.credit_balance = Decimal(balance)
    await session.commit()
    return organization, user, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _signed_payload(
    event: dict[str, object], timestamp: int | None = None
) -> tuple[bytes, dict[str, str]]:
    """Construye el cuerpo exacto y su cabecera `Stripe-Signature`."""

    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + body
    signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    headers = {
        "Stripe-Signature": f"t={ts},v1={signature}",
        "Content-Type": "application/json",
    }
    return body, headers


def _checkout_event(
    *, event_id: str, organization_id: uuid.UUID, credits: int, session_id: str
) -> dict[str, object]:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": session_id,
                "object": "checkout.session",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": credits * 100,
                "currency": "usd",
                "client_reference_id": str(organization_id),
                "metadata": {"organization_id": str(organization_id), "credits": str(credits)},
            }
        },
    }


def test_real_verifier_accepts_a_correctly_signed_payload() -> None:
    """La verificación de firma se prueba con el SDK real, no con un doble.

    Un doble que devuelve `True` no demuestra nada: probaría que el router llama al
    verificador, no que el verificador rechaza un payload manipulado.
    """

    event = _checkout_event(
        event_id="evt_real_1", organization_id=uuid.uuid4(), credits=500, session_id="cs_real"
    )
    body, headers = _signed_payload(event)
    client = _real_client()

    verified = asyncio.run(client.verify_webhook(body, headers["Stripe-Signature"]))

    assert verified["id"] == "evt_real_1"
    assert verified["type"] == "checkout.session.completed"


def test_real_verifier_rejects_a_tampered_payload() -> None:
    event = _checkout_event(
        event_id="evt_real_2", organization_id=uuid.uuid4(), credits=500, session_id="cs_real"
    )
    body, headers = _signed_payload(event)
    tampered = body.replace(b'"credits":"500"', b'"credits": "500000"')
    client = _real_client()

    with pytest.raises(StripeWebhookSignatureError):
        asyncio.run(client.verify_webhook(tampered, headers["Stripe-Signature"]))


def test_real_verifier_rejects_a_payload_signed_with_another_secret() -> None:
    event = _checkout_event(
        event_id="evt_real_3", organization_id=uuid.uuid4(), credits=500, session_id="cs_real"
    )
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    signature = hmac.new(
        b"whsec_secreto_del_atacante", f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    client = _real_client()

    with pytest.raises(StripeWebhookSignatureError):
        asyncio.run(client.verify_webhook(body, f"t={ts},v1={signature}"))


def _real_client() -> StripeSDKClient:
    """Cliente real con el secreto de pruebas.

    El secreto se pasa al constructor, así que no hace falta tocar la
    configuración global (que además es `frozen=True`).
    """

    return StripeSDKClient("sk_test_no_se_usa_para_verificar_firmas", WEBHOOK_SECRET)


@pytest.mark.asyncio
async def test_webhook_rejects_payload_with_invalid_signature(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="0")
    organization_id = organization.id
    event = _checkout_event(
        event_id="evt_bad",
        organization_id=organization_id,
        credits=500,
        session_id="cs_bad",
    )
    body, headers = _signed_payload(event)
    # Firma válida, cuerpo manipulado: es el ataque que el verificador debe cortar.
    tampered = body.replace(b'"credits":"500"', b'"credits": "500000"')
    transport = ASGITransport(app=app)

    with patch("backend.apps.billing.stripe.settings", SimpleNamespace(
        stripe_secret_key=SecretStr("sk_test_no_se_usa"),
        stripe_webhook_secret=SecretStr(WEBHOOK_SECRET),
    )):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/billing/webhooks",
                content=tampered,
                headers=headers,
            )

    assert response.status_code == 400
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization_id)
        )
    ).scalars().all()
    assert entries == []


@pytest.mark.asyncio
async def test_webhook_credits_the_organization_once(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="10")
    event = _checkout_event(
        event_id="evt_ok_1",
        organization_id=organization.id,
        credits=500,
        session_id="cs_test_ok_1",
    )
    body, headers = _signed_payload(event)
    transport = ASGITransport(app=app)
    stripe_mock = MagicMock()
    stripe_mock.verify_webhook = AsyncMock(return_value=event)

    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=stripe_mock
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert response.json()["duplicate"] is False
    assert response.json()["credits_granted"] == 500

    assert await credit_balance_of(integration_session, organization.id) == Decimal("510")
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization.id)
        )
    ).scalars().all()
    purchases = [entry for entry in entries if entry.reason == LedgerReasonEnum.STRIPE_PURCHASE]
    assert len(purchases) == 1
    assert purchases[0].amount_delta == Decimal("500")
    assert purchases[0].reference_id == "cs_test_ok_1"


@pytest.mark.asyncio
async def test_webhook_is_idempotent_for_repeated_delivery(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="0")
    event = _checkout_event(
        event_id="evt_dup_1",
        organization_id=organization.id,
        credits=250,
        session_id="cs_test_dup_1",
    )
    body, headers = _signed_payload(event)
    transport = ASGITransport(app=app)
    stripe_mock = MagicMock()
    stripe_mock.verify_webhook = AsyncMock(return_value=event)

    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=stripe_mock
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)
            second = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)
            third = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert third.json()["duplicate"] is True
    assert second.json()["credits_granted"] == 0
    assert await credit_balance_of(integration_session, organization.id) == Decimal("250")
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization.id)
        )
    ).scalars().all()
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_webhook_ignores_unhandled_event_types(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="0")
    event = {
        "id": "evt_unhandled",
        "type": "customer.subscription.deleted",
        "created": int(time.time()),
        "data": {"object": {"id": "cus_1", "object": "customer"}},
    }
    body, headers = _signed_payload(event)
    transport = ASGITransport(app=app)
    stripe_mock = MagicMock()
    stripe_mock.verify_webhook = AsyncMock(return_value=event)

    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=stripe_mock
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization.id)
        )
    ).scalars().all()
    assert entries == []


@pytest.mark.asyncio
async def test_webhook_rejects_session_without_organization_metadata(
    integration_session: AsyncSession,
) -> None:
    """Stripe puede llamar al webhook con sesiones creadas fuera de la plataforma."""

    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="0")
    event = {
        "id": "evt_no_org",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": "cs_foreign",
                "object": "checkout.session",
                "payment_status": "paid",
                "metadata": {},
            }
        },
    }
    body, headers = _signed_payload(event)
    transport = ASGITransport(app=app)
    stripe_mock = MagicMock()
    stripe_mock.verify_webhook = AsyncMock(return_value=event)

    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=stripe_mock
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)

    assert response.status_code == 422
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization.id)
        )
    ).scalars().all()
    assert entries == []


@pytest.mark.asyncio
async def test_webhook_rejects_unpaid_session(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="0")
    event = _checkout_event(
        event_id="evt_unpaid",
        organization_id=organization.id,
        credits=999,
        session_id="cs_unpaid",
    )
    event["data"]["object"]["payment_status"] = "unpaid"  # type: ignore[index]
    body, headers = _signed_payload(event)
    transport = ASGITransport(app=app)
    stripe_mock = MagicMock()
    stripe_mock.verify_webhook = AsyncMock(return_value=event)

    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=stripe_mock
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/billing/webhooks", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization.id)
        )
    ).scalars().all()
    assert entries == []


@pytest.mark.asyncio
async def test_checkout_session_creation_is_tenant_bound(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session, balance="0")
    transport = ASGITransport(app=app)
    stripe_mock = MagicMock()
    stripe_mock.create_checkout_session = AsyncMock(
        return_value={
            "id": "cs_test_created",
            "url": CHECKOUT_URL,
        }
    )

    with patch(
        "backend.apps.billing.router.get_stripe_client", return_value=stripe_mock
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/billing/checkout-session",
                json={"credits": 1500, "success_url": f"{settings.frontend_base_url}/billing?ok=1",
                      "cancel_url": f"{settings.frontend_base_url}/billing?cancel=1"},
                headers=headers,
            )

    assert response.status_code == 201
    payload = response.json()
    assert payload["session_id"] == "cs_test_created"
    assert payload["url"] == CHECKOUT_URL

    call = stripe_mock.create_checkout_session.await_args
    assert call.kwargs["metadata"]["organization_id"] == str(organization.id)
    assert call.kwargs["metadata"]["credits"] == "1500"
    # La organización viaja en metadata firmada por Stripe, nunca en la URL: una
    # URL de Checkout es pública y se puede compartir.
    assert str(organization.id) not in CHECKOUT_URL
    assert str(organization.id) not in response.text.replace(str(organization.id), "")


@pytest.mark.asyncio
async def test_checkout_session_rejects_success_url_from_another_origin(
    integration_session: AsyncSession,
) -> None:
    """Un `success_url` ajeno convierte el checkout en un redirect abierto."""

    assert integration_session is not None
    _organization, _user, headers = await _tenant(integration_session, balance="0")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/billing/checkout-session",
            json={
                "credits": 100,
                "success_url": "https://phishing.example.com/steal",
                "cancel_url": f"{settings.frontend_base_url}/billing",
            },
            headers=headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_checkout_session_rejects_unknown_credit_pack(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, _user, headers = await _tenant(integration_session, balance="0")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/billing/checkout-session",
            json={
                "credits": 7,
                "success_url": f"{settings.frontend_base_url}/billing",
                "cancel_url": f"{settings.frontend_base_url}/billing",
            },
            headers=headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_checkout_requires_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/billing/checkout-session",
            json={
                "credits": 100,
                "success_url": f"{settings.frontend_base_url}/billing",
                "cancel_url": f"{settings.frontend_base_url}/billing",
            },
        )

    assert response.status_code == 401
