"""Pruebas de la firma HMAC y del despacho con reintentos.

La batería de firma se apoya en `SignedPayload.verify`, que recalcula la firma con la
misma implementación que la produjo. No es una prueba tautológica porque el punto de la
comprobación es que la **fórmula** sea la del contrato, y eso se fija comparando contra un
HMAC calculado a mano en la propia prueba, no contra la función del módulo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.webhooks.dispatcher import (
    MAX_DELIVERY_ATTEMPTS,
    backoff_seconds,
    decrypt_endpoint_secret,
    deliver_with_retries,
    dispatch_webhook,
    encrypt_endpoint_secret,
    should_retry,
)
from backend.apps.webhooks.models import (
    FAILURE_AUTO_DISABLE_THRESHOLD,
    WebhookDelivery,
    WebhookEndpoint,
    generate_webhook_secret,
)
from backend.apps.webhooks.signing import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    compute_signature,
    sign_payload,
    signature_header,
)
from backend.core.security import hash_password

SECRETO = "whsec_" + "ab" * 32
PAYLOAD: dict[str, Any] = {"event": "pentest.completed", "pentest_id": "abc"}

# Sin esta marca, el `conftest` entrega `integration_session = None` a propósito y todas
# las pruebas que escriben en la base fallarían al empezar. El fixture existe así para que
# una prueba de integración que se cuelga en una suite rápida sea evidente, no silenciosa.
pytestmark = pytest.mark.integration



@pytest.fixture(autouse=True)
def _destino_resoluble() -> Iterator[Any]:
    """Hace que el host de los endpoints resuelva a una IP pública válida.

    Sin esto la guarda anti-SSRF corta la entrega antes de que salga, y las pruebas del
    despacho medirían el rechazo en lugar del comportamiento que dicen medir. Se sustituye
    el **resolutor**, no la guarda: la validación sigue ejecutándose entera, que es
    justamente lo que no hay que desactivar.
    """

    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    ):
        yield

# --------------------------------------------------------------------------- #
# Firma
# --------------------------------------------------------------------------- #


def test_la_firma_coincide_con_un_hmac_calculado_a_mano() -> None:
    """La fórmula del contrato, calculada fuera del módulo que la implementa.

    Es la única forma de fijar que la firma es `hmac(secret, "{ts}.{body}")` y no otra
    cosa con el mismo nombre. Comparar contra `compute_signature` probaría que la función
    es consistente consigo misma, que es otra cosa.
    """

    cuerpo = b'{"event":"ping"}'
    instante = 1_700_000_000
    esperado = hmac.new(
        SECRETO.encode("utf-8"),
        f"{instante}.".encode("ascii") + cuerpo,
        hashlib.sha256,
    ).hexdigest()

    assert compute_signature(SECRETO, instante, cuerpo) == esperado


def test_la_cabecera_tiene_el_formato_t_y_v1() -> None:
    cabecera = signature_header(SECRETO, b'{"a":1}', timestamp=1_700_000_000)

    assert cabecera.startswith("t=1700000000,")
    assert "v1=" in cabecera
    assert len(cabecera.split("v1=")[1]) == 64


def test_el_timestamp_entra_en_la_firma() -> None:
    """Sin timestamp dentro, una captura se podría reenviar indefinidamente.

    Quien no acepte transacciones con tolerancia de reloj no puede reutilizar un payload
    antiguo, porque la firma se calculó con otro instante. Por eso el `t` se firma y no solo
    se viaja.
    """

    cuerpo = b'{"a":1}'
    una = compute_signature(SECRETO, 1_700_000_000, cuerpo)
    otra = compute_signature(SECRETO, 1_700_000_001, cuerpo)

    assert una != otra


def test_cambiar_un_byte_del_cuerpo_invalida_la_firma() -> None:
    """La firma cubre el cuerpo exacto, no un objeto reconstruido.

    Es lo que permite al receptor verificarla sin depender de cómo serializa.
    """

    original = sign_payload(SECRETO, {"a": 1, "b": 2})
    alterado = sign_payload(SECRETO, {"a": 1, "b": 3})

    assert original.verify(SECRETO) is True
    assert alterado.verify(SECRETO) is True
    assert original.verify(SECRETO) == original.verify(SECRETO)
    # Y la firma de uno no sirve para el cuerpo del otro.
    assert (
        hmac.compare_digest(
            compute_signature(SECRETO, original.timestamp, alterado.body),
            original.signature.split("v1=")[1],
        )
        is False
    )


def test_otro_secreto_no_verifica_la_firma() -> None:
    payload = sign_payload(SECRETO, PAYLOAD)
    assert payload.verify(SECRETO) is True
    assert payload.verify("whsec_" + "cd" * 32) is False


def test_el_cuerpo_es_estable_entre_serializaciones() -> None:
    """El orden de las claves no puede cambiar los bytes.

    Sin `sort_keys`, dos serializaciones del mismo diccionario darían cuerpos distintos
    si el diccionario se construyó en distinto orden, y la misma información quedaría
    firmada de dos maneras.
    """

    uno = sign_payload(SECRETO, {"z": 1, "a": 2, "m": 3}).body
    otro = sign_payload(SECRETO, {"m": 3, "a": 2, "z": 1}).body

    assert uno == otro


def test_las_cabeceras_de_entrega_llevan_el_contrato_completo() -> None:
    payload = sign_payload(SECRETO, PAYLOAD)
    cabeceras = payload.headers("pentest.completed", "d-123")

    assert cabeceras["Content-Type"] == "application/json"
    assert SIGNATURE_HEADER in cabeceras
    assert cabeceras[EVENT_HEADER] == "pentest.completed"
    assert cabeceras[DELIVERY_HEADER] == "d-123"


def test_un_secreto_vacio_no_permite_firmar() -> None:
    from backend.apps.webhooks.signing import WebhookSignatureError

    with pytest.raises(WebhookSignatureError):
        compute_signature("", 1, b"{}")


# --------------------------------------------------------------------------- #
# Reintentos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        (None, True),  # timeout o error de conexion
        (200, False),
        (201, False),
        (301, False),
        (400, False),  # el receptor entendio y rechazo: repetir no cambia nada
        (401, False),
        (403, False),
        (404, False),
        (410, False),
        (408, True),
        (425, True),
        (429, True),  # el receptor pide esperar
        (500, True),
        (502, True),
        (503, True),
        (504, True),
    ],
)
def test_solo_se_reintenta_lo_que_puede_cambiar(codigo: int | None, esperado: bool) -> None:
    """Un `4xx` no se reintenta nunca.

    El receptor entendió la petición y la rechazó. Repetirla idéntica tres veces solo
    multiplica el rechazo y el tráfico, y convierte un error del cliente en tres.
    """

    assert should_retry(codigo) is esperado


def test_el_crecimiento_es_exponencial() -> None:
    esperas = [backoff_seconds(n) for n in range(1, MAX_DELIVERY_ATTEMPTS + 1)]

    assert esperas[0] == 2.0
    assert esperas[1] == 4.0
    assert esperas[2] == 8.0
    # Y crece de verdad, no solo "es mas": el ratio se comprueba.
    assert esperas[1] / esperas[0] == esperas[2] / esperas[1] == 2.0


# --------------------------------------------------------------------------- #
# Despacho
# --------------------------------------------------------------------------- #


def _transport(
    respuestas: list[httpx.Response], registradas: list[httpx.Request]
) -> Any:
    """Transporte falso que devuelve las respuestas en orden."""

    def handler(request: httpx.Request) -> httpx.Response:
        registradas.append(request)
        return respuestas[min(len(registradas) - 1, len(respuestas) - 1)]

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


async def _endpoint(
    session: AsyncSession, *, activo: bool = True, fallos: int = 0
) -> tuple[WebhookEndpoint, uuid.UUID]:
    suffix = uuid.uuid4().hex
    org = Organization(name=f"Hook {suffix}", slug=f"hook-{suffix}")
    session.add(org)
    await session.flush()
    session.add(
        Membership(
            organization_id=org.id,
            user_id=(
                await _usuario(session, suffix)
            ).id,
            role=RoleEnum.ADMIN,
        )
    )
    endpoint = WebhookEndpoint(
        organization_id=org.id,
        url="https://hooks.example.com/entrada",
        encrypted_secret=encrypt_endpoint_secret(generate_webhook_secret(), org.id),
        event_types=["pentest.completed"],
        is_active=activo,
        consecutive_failures=fallos,
    )
    session.add(endpoint)
    await session.commit()
    await session.refresh(endpoint)
    return endpoint, org.id


async def _usuario(session: AsyncSession, suffix: str) -> User:
    user = User(
        email=f"hook-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Hook User",
        email_verified=True,
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_una_entrega_exitosa_registra_el_codigo_y_el_tiempo(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)
    peticiones: list[httpx.Request] = []

    entrega = await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(200, text="recibido")], peticiones),
    )

    assert entrega.succeeded is True
    assert entrega.status_code == 200
    assert entrega.response_body == "recibido"
    assert entrega.execution_time_ms is not None
    assert entrega.attempt == 1
    assert entrega.error_message is None
    # Un solo intento: no se reintenta lo que funcionó.
    assert len(peticiones) == 1


@pytest.mark.asyncio
async def test_la_peticion_lleva_la_firma_verificable(
    integration_session: AsyncSession,
) -> None:
    """Lo que sale por la red se puede verificar con el secreto del endpoint.

    Es la prueba de que la firma sirve para algo: el receptor, con el mismo secreto,
    recalcula y compara. Sin esto, la cabecera podría ser decorativa.
    """

    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)
    peticiones: list[httpx.Request] = []

    await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(200)], peticiones),
    )

    secreto = decrypt_endpoint_secret(endpoint)
    recibido = sign_payload(secreto, PAYLOAD, timestamp=_timestamp_de(peticiones[0]))
    assert recibido.verify(secreto) is True
    assert peticiones[0].headers[SIGNATURE_HEADER] == recibido.signature
    assert peticiones[0].content == recibido.body


def _timestamp_de(request: httpx.Request) -> int:
    cabecera = request.headers[SIGNATURE_HEADER]
    return int(cabecera.split("t=")[1].split(",")[0])


@pytest.mark.asyncio
async def test_un_5xx_se_reintenta_hasta_tres_veces(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)
    peticiones: list[httpx.Request] = []
    esperas: list[float] = []

    async def sin_espera(segundos: float) -> None:
        esperas.append(segundos)

    entrega = await deliver_with_retries(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(500, text="boom")], peticiones),
        sleeper=sin_espera,
    )

    assert len(peticiones) == MAX_DELIVERY_ATTEMPTS
    assert entrega is not None
    assert entrega.succeeded is False
    # El retroceso se respeta: sin esperar, tres 500 llegan en microsegundos y el
    # receptor que estaba reiniciando no llega a tiempo.
    assert esperas == [2.0, 4.0]


@pytest.mark.asyncio
async def test_cada_intento_deja_su_fila_en_el_historial(
    integration_session: AsyncSession,
) -> None:
    """Cada intento se registra, no solo el último.

    El diagnóstico que necesita el cliente es "cuántas veces intentó y qué vio cada vez".
    Guardar solo el final deja esa pregunta sin respuesta, y es la respuesta que separa
    "se cayó dos segundos" de "lleva días caído".
    """

    assert integration_session is not None
    endpoint, org_id = await _endpoint(integration_session)
    peticiones: list[httpx.Request] = []

    async def sin_espera(segundos: float) -> None:
        return None

    await deliver_with_retries(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(503)], peticiones),
        sleeper=sin_espera,
    )

    filas = (
        (
            await integration_session.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.endpoint_id == endpoint.id)
                .order_by(WebhookDelivery.attempt)
            )
        )
        .scalars()
        .all()
    )
    assert len(filas) == MAX_DELIVERY_ATTEMPTS
    assert [f.attempt for f in filas] == [1, 2, 3]
    assert all(f.status_code == 503 for f in filas)
    assert all(f.organization_id == org_id for f in filas)


@pytest.mark.asyncio
async def test_un_4xx_no_se_reintenta(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)
    peticiones: list[httpx.Request] = []

    await deliver_with_retries(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(400, text="payload malo")], peticiones),
    )

    assert len(peticiones) == 1, "un 400 no se reintenta"


@pytest.mark.asyncio
async def test_un_2xx_despues_de_un_5xx_deja_el_contador_a_cero(
    integration_session: AsyncSession,
) -> None:
    """El contador es de fallos **consecutivos**, no acumulados.

    Un endpoint que falla dos veces y luego responde bien esta sano. Si el contador no
    se pusiera a cero, se desactivaria en la octava entrega aunque la novena y la decima
    hubieran funcionado, y el usuario veria un endpoint apagado sin motivo aparente.
    """

    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session, fallos=7)
    respuestas = [httpx.Response(500), httpx.Response(500), httpx.Response(200)]
    peticiones: list[httpx.Request] = []

    async def sin_espera(segundos: float) -> None:
        return None

    await deliver_with_retries(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport(respuestas, peticiones),
        sleeper=sin_espera,
    )

    refrescado = (
        await integration_session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint.id)
        )
    ).scalar_one()
    assert refrescado.consecutive_failures == 0
    assert refrescado.is_active is True


@pytest.mark.asyncio
async def test_un_timeout_se_registra_sin_codigo(
    integration_session: AsyncSession,
) -> None:
    """`status_code = NULL` distingue "no hubo respuesta" de "hubo un 0".

    Es lo que permite que el panel diga "el receptor no respondió" en vez de enseñar un
    código vacío que parece un bug.
    """

    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tarde demasiado")

    entrega = await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert entrega.status_code is None
    assert entrega.succeeded is False
    assert "Tiempo de espera" in (entrega.error_message or "")


@pytest.mark.asyncio
async def test_el_contador_llega_al_umbral_y_desactiva_el_endpoint(
    integration_session: AsyncSession,
) -> None:
    """Al décimo fallo el endpoint se apaga solo y queda en el rastro forense.

    Sin el asiento, un endpoint desactivado es un misterio: nadie sabe cuándo pasó, por
    qué ni cuál era la URL. El asiento responde a las tres.
    """

    assert integration_session is not None
    endpoint, org_id = await _endpoint(
        integration_session, fallos=FAILURE_AUTO_DISABLE_THRESHOLD - 1
    )

    await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(500)], []),
    )

    refrescado = (
        await integration_session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint.id)
        )
    ).scalar_one()
    assert refrescado.consecutive_failures == FAILURE_AUTO_DISABLE_THRESHOLD
    assert refrescado.is_active is False

    asiento = (
        (
            await integration_session.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.organization_id == org_id,
                    AuditLogEntry.action == AuditActionEnum.WEBHOOK_AUTO_DISABLED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(asiento) == 1
    assert asiento[0].entity_id == endpoint.id
    assert asiento[0].to_state == "auto_disabled"


@pytest.mark.asyncio
async def test_un_redireccion_se_registra_y_no_se_sigue(
    integration_session: AsyncSession,
) -> None:
    """Un `302` a la red interna es la vía más barata de saltar la validación.

    No se sigue, se guarda. El usuario ve en el historial que su receptor manda
    redirecciones, y eso es información que necesita para arreglarlo.
    """

    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)

    entrega = await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport(
            [httpx.Response(302, headers={"location": "http://169.254.169.254/"})], []
        ),
    )

    assert entrega.status_code == 302
    assert entrega.succeeded is False
    assert "redirección no se sigue" in (entrega.error_message or "")


@pytest.mark.asyncio
async def test_una_url_bloqueada_en_el_momento_del_envio_no_sale(
    integration_session: AsyncSession,
) -> None:
    """La guarda se aplica también al enviar, no solo al registrar.

    Una URL aceptada hace meses puede haber cambiado de DNS. Si solo se validara al
    registrar, la plataforma estaría haciendo peticiones a un destino que nadie revisó.
    """

    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)
    endpoint.url = "https://169.254.169.254/latest/meta-data"
    await integration_session.commit()
    peticiones: list[httpx.Request] = []

    entrega = await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(200)], peticiones),
    )

    assert len(peticiones) == 0, "salio una peticion a una direccion bloqueada"
    assert entrega.succeeded is False
    assert "anti-SSRF" in (entrega.error_message or "")


@pytest.mark.asyncio
async def test_el_secreto_no_aparece_en_la_entrega_ni_en_la_firma(
    integration_session: AsyncSession,
) -> None:
    """El historial guarda lo que se envió, y lo que se envió no lleva el secreto.

    Un `WebhookDelivery` se muestra en el panel y acaba en exportaciones. Si el secreto
    estuviera en la fila, cada lectura del historial sería una filtración.
    """

    assert integration_session is not None
    endpoint, _org = await _endpoint(integration_session)
    peticiones: list[httpx.Request] = []

    entrega = await dispatch_webhook(
        integration_session,
        endpoint,
        "pentest.completed",
        PAYLOAD,
        client_factory=_transport([httpx.Response(200)], peticiones),
    )

    secreto = decrypt_endpoint_secret(endpoint)
    assert secreto not in json.dumps(entrega.payload)
    assert secreto not in json.dumps(entrega.response_body or "")
    assert secreto not in peticiones[0].content.decode("utf-8")
    # Y la cabecera de firma no lo lleva: solo su HMAC.
    assert secreto not in peticiones[0].headers[SIGNATURE_HEADER]


def test_el_secreto_generado_tiene_la_forma_prometida() -> None:
    secreto = generate_webhook_secret()

    assert secreto.startswith("whsec_")
    assert len(secreto) == 70, "6 del prefijo + 64 hexadecimales de 32 bytes"
    assert len(set(secreto)) > 10, "un secreto generado no es un patron repetido"


def test_los_secretos_generados_no_se_repiten() -> None:
    assert len({generate_webhook_secret() for _ in range(5_000)}) == 5_000


def test_el_secreto_cifrado_tiene_forma_portatil_y_no_contiene_el_plano() -> None:
    """El cifrado es el del proyecto, con su prefijo de versión, y **cabe en la columna**.

    `v1.` permite rotar el algoritmo en el futuro distinguiendo material viejo del nuevo,
    y es lo que las columnas de `git_credentials` ya comprueban con una restricción.

    La longitud se compara con la constante del modelo y no con un número escrito aquí.
    Un `VARCHAR` corto en PostgreSQL trunca en silencio: el material se guarda incompleto
    y el único síntoma es que la firma del receptor no valida, mucho después y en el
    cliente. Esta aserción existe para que un cambio de formato obligue a mirar la
    columna, y para eso tiene que leer la misma constante que usa la columna.
    """

    from backend.apps.webhooks.models import WEBHOOK_SECRET_CIPHERTEXT_LENGTH

    plano = generate_webhook_secret()
    cifrado = encrypt_endpoint_secret(plano, uuid.uuid4())

    assert cifrado.startswith("v1.")
    assert plano not in cifrado
    assert len(cifrado) <= WEBHOOK_SECRET_CIPHERTEXT_LENGTH, (
        f"el secreto cifrado mide {len(cifrado)} y la columna "
        f"{WEBHOOK_SECRET_CIPHERTEXT_LENGTH} lo truncaría en silencio"
    )


def test_el_secreto_cifrado_no_se_descifra_con_otro_tenant() -> None:
    """El AAD ata el secreto a su organización.

    Sin esa ligazón, mover una fila de `webhook_endpoints` de un tenant a otro en la base
    haría que el secreto se descifrara en el contexto equivocado.
    """

    from backend.core.crypto import CryptoError

    organization_id = uuid.uuid4()
    cifrado = encrypt_endpoint_secret("whsec_" + "ff" * 32, organization_id)

    with pytest.raises(CryptoError):
        decrypt_endpoint_secret(
            WebhookEndpoint(
                organization_id=uuid.uuid4(),
                url="https://x.example.com",
                encrypted_secret=cifrado,
                event_types=[],
            )
        )
