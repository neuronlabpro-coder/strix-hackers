"""Cliente de Stripe y verificación de firma de webhooks.

Envuelve la librería oficial detrás de una interfaz propia para dos motivos:
las pruebas pueden sustituir `get_stripe_client` por un doble, y el resto del
código nunca ve un `SecretStr` de Stripe ni una respuesta cruda del proveedor.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final, Protocol

import stripe
from pydantic import SecretStr

from backend.core.config import settings

logger = logging.getLogger(__name__)

CHECKOUT_SESSION_MODE: Final[str] = "payment"

# Ventana de tolerancia de la marca temporal de la firma. Acota la ventana de
# reenvío de un webhook capturado: sin ella, una firma válida lo seguiría siendo
# indefinidamente.
WEBHOOK_TOLERANCE_SECONDS: Final[int] = 300


class StripeConfigurationError(RuntimeError):
    """Falta configuración de Stripe para ejecutar la operación solicitada."""


class StripeWebhookSignatureError(RuntimeError):
    """La firma del webhook no valida contra el secreto configurado."""


class StripeClient(Protocol):
    """Superficie mínima de Stripe que necesita la aplicación."""

    async def create_checkout_session(self, **kwargs: Any) -> dict[str, Any]: ...

    async def verify_webhook(self, payload: bytes, signature_header: str) -> dict[str, Any]: ...


class StripeSDKClient:
    """Adaptador sobre el SDK oficial de Stripe.

    El secreto del webhook se captura en la construcción en lugar de leerse del
    estado global en cada llamada: hace el cliente verificable y evita que un
    cambio de configuración a mitad de una verificación altere el resultado.
    """

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        self._sdk = stripe.StripeClient(secret_key, max_network_retries=2)
        self._webhook_secret = webhook_secret

    @property
    def _checkout_sessions(self) -> Any:
        """Servicio de sesiones de Checkout, en el espacio de nombres `v1`.

        `StripeClient.checkout` sigue funcionando en 15.x pero emite un
        `DeprecationWarning` en cada llamada y desaparecerá. Se usa `v1` cuando existe
        y se recurre al camino antiguo si el SDK instalado fuera anterior, para que el
        cliente no dependa de una versión exacta del SDK.

        La diferencia no es cosmética: el espacio `v1` es el que va a permanecer, y
        mantener el otro obligaría a tocar esto otra vez dentro de dos versiones.
        """

        namespace = getattr(self._sdk, "v1", None)
        checkout = getattr(namespace, "checkout", None) if namespace is not None else None
        if checkout is not None:
            return checkout.sessions
        return self._sdk.checkout.sessions

    async def create_checkout_session(self, **params: Any) -> dict[str, Any]:
        """Crea una sesión de Checkout.

        En `stripe` 15.x la firma real es
        `create_async(params: SessionCreateParams, options)`: **un único diccionario
        posicional**, no argumentos con nombre. La versión anterior de este método
        pasaba `mode=`, `line_items=`, `metadata=`… como kwargs y fallaba con
        `TypeError: create_async() got an unexpected keyword argument 'mode'`.

        Fallaba solo contra la API real. La suite lo cubría con un doble que acepta
        cualquier cosa, así que daba verde mientras la función no podía crear ni una
        sola sesión de pago. La firma se escribe aquí como posicional para que el
        próximo cambio del SDK lo detecte el analizador de tipos y no una compra
        fallida en producción.
        """

        session = await self._checkout_sessions.create_async(params)
        # `StripeObject` se proyecta a un dict con las claves que el backend
        # necesita. No se copia el objeto entero: el resto del código no debe
        # depender de los accesores dinámicos del SDK ni recibir campos que la
        # plataforma no usa.
        return {
            "id": getattr(session, "id", None),
            "url": getattr(session, "url", None),
        }

    async def verify_webhook(self, payload: bytes, signature_header: str) -> dict[str, Any]:
        """Verifica la firma HMAC y devuelve el evento ya parseado.

        En la API actual del SDK, `verify_header` solo comprueba la firma y
        devuelve un booleano; el parseo del evento es un paso aparte con
        `Event.construct_from`. Verificar y luego parsear el mismo `bytes` evita
        que el objeto verificado venga de otra fuente.
        """

        try:
            stripe.WebhookSignature.verify_header(
                payload,
                signature_header,
                self._webhook_secret,
                tolerance=WEBHOOK_TOLERANCE_SECONDS,
            )
        except stripe.SignatureVerificationError as error:
            raise StripeWebhookSignatureError("Firma de webhook inválida") from error
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError) as error:
            raise StripeWebhookSignatureError(
                "El cuerpo del webhook no es JSON válido"
            ) from error
        if not isinstance(decoded, dict):
            raise StripeWebhookSignatureError("El evento de Stripe no es un objeto")
        return decoded


def get_stripe_client() -> StripeClient:
    """Construye el cliente o falla con un mensaje accionable.

    La ausencia de credenciales en local no es un error de arranque: la
    plataforma debe arrancar y servir el panel para que un desarrollador pueda
    trabajar sin cuenta de Stripe. Solo falla cuando alguien intenta cobrar.
    """

    secret_key: SecretStr | None = settings.stripe_secret_key
    if secret_key is None or not secret_key.get_secret_value():
        raise StripeConfigurationError(
            "STRIPE_SECRET_KEY no está configurada: define la clave en .env para activar el cobro"
        )
    webhook_secret: SecretStr | None = settings.stripe_webhook_secret
    webhook_value = (
        webhook_secret.get_secret_value() if webhook_secret is not None else ""
    )
    return StripeSDKClient(secret_key.get_secret_value(), webhook_value)


def require_webhook_secret() -> str:
    """Entrega el secreto del webhook o falla con un mensaje accionable."""

    secret = settings.stripe_webhook_secret
    if secret is None or not secret.get_secret_value():
        raise StripeConfigurationError(
            "STRIPE_WEBHOOK_SECRET no está configurada: define el secreto en .env"
        )
    return secret.get_secret_value()
