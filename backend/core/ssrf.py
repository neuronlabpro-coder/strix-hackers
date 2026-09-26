"""Guarda anti-SSRF para los endpoints de webhook.

Un webhook saliente es una petición que **la plataforma** hace en nombre de quien
registró la URL, y sale de una red que tiene salida a la interna: PostgreSQL en la red de
Tailscale, Redis, los metadatos de la nube. Sin esta guarda, registrar un endpoint es
convertir la API en un escáner de red: cualquiera con permiso de crear un webhook puede
usar la plataforma para leer `169.254.169.254` y llevarse las credenciales de la instancia
en la respuesta.

## Qué defendse y qué no

**Defiendes:**

- Rangos privados y de loopback, en IPv4 e IPv6, incluidos los IPv4 mapeados a IPv6
  (`::ffff:127.0.0.1`), que son la vía clásica de salto.
- Direcciones de metadatos de nube, por si la lista de rangos no las cubre.
- El bucle local **resuelto por DNS**: un nombre público que resuelve a `127.0.0.1` es
  un ataque, no un nombre inocente.
- Redirecciones. Un `302` a una URL interna es el camino más fácil de saltarse la
  validación de la URL inicial, así que **no se siguen** y la redirección se registra
  como el resultado.
- Tiempo y tamaño de respuesta, para que un endpoint lento o enorme no retenga al worker.

**No defiendes, y conviene saberlo:**

- **DNS rebinding puro.** Se resuelve y se valida antes de conectar, y entre la validación
  y la conexión el nombre podría resolver a otra IP. Cerrarlo del todo exige fijar la IP
  en la conexión y manejar SNI y `Host` por separado, que es más frágil que el hueco
  que cierra: pierde validación de certificado para los hosts que lo necesiten. Se
  asume ese riesgo residual y se anota aquí en vez de fingir que está cubierto.
- Que un endpoint activo **sí** reciba tráfico de salida legítimo. Lo que se impide es
  apuntarlo a la red interna, no usarlo.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlsplit

from backend.core.config import settings

#: Ambientes donde se admite `http://`. Coincide con la convención que ya usa el proyecto
#: en `clients/base.py`: el cifrado se exige en staging y producción, no en local.
RELAXED_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"development", "test"})

#: Direcciones de metadatos de nube. Se listan aparte de los rangos porque no caben en
#: ninguna redRFC1918: son publicas y sin embargo apuntan a la configuracion de la
#: instancia. `169.254.169.254` es la de AWS y GCP, y es la que más se busca.
METADATA_ADDRESSES: Final[frozenset[str]] = frozenset(
    {
        "169.254.169.254",  # AWS, GCP, Azure, DigitalOcean, OpenStack
        "169.254.170.2",  # ECS: credenciales de tarea
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud: metadata de infraestructura
    }
)

#: Nombres que ni se intentan resolver. Resolverlos ya es un comportamiento de red
#: innecesario, y algunos de ellos barreñan una red entera en vez de devolver una IP.
BLOCKED_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

#: Tamaño máximo de la respuesta que se guarda. Un endpoint que devuelve 10 MB haría que
#: cada entrega ocupara 10 MB en la base para nada: lo que el usuario necesita ver es si
#: el evento llegó y con qué código.
MAX_RESPONSE_BODY_CHARS: Final[int] = 1024

#: Tiempo máximo por intento. Cinco segundos es lo que acepta la especificación, y
#: acumularlo con tres reintentos deja el peor caso en quince, que es lo que un worker
#: puede caber en su sitio.
DELIVERY_TIMEOUT_SECONDS: Final[float] = 5.0


class SsrfBlockedError(ValueError):
    """La URL apunta, o podría apuntar, a la red interna.

    Es un `ValueError` y no un `HTTPException` porque el validador no sabe nada de HTTP;
    el router lo traduce a `422`.
    """


class DnsResolutionError(ValueError):
    """El nombre de host no se pudo resolver.

    Distinto de `SsrfBlockedError` a propósito: un nombre inexistente es un error de
    tecleo del cliente, y un `422` distinto permite decir "no encontramos ese host" en
    vez del rechazo genérico, que no ayuda a nadie a corregir nada.
    """


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Devuelve el motivo del bloqueo, o `None` si la dirección es válida como destino.

    Se comprueban las propiedades de `ipaddress` en vez de comparar prefijos a mano
    porque la librería ya sabe qué es privado, qué es loopback, qué es enlazado local y
    qué es reservado, y una lista escrita a mano se queda desfasada en cuanto aparece un
    caso raro. La comprobación explícita de IPv4 mapeados en IPv6 es la excepción: la
    trata `::ffff:127.0.0.1` como un IPv6 global, y ese es justo el salto que se busca.
    """

    if address.version == 6:
        mapeado = address.ipv4_mapped
        if mapeado is not None:
            return _motivo_ipv4(mapeado)
        # Teredo (2001::/32) y 6to4 (2002::/16) envuelven IPv4 en IPv6. Se rechazan
        # enteros: un endpoint de webhook no va a Teredo, y aceptarlos abriría un camino
        # al rango privado envuelto.
        if address.teredo is not None:
            return "dirección Teredo (IPv4 encapsulado)"
        if address.sixtofour is not None:
            return "dirección 6to4 (IPv4 encapsulado)"

    # La lista de metadatos se comprueba **antes** que las categorías de `ipaddress`, y no
    # por gusto. `169.254.169.254` es enlace local y Python lo marca también como privado,
    # así que si se preguntara por las categorías primero el usuario recibiría "dirección
    # privada" y buscaría un router o un NAT en su empresa. El nombre correcto es el que
    # le hace reconocer en un segundo que ha apuntando a la metadata de la nube.
    if str(address) in METADATA_ADDRESSES:
        return "dirección de metadatos de nube"

    if address.is_loopback:
        return "dirección de bucle local"
    if address.is_private:
        return "dirección privada"
    if address.is_link_local:
        return "dirección de enlace local"
    if address.is_multicast:
        return "dirección multicast"
    if address.is_reserved:
        return "dirección reservada"
    if address.is_unspecified:
        return "dirección no especificada"
    return None


def _motivo_ipv4(address: ipaddress.IPv4Address) -> str | None:
    if str(address) in METADATA_ADDRESSES:
        return "dirección de metadatos de nube"
    if address.is_loopback:
        return "dirección de bucle local"
    if address.is_private:
        return "dirección privada"
    if address.is_link_local:
        return "dirección de enlace local"
    if address.is_reserved or address.is_unspecified or address.is_multicast:
        return "dirección reservada o no enrutable"
    return None


def resolve_and_validate(url: str) -> list[str]:
    """Valida una URL de destino y devuelve las IP a las que se puede conectar.

    Resuelve **todas** las direcciones del nombre, no solo la primera. Un nombre con
    varias entradas A puede devolver una pública y otra privada, y dependiendo de cuál
    resuelva el cliente acabaría hablando con la interna. Se rechazan si **alguna** es
    válida, que es la única lectura segura cuando el cliente puede elegir.

    Se devuelve la lista para que quien despacha pueda comprobar que la conexión fue a una
    de las direcciones que se validaron.
    """

    partes = urlsplit(url)
    esquema = partes.scheme.lower()

    # El esquema se comprueba **antes** que el host. `file:///etc/passwd` no tiene host y
    # `data:...` tampoco, así que comprobarlo al revés respondería "la URL no tiene host"
    # a un usuario que lo que escribió fue un esquema no admitido. El error más específico
    # es el que dice qué corregir.
    if esquema not in {"http", "https"}:
        raise SsrfBlockedError("Solo se admiten las URL http y https")

    if not partes.netloc:
        raise SsrfBlockedError("La URL no tiene host")
    if not partes.hostname:
        raise SsrfBlockedError("La URL no tiene un host válido")

    # Una IPv6 sin corchetes hace que `urlsplit` lea el primer grupo como host y el resto
    # como puerto: `https://fc00::1/` se convierte en host `fc00` y puerto `:1`. No es un
    # bypass —el nombre acabaría en el DNS y fallaría— pero deja ambiguo si lo que se
    # escribió era una dirección o un dominio, y una ambigüedad en un validador de
    # seguridad se cierra rechazando en vez de suponiendo.
    if ":" in partes.netloc and not partes.netloc.startswith("["):
        if partes.netloc.count(":") > 1:
            raise SsrfBlockedError(
                "Las direcciones IPv6 deben ir entre corchetes, como en https://[::1]/"
            )

    if esquema == "http" and settings.environment not in RELAXED_ENVIRONMENTS:
        raise SsrfBlockedError(
            "Las URL en http:// solo se admiten en desarrollo; en "
            f"{settings.environment} el trafico debe ir cifrado con https://"
        )

    host = partes.hostname.rstrip(".").lower()
    if host in BLOCKED_HOSTNAMES:
        raise SsrfBlockedError(f"El host {host} no puede usarse como destino")

    # Una IP literal se valida sin pasar por DNS. Es el caso que un atacante controla por
    # completo, así que es el que no puede depender de una resolucion posterior.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        motivo = _is_blocked_address(literal)
        if motivo is not None:
            raise SsrfBlockedError(f"El destino {host} esta bloqueado: {motivo}")
        return [str(literal)]

    try:
        informacion = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise DnsResolutionError(f"No se pudo resolver el host {host}") from error

    direcciones: list[str] = []
    for familia, _, _, _, sockaddr in informacion:
        try:
            direccion = ipaddress.ip_address(sockaddr[0])
        except ValueError:  # pragma: no cover - getaddrinfo no devuelve esto
            continue
        if familia not in {socket.AF_INET, socket.AF_INET6}:
            continue
        motivo = _is_blocked_address(direccion)
        if motivo is not None:
            # Se nombra la direccion concreta: decir solo "el host esta bloqueado"
            # lleva al usuario a revisar el nombre en vez de la resolucion, que es donde
            # esta casi siempre.
            raise SsrfBlockedError(
                f"El host {host} resuelve a {direccion}, que esta bloqueado: {motivo}"
            )
        if str(direccion) not in direcciones:
            direcciones.append(str(direccion))

    if not direcciones:
        raise DnsResolutionError(f"El host {host} no resuelve a ninguna direccion valida")
    return direcciones


def ensure_delivery_url_allowed(url: str) -> None:
    """Valida una URL justo antes de despachar, no solo al registrarla.

    Una URL aceptada hace seis meses puede haber cambiado de IP. Revalidar en el momento
    del envío cuesta una resolución DNS y es la unica vez que la comprobacion afecta de
    verdad a la peticion que sale. Sin esto, el registro no protege nada: se registra una
    IP publica y meses despues el mismo nombre apunta a la red interna.
    """

    resolve_and_validate(url)


def truncate_response(body: str) -> str:
    """Recorta el cuerpo de respuesta a lo que se guarda y se muestra.

    Se anade un marcador al final cuando hubo que recortar, para que nadie lea un cuerpo
    truncado como si fuera completo. Un error de servidor con la traza entera no cabe en
    una columna, y una truncacion silenciosa hace que el usuario busque en el JSON un
    mensaje que no esta porque se comio el limite.
    """

    if len(body) <= MAX_RESPONSE_BODY_CHARS:
        return body
    return f"{body[:MAX_RESPONSE_BODY_CHARS]}…[truncado]"


def is_redirect(status_code: int) -> bool:
    """`True` si la respuesta es una redireccion que **no** se va a seguir."""

    return status_code in {301, 302, 303, 307, 308}
