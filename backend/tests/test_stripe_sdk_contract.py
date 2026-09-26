"""Prueba que fija la forma de la llamada al SDK de Stripe.

Existe por un bug que la suite no podia ver: `create_checkout_session` pasaba
`mode=`, `line_items=`, `metadata=` como argumentos con nombre, y en `stripe` 15.x la
firma real es `create_async(params)` con un unico diccionario posicional. La suite
pasaba porque el doble acepta cualquier llamada; contra la API real reventaba con
`TypeError`.

Esta prueba ata la forma de la llamada **sin** llamar a la red: intercepta el
servicio y comprueba como se invoca. Un doble que acepta cualquier cosa no detectaria
una ruptura de contrato con el SDK, que es justo lo que ocurrio.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.apps.billing.stripe import StripeSDKClient


class _FakeSession:
    id = "cs_test_123"
    url = "https://checkout.stripe.com/c/pay/cs_test_123"


class _RecordingService:
    """Captura como se invoca `create_async` y reproduce la firma real del SDK."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def create_async(self, *args: Any, **kwargs: Any) -> _FakeSession:
        # La firma real es `create_async(params, options)`: un dict posicional y un
        # segundo argumento opcional. Cualquier otra forma es un TypeError en el SDK
        # de verdad, y este doble lo reproduce en vez de aceptarlo en silencio.
        self.calls.append((args, kwargs))
        if not args or not isinstance(args[0], dict):
            raise TypeError(
                "create_async() missing 1 required positional argument: 'params'"
            )
        if kwargs:
            unexpected = next(iter(kwargs))
            raise TypeError(f"create_async() got an unexpected keyword argument '{unexpected}'")
        return _FakeSession()


def _client_with(service: _RecordingService) -> StripeSDKClient:
    client = StripeSDKClient.__new__(StripeSDKClient)
    client._sdk = object()  # type: ignore[attr-defined]
    client._webhook_secret = "whsec_test"  # type: ignore[attr-defined]
    # Se sustituye la property por un servicio que exige la firma real.
    type(client)._checkout_sessions = property(lambda self: service)  # type: ignore[assignment]
    return client


@pytest.fixture(autouse=True)
def _restore_property() -> Any:
    original = StripeSDKClient._checkout_sessions
    yield
    StripeSDKClient._checkout_sessions = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_create_checkout_session_passes_a_single_positional_params_dict() -> None:
    """El SDK recibe UN diccionario posicional, no una lluvia de kwargs."""

    service = _RecordingService()
    client = _client_with(service)

    result = await client.create_checkout_session(
        mode="payment",
        line_items=[{"price_data": {"currency": "usd", "unit_amount": 1900}}],
        metadata={"organization_id": str(uuid.uuid4())},
    )

    assert result == {"id": "cs_test_123", "url": "https://checkout.stripe.com/c/pay/cs_test_123"}
    assert len(service.calls) == 1
    args, kwargs = service.calls[0]
    # Cero kwargs: es exactamente lo que rompia.
    assert kwargs == {}, f"la llamada no debe pasar kwargs, recibio {sorted(kwargs)}"
    assert len(args) == 1, f"debe recibir un solo argumento posicional, recibio {len(args)}"
    params = args[0]
    assert isinstance(params, dict)
    # Y el contenido llega intacto.
    assert params["mode"] == "payment"
    assert params["metadata"]["organization_id"]
    assert params["line_items"][0]["price_data"]["unit_amount"] == 1900


@pytest.mark.asyncio
async def test_sdk_signature_is_a_single_positional_params_dict() -> None:
    """Verifica la firma contra el SDK instalado, no contra una suposición.

    Esta es la prueba que habría detectado el bug: si el SDK cambia la firma otra vez,
    falla aqui en vez de en la primera compra de un cliente.
    """

    import inspect

    import stripe

    sdk_client = stripe.StripeClient("sk_test_placeholder")
    namespace = getattr(sdk_client, "v1", None)
    service = (
        namespace.checkout.sessions if namespace is not None else sdk_client.checkout.sessions
    )
    parameters = list(inspect.signature(service.create_async).parameters)
    assert parameters[0] == "params", (
        f"el SDK ahora recibe {parameters!r} como primer parametro; el cliente tiene "
        "que pasar un diccionario posicional"
    )
    # Y solo dos: params y options. Un tercer argumento seria una senal de que la API
    # cambio de forma otra vez.
    assert parameters[:2] == ["params", "options"], parameters
