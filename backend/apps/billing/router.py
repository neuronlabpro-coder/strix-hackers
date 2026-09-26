"""Endpoints de checkout de Stripe y webhook de recarga."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum, StripeEvent
from backend.apps.billing.schemas import (
    CREDIT_PACKS,
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    CreditLedgerEntryResponse,
    WebhookAckResponse,
)
from backend.apps.billing.service import apply_credit_delta
from backend.apps.billing.stripe import (
    StripeConfigurationError,
    StripeWebhookSignatureError,
    get_stripe_client,
)
from backend.apps.organizations.models import Organization, RoleEnum
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant
from backend.core.rate_limit import enforce_checkout_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
CheckoutRateLimit = Depends(enforce_checkout_rate_limit)

# Tope del cuerpo del webhook. Stripe envía payloads pequeños; un límite duro
# evita que alguien use el endpoint sin firma para gastar memoria.
MAX_WEBHOOK_BYTES = 256 * 1024

CHECKOUT_COMPLETED = "checkout.session.completed"


def _validate_redirect(url: str, field_name: str) -> None:
    """Impide open redirect: las URLs de retorno deben vivir en el frontend."""

    expected = urlsplit(settings.frontend_base_url)
    candidate = urlsplit(url)
    if candidate.scheme != expected.scheme or candidate.netloc != expected.netloc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} debe apuntar a {settings.frontend_base_url}",
        )


@router.post(
    "/checkout-session",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[CheckoutRateLimit],
)
async def create_checkout_session(
    payload: CheckoutSessionRequest,
    tenant: TenantDependency,
    _session: SessionDependency,
) -> CheckoutSessionResponse:
    """Crea una sesión de Stripe Checkout para un paquete de créditos."""

    if tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador para recargar créditos",
        )
    _validate_redirect(payload.success_url, "success_url")
    _validate_redirect(payload.cancel_url, "cancel_url")

    try:
        client = get_stripe_client()
    except StripeConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El cobro no está disponible: falta configurar Stripe en el servidor",
        ) from error

    amount = CREDIT_PACKS[payload.credits]
    # La organización viaja en metadata firmada por Stripe, nunca en la URL: la
    # URL de Checkout es pública y se puede compartir o interceptar.
    metadata = {
        "organization_id": str(tenant.organization.id),
        "credits": str(payload.credits),
    }
    try:
        checkout = await client.create_checkout_session(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(amount * 100),
                        "product_data": {"name": f"Fenix credits ({payload.credits})"},
                    },
                    "quantity": 1,
                }
            ],
            metadata=metadata,
            client_reference_id=str(tenant.organization.id),
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
        )
    except Exception as error:
        logger.exception("Fallo creando la sesión de checkout de Stripe")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stripe no pudo crear la sesión de pago",
        ) from error

    checkout_url = checkout.get("url")
    session_id = checkout.get("id")
    if not isinstance(checkout_url, str) or not isinstance(session_id, str):
        logger.error("Stripe devolvió una sesión de checkout sin id o sin url")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stripe devolvió una sesión de pago incompleta",
        )
    return CheckoutSessionResponse(
        session_id=session_id,
        url=checkout_url,
        credits=payload.credits,
        amount_usd=amount,
    )


def _extract_organization_id(data_object: dict[str, Any]) -> UUID | None:
    """Obtiene la organización de `metadata` y, si falta, de `client_reference_id`."""

    metadata = data_object.get("metadata")
    for candidate in (
        metadata.get("organization_id") if isinstance(metadata, dict) else None,
        data_object.get("client_reference_id"),
    ):
        if isinstance(candidate, str) and candidate:
            try:
                return UUID(candidate)
            except ValueError:
                logger.warning("Stripe entregó un organization_id con formato inválido")
                return None
    return None


def _extract_credits(data_object: dict[str, Any]) -> int:
    """Lee los créditos comprados. Un valor no entero o negativo invalida el evento."""

    metadata = data_object.get("metadata")
    raw = metadata.get("credits") if isinstance(metadata, dict) else None
    try:
        credits = int(str(raw))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La sesión de Stripe no indica cuántos créditos se compraron",
        ) from None
    if credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La sesión de Stripe declara una cantidad de créditos no válida",
        )
    return credits


@router.post("/webhooks", response_model=WebhookAckResponse)
async def receive_stripe_webhook(
    request: Request,
    session: SessionDependency,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> WebhookAckResponse:
    """Valida la firma de Stripe y recarga créditos de forma idempotente.

    El contrato con Stripe es que cualquier respuesta `2xx` detiene los
    reintentos. Por eso un evento que no nos aplica (`ignored`) devuelve `200` y
    solo un fallo transitorio devuelve `5xx`; un evento corrupto devuelve `4xx`,
    que Stripe tampoco reintenta pero que nos deja verlo en los logs.
    """

    if stripe_signature is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta la cabecera Stripe-Signature",
        )
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="El cuerpo del webhook supera el tamaño permitido",
        )

    try:
        client = get_stripe_client()
        event = await client.verify_webhook(body, stripe_signature)
    except StripeWebhookSignatureError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Firma de webhook inválida",
        ) from error
    except StripeConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El webhook no está configurado en el servidor",
        ) from error
    except Exception as error:
        logger.exception("Fallo verificando la firma del webhook de Stripe")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pudo verificar la firma del webhook",
        ) from error

    event_id = event.get("id") if isinstance(event, dict) else None
    event_type = event.get("type") if isinstance(event, dict) else None
    if not isinstance(event_id, str) or not isinstance(event_type, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El evento de Stripe no trae identificador ni tipo",
        )

    existing = await session.execute(
        select(StripeEvent).where(StripeEvent.event_id == event_id)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("Evento de Stripe duplicado ignorado: %s", event_id)
        return WebhookAckResponse(
            status="ignored", duplicate=True, event_id=event_id, event_type=event_type
        )

    if event_type != CHECKOUT_COMPLETED:
        return await _record_ignored(session, event_id, event_type)

    data = event.get("data")
    data_object = data.get("object") if isinstance(data, dict) else None
    if not isinstance(data_object, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El evento de checkout no incluye el objeto de la sesión",
        )

    # Una sesión no pagada no acredita nada. Se registra y se responde `200` para
    # que Stripe deje de reintentar: el pago puede completarse más tarde y
    # llegará su propio evento.
    if data_object.get("payment_status") != "paid":
        logger.info("Sesión de checkout sin pago (%s); no se acredita", event_id)
        return await _record_ignored(
            session, event_id, event_type, session_id=_as_str(data_object.get("id"))
        )

    organization_id = _extract_organization_id(data_object)
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La sesión de Stripe no identifica a ninguna organización",
        )
    organization = (
        await session.execute(
            select(Organization).where(Organization.id == organization_id)
        )
    ).scalar_one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La organización de la sesión de Stripe no existe",
        )
    credits = _extract_credits(data_object)
    session_id = _as_str(data_object.get("id"))

    try:
        entry = await apply_credit_delta(
            session=session,
            organization_id=organization_id,
            amount=Decimal(credits),
            reason=LedgerReasonEnum.STRIPE_PURCHASE,
            reference_id=session_id,
        )
    except Exception:
        await session.rollback()
        raise
    session.add(
        StripeEvent(
            event_id=event_id,
            event_type=event_type,
            organization_id=organization_id,
            session_id=session_id,
            credits_granted=entry.amount_delta,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Dos entregas concurrentes del mismo evento: la restricción única sobre
        # `event_id` es la que decide, y el perdedor descarta su recarga.
        await session.rollback()
        logger.info("Evento de Stripe concurrente descartado: %s", event_id)
        return WebhookAckResponse(
            status="ignored", duplicate=True, event_id=event_id, event_type=event_type
        )

    logger.info(
        "Recarga aplicada organization=%s credits=%s session=%s",
        organization_id,
        credits,
        session_id,
    )
    return WebhookAckResponse(
        status="processed",
        duplicate=False,
        event_id=event_id,
        event_type=event_type,
        credits_granted=credits,
        balance_after=entry.balance_after,
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


async def _record_ignored(
    session: AsyncSession,
    event_id: str,
    event_type: str,
    *,
    session_id: str | None = None,
) -> WebhookAckResponse:
    """Persista un evento que no aplica, para no reprocesarlo en la reentrega."""

    session.add(
        StripeEvent(event_id=event_id, event_type=event_type, session_id=session_id)
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
    return WebhookAckResponse(
        status="ignored", duplicate=False, event_id=event_id, event_type=event_type
    )


@router.get("/credits/ledger", response_model=list[CreditLedgerEntryResponse])
async def read_credit_ledger(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[CreditLedgerEntryResponse]:
    """Historial de asientos del tenant activo, del más reciente al más antiguo."""

    result = await session.execute(
        select(CreditLedger)
        .where(CreditLedger.organization_id == tenant.organization.id)
        .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
        .limit(limit)
    )
    return [
        CreditLedgerEntryResponse(
            id=entry.id,
            amount_delta=entry.amount_delta,
            balance_after=entry.balance_after,
            reason=entry.reason.value,
            reference_id=entry.reference_id,
            created_at=entry.created_at.isoformat(),
        )
        for entry in result.scalars().all()
    ]
