"""Verificación de propiedad de un dominio por registro DNS TXT.

## Por qué el cliente tiene que **resolver** y no consultar un servicio

Porque el objetivo de la verificación es responder una pregunta concreta: ¿puede este
workspace publicar un registro DNS en este nombre? La única forma de saberlo es preguntarle
al DNS del propio nombre. Un servicio de terceros que "verifique" por ti mide lo que ese
servicio ve desde su propia red, que puede ser distinta de la del cliente: un `CNAME` a un
CDN, un `ANYCAST`, o un proxy de salida distinto dan respuestas diferentes para la misma
configuración. Verificar desde aquí es verificar desde el lado del escáner, que es el que
después va a golpear los hosts.

## Por qué `asyncio.to_thread` y no una librería asíncrona

Porque `dnspython` es la librería estándar, ya está en el entorno, y su API es síncrona. Envolverla
en un hilo es la forma correcta de no bloquear el bucle de eventos **sin** añadir una
dependencia asíncrona que hace lo mismo con una API distinta.

La alternativa —no usar hilo y llamar directo— bloquearía el bucle durante los 5 segundos del
timeout. En un endpoint web con un solo worker eso es una caída de servicio, no una lentitud:
las demás peticiones dejan de servirse mientras este espera un DNS que no contesta.

## Por qué cada estado de DNS tiene su propio resultado

Porque un `NXDOMAIN` y un timeout dicen cosas distintas y el usuario tiene que poder
distinguirlas:

- **NXDOMAIN**: el nombre no existe. Casi siempre es un error de tecleo.
- **NoAnswer**: el nombre existe pero no tiene TXT. Es el estado normal de "todavía no lo
  he publicado", y es el que más se va a ver.
- **Timeout**: el DNS no contesta. Es un problema de red o del nombre, y reintentar en unos
  segundos suele funcionar.

Devolver un único "no verificado" para los tres hace que el usuario no sepa si tiene que
esperar, corregir el nombre o llamar a su proveedor. El tipo de resultado lo dice, y el panel
lo pinta.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import dns.exception
import dns.resolver

from backend.core.config import settings

logger = logging.getLogger(__name__)

#: Prefijo del **valor** del registro TXT. Es parte del contrato con el cliente: lo que se
#: publica tiene que empezar por esto, y `build_txt_record` produce exactamente la misma
#: cadena que se compara. Las dos usan esta constante para que no puedan separarse.
TXT_PREFIX: Final[str] = "fenix-domain-verify="

#: Etiqueta del **nombre** del registro, sin el dominio.
#:
#: El nombre y el valor son dos cosas distintas en DNS, y confundirlas produce un registro
#: que ningún proveedor acepta: el nombre tiene que ser una etiqueta válida —sin `=`, sin
#: barras— y el valor es texto libre. Un nombre como `fenix-domain-verify=fenix` no se puede
#: publicar en ninguna zona, y el usuario vería en el panel una instrucción que falla al
#: copiarla.
#:
#: Lleva guion bajo inicial porque es la convención que reserva un espacio de nombres al
#: propietario del dominio: los nombres con prefijo `_` son los que un tercero no debe usar
#: para sus servicios, así que no se solapa con un registro real de nadie.
#:
#: Viaja **relativo** a propósito. El panel lo concatena con el dominio para mostrar el
#: nombre completo, y hacerlo aquí fijaría un dominio de ejemplo en la constante.
TXT_RECORD_LABEL: Final[str] = "_fenix-verification"


def build_txt_record_name(domain: str) -> str:
    """El nombre completo del registro TXT para un dominio concreto.

    Se construye aquí y no en el panel por el mismo motivo que el valor: la cadena que se
    consulta y la que se enseña tienen que salir del mismo sitio, o divergen en cuanto uno
    de los dos cambie.
    """

    return f"{TXT_RECORD_LABEL}.{domain}"

#: Tope de la consulta, en segundos. Vive en `Settings` y **no** aquí: es una política
#: operativa —cuánto espera el panel a un DNS lento— y cambiarla no debería obligar a tocar
#: código ni a reinstalar. Se usa un valor por defecto *dentro* del plazo como red de
#: seguridad para que esta función siga siendo utilizable en un test que no construye
#: `Settings`, pero el valor real sale siempre de la configuración.
DEFAULT_DNS_TIMEOUT_SECONDS: Final[float] = 4.0

#: Margen sobre el tope del resolver para el corte del bucle de eventos. Un resolver bien
#: configurado respeta su propio `lifetime`; este margen cubre la latencia de despertar el
#: hilo. Sin él, el corte del bucle llegaría **antes** que el del resolver y se descartaría
#: una respuesta que estaba a punto de llegar, que es peor que esperar un segundo más.
TIMEOUT_MARGIN_SECONDS: Final[float] = 1.0

#: Servidores de nombres públicos que se consultan.
#:
#: Se declaran en vez de usar el `resolv.conf` del sistema porque en el contenedor del worker
#: ese fichero puede no existir o apuntar a un stub del propio runtime, y una verificación
#: que depende de la configuración de red de la máquina es una verificación que falla sin
#: explicación. Con los servidores públicos explícitos, el resultado depende del DNS y no
#: del entorno donde corrió el proceso.
PUBLIC_NAMESERVERS: Final[tuple[str, ...]] = ("1.1.1.1", "8.8.8.8")


#: Gramática de una etiqueta DNS **de servicio**, con guion bajo.
#:
#: Es distinta de la que se usa para validar el nombre de un dominio que alguien quiere
#: registrar, y la diferencia no es un detalle: un nombre de dominio no puede llevar `_`,
#: pero un registro TXT de servicio **sí**, porque es así como funcionan `_dmarc` y
#: `_domainkey`. Usar la regla estricta del dominio para la etiqueta de verificación
#: rechazaría un nombre que es perfectamente publicable.
#:
#: Vive aquí y no en `schemas` porque es la regla de **este** registro, no la del alta de
#: dominios: son dos espacios de nombres con reglas distintas y mezclarlos rechazaría el
#: propio registro de la plataforma.
SERVICIO_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")


def is_publishable_record_name(name: str) -> bool:
    """Si un nombre de registro se puede publicar en una zona real.

    Se usa para comprobar que la etiqueta de verificación es válida en el mismo sentido que
    lo será cuando el usuario la escriba en su panel de DNS. Un nombre que no se puede
    publicar produce un fallo que no aparece en ninguna respuesta de la API: el alta va
    bien, el panel muestra las instrucciones, y el error solo aflora cuando el cliente va a
    su proveedor.
    """

    etiquetas = name.split(".")
    if len(etiquetas) < 2:
        return False
    return all(SERVICIO_LABEL_PATTERN.match(etiqueta) for etiqueta in etiquetas)


def dns_timeout_seconds() -> float:
    """El tope de consulta configurado.

    Se lee en cada llamada y no en el import para que un test pueda cambiarlo sin recargar
    el módulo, y para que el valor no quede congelado en el proceso si se reconfigura.
    """

    return settings.asset_discovery_dns_timeout_seconds


class DnsLookupOutcome(StrEnum):
    """Qué respondió el DNS. Es el resultado, no un error: los tres son espera normal."""

    #: Se encontró el registro con el token correcto.
    MATCH = "MATCH"
    #: El nombre existe y tiene TXT, pero ninguno lleva el token.
    MISMATCH = "MISMATCH"
    #: El nombre existe y no tiene ningún registro TXT.
    NO_TXT = "NO_TXT"
    #: El nombre no existe en DNS.
    NXDOMAIN = "NXDOMAIN"
    #: El DNS no respondió dentro del tiempo.
    TIMEOUT = "TIMEOUT"
    #: El servidor de nombres devolvió un error de protocolo.
    ERROR = "ERROR"


class DomainVerificationError(RuntimeError):
    """Fallo controlado al verificar un dominio.

    Se reserva para lo que **no** debería ocurrir nunca con una entrada válida: un nombre mal
    formado que ha pasado el validador, o un resolver que no se puede construir. Los estados
    normales de DNS son `DnsLookupOutcome`, no excepciones.
    """


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """El resultado de consultar el TXT de un dominio."""

    outcome: DnsLookupOutcome
    #: Los valores TXT encontrados, sin recortar. Se devuelven para que el panel pueda
    #: mostrar lo que hay publicado cuando el resultado es `MISMATCH`: sin ellos, el usuario
    #: solo sabe que "no coincide" y no puede ver el token equivocado que tiene puesto.
    found_values: tuple[str, ...]
    #: El valor exacto que se esperaba. Viaja en la respuesta para que el panel pueda
    #: reintentar sin volver a pedirlo a la API.
    expected_value: str

    @property
    def verified(self) -> bool:
        """Si el dominio queda verificado."""

        return self.outcome is DnsLookupOutcome.MATCH

    def message_key(self) -> str:
        """Clave de i18n del mensaje, sin namespace.

    El mensaje vive en el namespace del panel y **no** en el backend, que no tiene
    traducciones. Devolver la clave y no el texto es lo que permite que el panel lo pinte en
    español o en inglés según el idioma activo.
        """

        return f"verify.outcome.{self.outcome.value}"


def build_txt_record(token: str) -> str:
    """El valor TXT completo que hay que publicar para este token."""

    return f"{TXT_PREFIX}{token}"


def _normalize_txt(value: str) -> str:
    """Limpia un valor TXT para compararlo.

    Se quitan las comillas que algunos resolvers incluyen y se recortan los espacios. El
    DNS no las considera parte del valor, pero un usuario que copia el contenido de un panel
    de su proveedor las pega, y comparar sin normalizar daría un falso `MISMATCH` con el
    token perfectamente publicado.
    """

    limpio = value.strip()
    if limpio.startswith('"') and limpio.endswith('"') and len(limpio) >= 2:
        limpio = limpio[1:-1]
    return limpio.strip()


def _query_sync(record_name: str) -> tuple[DnsLookupOutcome, tuple[str, ...]]:
    """Consulta los TXT de un nombre. **Síncrono**: se ejecuta en un hilo.

    ## Por qué devuelve el resultado **y** los valores

    Porque `NXDOMAIN` y "existe pero no tiene TXT" producen ambos una lista vacía, y
    confundirlos es exactamente el fallo que este módulo existe para evitar: el primero es un
    error de tecleo y el segundo es el estado normal de "todavía no lo he publicado". Con un
    solo valor vacío el usuario no sabría si corregir el nombre o esperar.

    Se construye un `Resolver` propio en lugar de usar el global porque el global de
    `dnspython` lee `/etc/resolv.conf`, que en un contenedor puede no existir o apuntar a un
    stub. Un resolver explícito con nameservers públicos es predecible.

    `raise_on_no_answer=False` convierte "existe y no hay TXT" en una lista vacía en lugar de
    una excepción. `NXDOMAIN` sí lanza, y se captura aquí para poder devolverlo como estado
    en vez de propagarlo.
    """

    plazo = dns_timeout_seconds()
    resolver = dns.resolver.Resolver(configure=True)
    resolver.nameservers = PUBLIC_NAMESERVERS
    resolver.timeout = plazo
    resolver.lifetime = plazo
    try:
        respuestas = resolver.resolve(record_name, "TXT", raise_on_no_answer=False)
    except dns.resolver.NXDOMAIN:
        return (DnsLookupOutcome.NXDOMAIN, ())
    return (
        DnsLookupOutcome.NO_TXT,
        tuple(_normalize_txt(respuesta.to_text()) for respuesta in respuestas),
    )


async def verify_domain_txt(domain: str, token: str) -> VerificationResult:
    """Consulta el TXT de `domain` y comprueba si lleva el token.

    ## Por qué consulta la etiqueta de verificación y no el dominio

    Porque es donde el cliente publica la prueba. Un TXT en la raíz del dominio publicaría el
    token, sí, pero lo haría en un espacio de nombres que el cliente comparte con sus
    servicios reales y donde un borrado accidental lo destruiría. La etiqueta `_fenix-
    verification` es un espacio propio: el cliente la crea, nadie más la usa, y borrarla no
    toca nada del sitio.

    Y no es un detalle cosmético: consultar el nombre equivocado devolvería `NO_TXT` con el
    token perfectamente publicado, y el usuario vería un "no coincide" sin ninguna forma de
    entender por qué.

    ## Por qué no propaga las excepciones de la librería

    Porque cada una de ellas significa algo distinto para el usuario y todas tienen un
    equivalente en `DnsLookupOutcome`. Dejar que escapen produciría un `500` para un dominio
    que simplemente no existe todavía, y el panel no podría distinguir "aún no lo he
    publicado" de "el servidor se ha caído" —que es la diferencia entre esperar y abrir un
    ticket—.

    El tope configurado es el tope de cada espera, y como la consulta va en un hilo, el
    timeout del bucle de eventos es el segundo corte: si el hilo se queda colgado, la
    petición tampoco. Un hilo que se cuelga se abandona con `asyncio.wait_for`, y la
    respuesta dice `TIMEOUT` igual que si el DNS no hubiera contestado, porque para el
    usuario es lo mismo.
    """

    esperado = build_txt_record(token)
    nombre_registro = build_txt_record_name(domain)
    # El plazo se lee **una vez** y se usa para el resolver y para el corte del bucle. Leerlo
    # dos veces dejaría una ventana en la que el valor cambia entre ambos y el corte podría
    # caer por debajo del `lifetime` del resolver, que es el orden que hace que la respuesta
    # que estaba a punto de llegar se descartase.
    plazo = dns_timeout_seconds()

    try:
        resultado, valores = await asyncio.wait_for(
            asyncio.to_thread(_query_sync, nombre_registro),
            timeout=plazo + TIMEOUT_MARGIN_SECONDS,
        )
    except TimeoutError:
        logger.info("La consulta TXT de %s se agoto", nombre_registro)
        return VerificationResult(DnsLookupOutcome.TIMEOUT, (), esperado)
    except (dns.resolver.NoNameservers, dns.resolver.LifetimeTimeout) as error:
        logger.info("El DNS no respondio para %s: %s", nombre_registro, error)
        return VerificationResult(DnsLookupOutcome.TIMEOUT, (), esperado)
    except dns.exception.DNSException as error:
        logger.warning("Error de DNS inesperado para %s: %s", nombre_registro, error)
        return VerificationResult(DnsLookupOutcome.ERROR, (), esperado)

    # `NXDOMAIN` se propaga tal cual, y es una línea propia y no un `if` combinado con el
    # resto. Es el único resultado que **no** depende de la lista de valores, así que
    # dejarlo para el final lo convertía en `MISMATCH`: el usuario que teclea mal el nombre
    # veía "el token no coincide" con la lista de valores encontrada vacía, que es un
    # mensaje que no lleva a ninguna acción. Con `NXDOMAIN` el panel puede decir "ese
    # nombre no existe".
    if resultado is DnsLookupOutcome.NXDOMAIN:
        return VerificationResult(DnsLookupOutcome.NXDOMAIN, (), esperado)
    if not valores:
        return VerificationResult(DnsLookupOutcome.NO_TXT, (), esperado)
    if esperado in valores:
        return VerificationResult(DnsLookupOutcome.MATCH, valores, esperado)
    return VerificationResult(DnsLookupOutcome.MISMATCH, valores, esperado)


__all__ = [
    "PUBLIC_NAMESERVERS",
    "SERVICIO_LABEL_PATTERN",
    "TIMEOUT_MARGIN_SECONDS",
    "TXT_PREFIX",
    "TXT_RECORD_LABEL",
    "DnsLookupOutcome",
    "DomainVerificationError",
    "VerificationResult",
    "build_txt_record",
    "build_txt_record_name",
    "dns_timeout_seconds",
    "is_publishable_record_name",
    "verify_domain_txt",
]
