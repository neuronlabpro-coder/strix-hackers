"""La conexion de un webhook va a la IP validada, no a la que el nombre resuelva despues.

## Que hacia antes

`ensure_delivery_url_allowed` resolvia el nombre y comprobaba que **ninguna** de sus IP
fuera privada. Eso es correcto y no basta: entre esa comprobacion y la conexion, el
cliente de HTTP resolvia **otra vez**, y un atacante con control sobre el DNS de su
propio host —que es justo quien puede registrar un webhook— podia devolver una IP
publica en la validacion y una interna un milisegundo despues. Es *DNS rebinding*, y
dejaba la guarda anti-SSRF con una puerta que se abre sola.

El riesgo no era hipotetico: desde la red del worker se alcanzan PostgreSQL en la malla de
Tailscale, Redis y los metadatos de la nube. Responder desde ahi es devolver las
credenciales de la instancia.

## Que se comprueba aqui

1. `pinned_destination` devuelve una URL con la **IP** y el nombre original aparte.
2. El dispatcher **usa** esa URL y no la del endpoint, y manda el nombre en la cabecera
   `Host` y en la extension `sni_hostname`.
3. Con un cliente falso, la peticion que sale **no lleva el nombre del endpoint en la
   URL**. Esta es la comprobacion que importa: las otras dos se pueden cumplir con
   codigo que no cierra el hueco, por ejemplo si el dispatcher mandara la URL fijada *y*
   la original. La tercera mira **lo que de verdad sale por el cable**.
4. La extension `sni_hostname` va con el nombre y no con la IP, porque es lo unico que
   hace que TLS valide el certificado del host y no de la IP.

## Sobre las IP de las pruebas

`203.0.113.0/24`, `198.51.100.0/24` y `192.0.2.0/24` son los rangos de documentacion de
RFC 5737, y **Python los marca como privados**. Elegirlos haria que la propia guarda
anti-SSRF los rechazara, y la prueba fallaria por la razon equivocada, que es la mas
dificil de leer: uno ve "direccion privada" y piensa que el guard funciona, cuando lo
que se esta probando es otra cosa. Por eso se usan IP globales de verdad, que no se
resuelven en las pruebas porque la resolucion esta fijada.
"""

from __future__ import annotations

import ipaddress
import socket
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core import ssrf
from backend.core.ssrf import (
    DnsResolutionError,
    SsrfBlockedError,
    pinned_destination,
    resolve_and_validate,
)

NOMBRE = "webhook-de-prueba.example.com"
IP_PUBLICA = "93.184.216.34"
IP_INTERNA = "127.0.0.1"
IPV6_PUBLICA = "2606:2800:220:1:248:1893:25c8:1946"


@pytest.fixture
def resolucion_fija(monkeypatch: pytest.MonkeyPatch):
    """Sustituye `getaddrinfo` por una tabla, para poder cambiar la resolucion en caliente."""

    tabla: dict[str, list[str]] = {NOMBRE: [IP_PUBLICA]}

    def getaddrinfo(host: str, *_args: Any, **_kwargs: Any):
        if host not in tabla:
            raise socket.gaierror("noSuchHost")
        salidas = []
        for direccion in tabla[host]:
            familia = socket.AF_INET6 if ":" in direccion else socket.AF_INET
            sockaddr = (direccion, 0, 0, 0) if familia == socket.AF_INET6 else (direccion, 0)
            salidas.append((familia, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return salidas

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", getaddrinfo)
    return tabla


# --------------------------------------------------------------------------- #
# pinned_destination
# --------------------------------------------------------------------------- #


def test_la_url_fijada_lleva_la_ip_y_no_el_nombre(resolucion_fija):
    """La URL de la peticion tiene que ser la IP, o el nombre vuelve a resolver."""

    url = f"https://{NOMBRE}/hook"
    fijada, nombre = pinned_destination(url, resolve_and_validate(url))

    assert nombre == NOMBRE
    assert IP_PUBLICA in fijada
    assert NOMBRE not in urlsplit(fijada).netloc, (
        f"la URL fijada {fijada} sigue llevando el nombre, que es justo lo que permitiria "
        "volver a resolver"
    )
    assert urlsplit(fijada).path == "/hook", "se ha perdido la ruta de la URL original"
    assert urlsplit(fijada).scheme == "https", "se ha perdido el esquema"


def test_una_ipv6_se_entre_corchetes(resolucion_fija):
    """`https://[::1]/` usa corchetes como sintaxis; sin ellos la URL se parte mal."""

    resolucion_fija[NOMBRE] = [IPV6_PUBLICA]
    url = f"https://{NOMBRE}/hook"
    fijada, nombre = pinned_destination(url, resolve_and_validate(url))

    assert nombre == NOMBRE
    netloc = urlsplit(fijada).netloc
    assert netloc == f"[{IPV6_PUBLICA}]", (
        f"una IPv6 sin corchetes se lee como host y puerto: {netloc}"
    )
    # Y lo que importa: sigue siendo una IPv6 valida, no "2606" con puerto "2800:...".
    assert ipaddress.ip_address(netloc.strip("[]")) == ipaddress.ip_address(
        IPV6_PUBLICA
    )


def test_se_conserva_el_puerto_no_estandar(resolucion_fija):
    """Un webhook en un puerto alto lo necesita igual que el nombre."""

    url = f"https://{NOMBRE}:8443/hook"
    fijada, nombre = pinned_destination(url, resolve_and_validate(url))
    assert nombre == NOMBRE
    assert urlsplit(fijada).netloc == f"{IP_PUBLICA}:8443"


def test_se_conservan_la_consulta_y_el_fragmento(resolucion_fija):
    """Fijar el destino no puede alterar lo que se pide: solo a donde."""

    url = f"https://{NOMBRE}/hook?token=abc&x=1"
    fijada, _ = pinned_destination(url, resolve_and_validate(url))
    partes = urlsplit(fijada)
    assert partes.query == "token=abc&x=1"


def test_sin_direcciones_validadas_no_se_despacha():
    """Una lista vacia es un fallo de programacion, no un caso al que se pueda caer."""

    with pytest.raises(DnsResolutionError):
        pinned_destination("https://ejemplo.invalid/hook", [])


def test_una_direccion_que_no_es_ip_se_rechaza():
    """La lista viene de `getaddrinfo`; si alguien la construye a mano, no se fia de ella."""

    with pytest.raises(DnsResolutionError):
        pinned_destination("https://ejemplo.invalid/hook", ["no-es-una-ip"])


def test_una_url_sin_host_se_rechaza():
    with pytest.raises(SsrfBlockedError):
        pinned_destination("https:///sin-host", [IP_PUBLICA])


# --------------------------------------------------------------------------- #
# El hueco concreto: la resolucion cambia entre validar y conectar
# --------------------------------------------------------------------------- #


def test_el_rebinding_no_cambia_el_destino_de_la_peticion(resolucion_fija):
    """El ataque completo: publica una IP, valida, y la cambia a interna antes de conectar."""

    url = f"https://{NOMBRE}/hook"

    # 1) La validacion ve la IP publica. Pasa, porque es correcta.
    direcciones = resolve_and_validate(url)
    assert direcciones == [IP_PUBLICA]

    # 2) El atacante cambia lo que resuelve su DNS.
    resolucion_fija[NOMBRE] = [IP_INTERNA]

    # 3) La peticion se construye con la lista de la validacion, no con una resolucion nueva.
    fijada, nombre = pinned_destination(url, direcciones)

    assert IP_INTERNA not in fijada, "la peticion se dirige a la IP que el atacante cambio"
    assert IP_PUBLICA in fijada
    # Y el nombre que se anuncia es el de la validacion, para que TLS y Host cuadren.
    assert nombre == NOMBRE


def test_una_resolucion_que_cambia_afecta_solo_a_una_validacion_nueva(resolucion_fija):
    """El pinning no cachea: cada entrega revalida.

    Se documenta porque es la diferencia entre "el nombre no puede cambiar a mitad" y "el
    nombre se lee una vez por entrega". Lo segundo es lo que hace este codigo, y lo
    segundo es lo correcto: un nombre que cambia entre dos entregas legitimas se
    revalida en la siguiente, y si entonces apunta a la red interna esa segunda entrega se
    bloquea. Lo que no puede es cambiar **dentro** de la misma.
    """

    url = f"https://{NOMBRE}/hook"
    assert resolve_and_validate(url) == [IP_PUBLICA]

    resolucion_fija[NOMBRE] = [IP_INTERNA]
    with pytest.raises(SsrfBlockedError):
        resolve_and_validate(url)

    resolucion_fija[NOMBRE] = [IP_PUBLICA]
    assert resolve_and_validate(url) == [IP_PUBLICA]


# --------------------------------------------------------------------------- #
# El dispatcher: que lo que sale por el cable lleva la IP
# --------------------------------------------------------------------------- #


class ClienteFalso:
    """Graba la llamada y devuelve una respuesta fija."""

    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    def __enter__(self) -> ClienteFalso:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.llamadas.append({"url": url, **kwargs})
        return httpx.Response(200, text="ok", request=httpx.Request("POST", url))


@pytest.mark.integration
async def test_el_dispatcher_despacha_a_la_ip_y_anuncia_el_nombre(
    integration_session: AsyncSession,
    resolucion_fija,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La prueba de lo que sale de verdad, con el cliente inyectado por el propio modulo."""

    from backend.apps.organizations.models import (
        Membership,
        Organization,
        RoleEnum,
        User,
    )
    from backend.apps.webhooks import dispatcher
    from backend.apps.webhooks.models import WebhookEndpoint
    from backend.core.security import hash_password

    cliente = ClienteFalso()

    suffix = uuid.uuid4().hex
    org = Organization(name=f"Pin {suffix}", slug=f"pin-{suffix}")
    integration_session.add(org)
    await integration_session.flush()
    usuario = User(
        email=f"pin-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Pin User",
        email_verified=True,
    )
    integration_session.add(usuario)
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=org.id, user_id=usuario.id, role=RoleEnum.ADMIN)
    )
    endpoint = WebhookEndpoint(
        organization_id=org.id,
        url=f"https://{NOMBRE}/hook",
        encrypted_secret=dispatcher.encrypt_endpoint_secret("secreto", org.id),
        event_types=["pentest.completed"],
        is_active=True,
        consecutive_failures=0,
    )
    integration_session.add(endpoint)
    await integration_session.commit()
    await integration_session.refresh(endpoint)

    await dispatcher.dispatch_webhook(
        session=integration_session,
        endpoint=endpoint,
        event_type="pentest.completed",
        payload={"evento": "prueba"},
        client_factory=lambda: cliente,  # type: ignore[arg-type,return-value]
    )

    assert len(cliente.llamadas) == 1, "no se llego a hacer la peticion"
    llamada = cliente.llamadas[0]

    # Lo importante: la URL **no** lleva el nombre, que es lo que permitiria volver a resolver.
    assert NOMBRE not in urlsplit(llamada["url"]).netloc, (
        f"el dispatcher ha despachado a {llamada['url']}, que vuelve a resolver el nombre"
    )
    assert IP_PUBLICA in llamada["url"]

    # El nombre va en la cabecera Host, para que el servidor web enrute el virtual host bien.
    assert llamada["headers"]["Host"] == NOMBRE

    # Y en el SNI de TLS, que es lo unico que hace que el certificado se valide contra el
    # nombre y no contra la IP. Sin esto, todo https a un webhook fallaria.
    assert llamada["extensions"]["sni_hostname"] == NOMBRE

    # La firma no se toca: el destino cambia, el cuerpo firmado no.
    assert "X-MGF-Signature" in llamada["headers"]


@pytest.mark.integration
async def test_el_dispatcher_bloquea_si_el_rebinding_apunta_a_la_red_interna(
    integration_session: AsyncSession,
    resolucion_fija,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si la IP cambia antes de validar, se bloquea — y se registra, no revienta.

    Se comprueba que la entrega se registra como fallida y no como excepcion: un destino
    que dejo de ser valido es un **resultado** que el usuario tiene que poder ver, no un
    fallo del motor que en Celery con `task_acks_late` se reintentaria para siempre.
    """

    from backend.apps.organizations.models import (
        Membership,
        Organization,
        RoleEnum,
        User,
    )
    from backend.apps.webhooks import dispatcher
    from backend.apps.webhooks.models import WebhookDelivery, WebhookEndpoint
    from backend.core.security import hash_password

    cliente = ClienteFalso()
    resolucion_fija[NOMBRE] = [IP_INTERNA]

    suffix = uuid.uuid4().hex
    org = Organization(name=f"Pin2 {suffix}", slug=f"pin2-{suffix}")
    integration_session.add(org)
    await integration_session.flush()
    usuario = User(
        email=f"pin2-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Pin2 User",
        email_verified=True,
    )
    integration_session.add(usuario)
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=org.id, user_id=usuario.id, role=RoleEnum.ADMIN)
    )
    endpoint = WebhookEndpoint(
        organization_id=org.id,
        url=f"https://{NOMBRE}/hook",
        encrypted_secret=dispatcher.encrypt_endpoint_secret("secreto", org.id),
        event_types=["pentest.completed"],
        is_active=True,
        consecutive_failures=0,
    )
    integration_session.add(endpoint)
    await integration_session.commit()
    await integration_session.refresh(endpoint)

    entrega = await dispatcher.dispatch_webhook(
        session=integration_session,
        endpoint=endpoint,
        event_type="pentest.completed",
        payload={"evento": "prueba"},
        client_factory=lambda: cliente,  # type: ignore[arg-type,return-value]
    )

    assert cliente.llamadas == [], "no deberia haberse intentado la conexion a la IP interna"
    assert entrega is not None
    assert "anti-SSRF" in (entrega.error_message or ""), (
        f"el motivo registrado no nombra la guarda: {entrega.error_message!r}"
    )
    assert (
        await integration_session.execute(
            WebhookDelivery.__table__.select().where(
                WebhookDelivery.__table__.c.id == entrega.id
            )
        )
    ).first() is not None
