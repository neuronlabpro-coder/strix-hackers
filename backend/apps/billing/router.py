"""Endpoints de checkout de Stripe y webhook de recarga."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
    PRO_SUBSCRIPTION_PLAN,
    BillingSummaryResponse,
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
from backend.apps.billing.summary import (
    available_packs,
    credit_activity,
    credits_to_usd,
    subscription_offer,
    volume_pricing,
)
from backend.apps.organizations.models import Organization, RoleEnum
from backend.apps.webhooks.emission import (
    EventType,
    credits_purchased_payload,
    publish_event,
)
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.http_body import leer_cuerpo_acotado
from backend.core.middleware import TenantContext, get_current_tenant
from backend.core.rate_limit import enforce_checkout_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
CheckoutRateLimit = Depends(enforce_checkout_rate_limit)

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
    """Crea una sesión de Stripe Checkout: recarga de créditos o suscripción Pro.

    ## Por qué el importe sale del catálogo y nunca del cliente

    El cuerpo lleva `credits`, no un importe. El precio lo decide `price_for_credits` en el
    servidor. Aceptar un `amount` del cliente sería aceptar que el cliente decida cuánto
    paga por cuántos créditos, que es la definición de un endpoint de cobro roto.

    Lo mismo con el descuento: el cliente no envía un porcentaje, solo la cantidad. El
    tramo lo elige la escalera, y por tanto el servidor. Un endpoint que aceptara el
    descuento lo dejaría en manos de quien llama.
    """

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

    # El importe lo resuelve el catálogo, nunca el cuerpo. Duplicar la regla aquí la haría
    # divergir en cuanto el catálogo cambiara.
    amount = payload.amount_usd
    es_suscripcion = payload.mode == "subscription"
    # La organización viaja en metadata firmada por Stripe, nunca en la URL: la
    # URL de Checkout es pública y se puede compartir o interceptar.
    #
    # `credits` va a `0` en una suscripción, y `mode` explícito para que el webhook sepa
    # qué hacer **antes** de mirar nada más. Sin ese discriminante, un webhook de suscripción
    # con `credits=0` acreditaría cero créditos y parecería un glitch en vez de un cambio de
    # plan.
    metadata = {
        "organization_id": str(tenant.organization.id),
        "credits": str(payload.granted_credits),
        "mode": payload.mode,
    }
    try:
        checkout = await client.create_checkout_session(
            mode="subscription" if es_suscripcion else "payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(amount * 100),
                        "product_data": {
                            "name": (
                                "Fenix Pro (suscripción mensual)"
                                if es_suscripcion
                                else f"Fenix credits ({payload.credits})"
                            )
                        },
                        "recurring": {"interval": "month"} if es_suscripcion else None,
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
        mode=payload.mode,
        credits=payload.granted_credits,
        amount_usd=amount,
    )


def _extract_mode(data_object: dict[str, Any]) -> str:
    """El modo de pago que se creó la sesión: `credits` o `subscription`.

    ## Por qué no se deduce de los créditos

    Porque la suscripción **no** acredita créditos. Una sesión de suscripción lleva
    `credits=0` en su metadata, que es indistinguible de una recarga de cero créditos —y una
    recarga de cero no debería existir, así que un webhook que la tratara como recarga
    escribiría un asiento de valor cero en el libro de un cliente que ya pagó.

    El discriminante viaja explícito en la metadata que pone `create_checkout_session`, así
    que lo decide el endpoint que creó la sesión y no este webhook. Un valor desconocido se
    trata como `credits`, que es el modo por defecto del esquema y el que existía antes de
    que hubiera suscripciones: un evento antiguo, sin la clave, sigue acreditando.
    """

    metadata = data_object.get("metadata")
    if not isinstance(metadata, dict):
        return "credits"
    modo = metadata.get("mode")
    if modo == "subscription":
        return "subscription"
    return "credits"


async def _aplicar_suscripcion(
    session: SessionDependency,
    *,
    organization: Organization,
    event_id: str,
    event_type: str,
    data_object: dict[str, Any],
) -> WebhookAckResponse:
    """Sube el plan del workspace a Pro y asienta el evento.

    ## Por qué se guarda el `StripeEvent` también en una suscripción

    Por idempotencia, y por el mismo motivo que en la recarga. Stripe reintenta cualquier
    entrega que no reciba un `2xx`, así que este webhook **va** a recibir el mismo
    `checkout.session.completed` dos veces. Sin la fila en `stripe_events`, la segunda
    entrega volvería a aplicar el cambio.

    Aquí el cambio es idempotente por naturaleza —asignar `PRO` a algo que ya es `PRO` no
    tiene efecto— así que el riesgo es menor que en la recarga. Aun así se registra: el
    evento existe, el registro es la prueba de que llegó, y `plan_tier` en la respuesta
    permite al panel refrescar la cabecera sin una llamada extra.

    ## Por qué no se toca el saldo

    Una suscripción cambia el plan. Si acreditara créditos, el cliente esperaría un saldo
    que depende de un segundo que la interfaz no le muestra, y el soporte recibiría
    preguntas que no puede responder con los datos que tiene. El saldo se compra con
    `mode="credits"`, que es un camino explícito y separado.
    """

    previous_plan = organization.plan_tier
    organization.plan_tier = PRO_SUBSCRIPTION_PLAN
    session.add(
        StripeEvent(
            event_id=event_id,
            event_type=event_type,
            organization_id=organization.id,
            session_id=_as_str(data_object.get("id")),
            credits_granted=0,
            amount_cents=_extract_amount_cents(data_object),
        )
    )
    await session.commit()
    logger.info(
        "Suscripcion aplicada organization=%s plan %s -> %s",
        organization.id,
        previous_plan,
        PRO_SUBSCRIPTION_PLAN,
    )
    return WebhookAckResponse(
        status="processed",
        event_id=event_id,
        event_type=event_type,
        credits_granted=0,
        plan_tier=organization.plan_tier,
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


def _extract_amount_cents(data_object: dict[str, Any]) -> int | None:
    """El importe cobrado, en centavos, o `None` si el evento no lo trae.

    ## Por qué se guarda ahora y no se deducía después

    La tabla `stripe_events` guardaba qué evento se procesó, a qué organización y cuántos
    créditos acreditó, pero **no cuánto se cobró**. La consola de administración muestra las
    ventas con su importe, y ese dato no estaba: había dos salidas, inventar un cero o
    preguntarle a Stripe una fila cada vez que se abría la vista. Un cero en una columna de
    importes informa de que no se ha facturado nada, que es una conclusión falsa.

    Guardarlo **en el momento de la ingestión** es el único sitio donde el payload existe.
    Después, el evento ya está procesado y el importe solo se obtendría llamando a Stripe
    de nuevo.

    ## Por qué devuelve `None` y no `0`

    No todos los eventos de Stripe son un cobro. Una suscripción, un aviso de cuenta o una
    sesión caducada no tienen importe, y `None` los distingue de un cobro de cero —que no
    existe, porque Stripe no cobra cero— sin ensuciar la columna con un número inventado.
    """

    for clave in ("amount_total", "amount"):
        valor = data_object.get(clave)
        if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
            return valor
    return None


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
    # La lectura acotada va **antes** de la firma a propósito. Antes era `await
    # request.body()` con un `if len(body) > ...` detrás, que materializa el cuerpo entero para
    # luego decir que era demasiado grande: un atacante sin firma ganaba la memoria antes de que
    # el tope se aplicara. Ver `core/http_body.py`.
    body = await leer_cuerpo_acotado(request, settings.stripe_webhook_max_body_bytes)

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

    if _extract_mode(data_object) == "subscription":
        # Una suscripción cambia el plan y **no** acredita saldo. La rama va antes de leer
        # los créditos para que no haya forma de que un `credits=0` acabe en
        # `apply_credit_delta`, donde se escribiría un asiento de valor cero que ensuciaría
        # el libro y que la UI mostraría como una compra de nada.
        return await _aplicar_suscripcion(
            session,
            organization=organization,
            event_id=event_id,
            event_type=event_type,
            data_object=data_object,
        )

    credits = _extract_credits(data_object)
    session_id = _as_str(data_object.get("id"))
    amount_cents = _extract_amount_cents(data_object)

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
            amount_cents=amount_cents,
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
    # Se emite **después** del commit y solo si no era duplicado: la rama de arriba
    # devuelve antes para el perdedor de la carrera, y un evento `credits_purchased` de
    # una compra ya acreditada haría que el receptor de turno confirmara dos veces.
    await publish_event(
        session,
        EventType.CREDITS_PURCHASED,
        organization_id,
        credits_purchased_payload(
            amount=entry.amount_delta,
            balance_after=entry.balance_after,
            session_id=session_id,
        ),
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


@router.get("/summary", response_model=BillingSummaryResponse)
async def read_billing_summary(
    tenant: TenantDependency,
    session: SessionDependency,
) -> BillingSummaryResponse:
    """Saldo, consumo del mes y catálogo comercial, en una respuesta.

    Va todo junto porque el panel pinta una fila de tarjetas sobre el saldo y el catálogo
    de packs a la vez: pedirlo en tres llamadas paralelas se ve como un panel que tarda en
    aparecer y parpadea mientras llegan.

    ## Por qué el consumo del mes se recorta en el servidor

    Recortar en el cliente obligaría a traer los asientos de todo el histórico para
    descartar los de meses anteriores. La fecha la calcula el servidor en UTC, así que el
    resultado no depende de la zona horaria de quien mira.
    """

    now = datetime.now(UTC)
    saldo, consumidos, comprados = await credit_activity(
        session, tenant.organization.id, now
    )
    escalera = volume_pricing()
    return BillingSummaryResponse(
        credit_balance=saldo,
        credit_balance_usd=credits_to_usd(saldo),
        credits_per_usd=settings.credits_per_usd,
        spent_this_month=consumidos,
        purchased_this_month=comprados,
        spent_this_month_usd=credits_to_usd(consumidos),
        period_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        packs=available_packs(),
        custom_minimum=escalera.minimum_credits,
        custom_maximum=escalera.maximum_credits,
        volume=escalera,
        subscription=subscription_offer(tenant.organization),
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
