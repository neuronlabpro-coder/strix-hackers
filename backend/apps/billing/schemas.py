"""Esquemas de la API de facturación."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Paquetes de créditos comercializables. R1 prohíbe precios en el código, así que
# viven aquí como catálogo versionable y no en el frontend: el panel los lee de
# esta respuesta y jamás inventa un importe.
CREDIT_PACKS: dict[int, Decimal] = {
    25: Decimal("25.00"),
    100: Decimal("100.00"),
    250: Decimal("250.00"),
}

#: Mínimo del pack a medida, en créditos. Con la paridad 1:1 del catálogo, 10 créditos son
#: $10, que es el mínimo que cubre el coste fijo de una sesión de Checkout.
#:
#: Vive aquí y no en la validación del endpoint por la misma razón que el resto del
#: catálogo: es política comercial versionable. El esquema lo lee, no lo decide.
CUSTOM_CREDITS_MINIMUM = 10


def price_for_credits(credits: int) -> Decimal:
    """El precio de una cantidad de créditos, en dólares.

    ## Por qué el pack a medida cuesta lo que vale

    El catálogo está a la paridad declarada en `core.config`: `credits_per_usd = 1.00`, y
    los tres packs lo respetan. Una cantidad que no está en el catálogo se cobra por la
    misma paridad, así que el precio de cualquier compra es `credits * credits_per_usd`.

    Antes el catálogo tenía descuento por volumen —$0,038 en el pack pequeño y $0,0266 en
    el grande— y por eso no existía un precio único: en medio del descuento, «100 créditos»
    no tenía un precio sino un rango. Con la paridad única, **sí** lo tiene, y el panel
    puede calcular el importe de la compra a medida sin preguntar.

    La función existe para que ese cálculo no se escriba en tres sitios. Es la única que
    decide el importe, y tanto el endpoint de compra como el resumen de facturación la
    usan.
    """

    if credits in CREDIT_PACKS:
        return CREDIT_PACKS[credits]
    return Decimal(credits)


class CheckoutSessionRequest(BaseModel):
    """Solicitud de sesión de Stripe Checkout para una compra de créditos.

    Admite las tres cosas que el panel puede pedir:

    - un pack del catálogo (`25`, `100`, `250`), que es lo que ofrecen las tarjetas;
    - cualquier cantidad a partir de `CUSTOM_CREDITS_MINIMUM`, que es el campo libre del
      pack a medida.

    ## Por qué se acepta una cantidad que no está en el catálogo

    Restringir la compra a tres valores obligaría al cliente que quiere 40 créditos a
    comprar 25 y desperdiciar 5, o 100 y pagar 60 de más. El mínimo cubre el coste fijo de
    la sesión de cobro; por encima de eso, cualquier cantidad es una compra legítima.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    credits: int = Field(
        description="Créditos a comprar. Pack del catálogo o cantidad a medida.",
        ge=CUSTOM_CREDITS_MINIMUM,
        le=1_000_000,
    )
    success_url: str = Field(max_length=2048)
    cancel_url: str = Field(max_length=2048)

    @model_validator(mode="after")
    def validate_pack_and_urls(self) -> CheckoutSessionRequest:
        if self.credits < CUSTOM_CREDITS_MINIMUM:
            raise ValueError(
                f"La compra mínima son {CUSTOM_CREDITS_MINIMUM} créditos; "
                f"se recibieron {self.credits}"
            )
        for field_name, value in (
            ("success_url", self.success_url),
            ("cancel_url", self.cancel_url),
        ):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{field_name} debe ser una URL absoluta HTTP(S)")
        return self

    @property
    def amount_usd(self) -> Decimal:
        """El importe de esta compra, resuelto por el catálogo."""

        return price_for_credits(self.credits)


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


class CreditPackResponse(BaseModel):
    """Un paquete comprable del catálogo comercial.

    `usd_per_credit` lo calcula el servidor para que el panel pueda mostrar el precio
    unitario sin dividir en el cliente, donde un redondeo distinto por navegador daría dos
    cifras para el mismo producto.
    """

    credits: int = Field(gt=0)
    amount_usd: Decimal = Field(gt=0)
    usd_per_credit: Decimal = Field(gt=0)


class BillingSummaryResponse(BaseModel):
    """Resumen de facturacion del tenant, para las tarjetas KPI del panel.

    ## Por qué el importe en dólares es exacto y no una estimación

    El catálogo está a la paridad declarada en `core.config`: `credits_per_usd = 1.00`, y
    los tres packs lo respetan. Sin descuento por volumen, el precio de una cantidad de
    créditos es la cantidad, así que el equivalente en dólares de un saldo es un número que
    el cliente puede comprobar en lugar de una cifra que hay que estimar.

    Cuando el catálogo tenía descuento —$0,038 por crédito en el pack pequeño y $0,0266 en
    el grande— no existía un precio por crédito y este campo tenía que ser una estimación
    al mejor precio. Consecuencia visible de haberlo dejado así: 1000 créditos salían en
    $26.315,79, que no correspondía a ningún producto y no se podía comprobar contra nada.
    """

    credit_balance: Decimal
    credit_balance_usd: Decimal
    #: La paridad usada, para que el panel no la vuelva a derivar por su cuenta.
    credits_per_usd: Decimal = Field(gt=0)
    spent_this_month: Decimal = Field(ge=0)
    purchased_this_month: Decimal = Field(ge=0)
    spent_this_month_usd: Decimal = Field(ge=0)
    period_start: datetime
    packs: list[CreditPackResponse]
    #: Límites del pack a medida, en créditos.
    custom_minimum: int = Field(ge=1)
    custom_maximum: int = Field(ge=1)
