"""Esquemas de la API de facturación."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Paquetes de créditos comercializables. R1 prohíbe precios en el código, así que
# viven aquí como catálogo versionable y no en el frontend: el panel los lee de
# esta respuesta y jamás inventa un importe.
CREDIT_PACKS: dict[int, Decimal] = {
    500: Decimal("19.00"),
    1500: Decimal("49.00"),
    5000: Decimal("149.00"),
    15000: Decimal("399.00"),
}


class CheckoutSessionRequest(BaseModel):
    """Solicitud de sesión de Stripe Checkout para un paquete de créditos."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    credits: int = Field(description="Créditos del catálogo comercializable.")
    success_url: str = Field(max_length=2048)
    cancel_url: str = Field(max_length=2048)

    @model_validator(mode="after")
    def validate_pack_and_urls(self) -> CheckoutSessionRequest:
        if self.credits not in CREDIT_PACKS:
            raise ValueError("El paquete de créditos solicitado no existe en el catálogo")
        for field_name, value in (
            ("success_url", self.success_url),
            ("cancel_url", self.cancel_url),
        ):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{field_name} debe ser una URL absoluta HTTP(S)")
        return self


class CheckoutSessionResponse(BaseModel):
    """Sesión de pago creada. Nunca incluye la clave secreta del backend."""

    session_id: str
    url: str
    credits: int
    amount_usd: Decimal
    currency: str = "usd"


class WebhookAckResponse(BaseModel):
    """Acuse de recibo del webhook de Stripe.

    Stripe reintenta cualquier respuesta que no sea `2xx`, así que el código de
    estado es parte del contrato: un evento que no se pudo aplicar por una causa
    permanente debe devolver `2xx` con `status: "ignored"` para detener los
    reintentos, y solo un error transitorio devuelve `5xx`.
    """

    status: Literal["processed", "ignored"]
    duplicate: bool = False
    event_id: str
    event_type: str
    credits_granted: int = 0
    balance_after: Decimal | None = None


class CreditLedgerEntryResponse(BaseModel):
    """Asiento del ledger expuesto al panel de facturación."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    amount_delta: Decimal
    balance_after: Decimal
    reason: str
    reference_id: str | None
    created_at: str
