"""Descubrimiento de la superficie de ataque de un dominio verificado.

## La línea que este módulo no cruza: solo DNS

Cada activo que se guarda aquí se obtuvo **preguntando al DNS**. El proceso no abre una
conexión ni un paquete TCP a ningún host descubierto. Eso no es una limitación que se haya
elegido por comodidad: es la frontera entre *descubrir* y *atacar*.

Descubrir es enumerar los nombres que el cliente tiene publicados. Atacar es enviar tráfico
a un host que no se ha pedido. La primera se autoriza con la verificación de propiedad del
dominio, que es exactamente lo que esa verificación significa: "este workspace controla
estos nombres y pide saber cuáles existen". La segunda necesita una orden de pentest
explícita, y esa orden se ejecuta en un contenedor efímero y aislado por R3, no desde el
proceso de la API.

Si esta función conectara con lo que encuentra, un `POST /discover` bastaría para que un
cliente escaneara la red de otro, sabiendo que su nombre de DNS se resolvió. El resultado
de un `A` record es que **existe**; que exista no es permiso para contactarlo.

Por eso aquí no hay `httpx` ni sockets: hay `dnspython`. Y por eso el descubrimiento exige
`is_verified == True` y no es solo una formalidad: es la prueba de que quien pide la
enumeración es el dueño del nombre que se va a enumerar.

## Por qué una lista de candidatos y no un diccionario masivo

Porque el tamaño de la lista es el tamaño de la superficie que se puede enumerar, y porque
esta ejecución sale del proceso del API: sale de un worker de Celery con un tiempo limitado.
Un diccionario de 200.000 entradas contra un dominio ajeno no es un escaneo, es un
denegio de servicio dirigido, y además tardaría más que el tope blando de la tarea.

La lista es configurable por `ASSET_DISCOVERY_WORDLIST` porque R1 saca del código todo lo que
sea política. Lo que no se permite es que venga de una carga arbitraria del usuario: es un
nombre de campo del despliegue, no un parámetro de la petición, para que un cliente no pueda
convertir esta tarea en el vector que el tamaño de la lista controla.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Final

import dns.exception
import dns.resolver

from backend.apps.assets.models import AssetTypeEnum
from backend.apps.assets.verifier import PUBLIC_NAMESERVERS
from backend.core.config import settings

logger = logging.getLogger(__name__)

#: Registros que se consultan para cada candidato. `A` y `AAAA` dicen si el nombre existe y
#: qué direcciones tiene; `CNAME` delata la externalización típica —un subdominio detrás de un
#: CDN o de un SaaS—, que es uno de los hallazgos más útiles y no aparece en el `A`.
DNS_RECORD_TYPES: Final[tuple[str, ...]] = ("A", "AAAA", "CNAME")


@dataclass(frozen=True, slots=True)
class ResolvedHost:
    """Un nombre que existe, con lo que el DNS devolvió para él."""

    fqdn: str
    addresses: tuple[str, ...]
    cname: str | None

    @property
    def has_address(self) -> bool:
        """Si el nombre resuelve a alguna dirección.

        Un `CNAME` sin `A` final no cuenta. Aparece en la consulta intermedia, no en el
        resultado, y registrarlo como activo sería meter en el inventario un nombre que el
        cliente no controla directamente y que además puede no resolver nunca.
        """

        return bool(self.addresses)


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """Lo que encontró una ejecución de descubrimiento.

    Viaja de vuelta al worker y de ahí al log, no al panel: el panel no muestra el informe,
    muestra el inventario. Existir aparte es lo que permite que el log diga "de 64 candidatos
    resolveron 12" sin tener que contar filas en la base de datos para después de haber
    escrito en ella.
    """

    candidates_tried: int
    hosts: tuple[ResolvedHost, ...] = field(default=())
    timed_out: int = 0
    errors: int = 0

    @property
    def resolved(self) -> int:
        """Cuántos candidatos resolvieron a alguna dirección."""

        return sum(1 for host in self.hosts if host.has_address)


def candidate_prefixes(
    wordlist: str | None = None, max_candidates: int | None = None
) -> tuple[str, ...]:
    """Los prefijos a probar, de la configuración y acotados.

    ## Por qué acepta la lista y el tope como parámetros

    Porque `Settings` es **inmutable**: no se puede cambiar en caliente, ni siquiera en una
    prueba. Meter los valores por parámetro hace dos cosas a la vez: deja al llamador de
    producción sin argumentos —que es como debe llamarse— y permite a una prueba ejercitar
    el recorte sin `monkeypatch` sobre un objeto congelado.

    La alternativa, leer siempre del global y relajar el congelado para poder escribir en él,
    debilitaría una garantía que existe por razones reales: que la configuración no cambie a
    mitad de una petición.

    Se recortan **conservando el orden** de la configuración, no de forma aleatoria: el
    orden es el de prioridad que escribió quien despliega, y recortarlo por sorpresa
    descartaría justo los prefijos que aparecen primero.
    """

    bruto = wordlist if wordlist is not None else settings.asset_discovery_wordlist
    separador = "," if "," in bruto else None
    piezas = bruto.split(separador) if separador else bruto.split()
    normalizados = tuple(
        dict.fromkeys(
            pieza.strip().strip(".").lower()
            for pieza in piezas
            if pieza.strip() and pieza.strip() != "*"
        )
    )
    tope = (
        max_candidates
        if max_candidates is not None
        else settings.asset_discovery_max_candidates
    )
    return normalizados[:tope]


def _resolver_sync(
    fqdn: str, record_type: str, plazo: float
) -> tuple[tuple[str, ...], str | None]:
    """Consulta un registro en un hilo. Devuelve `(direcciones, cname)`.

    Separar el `CNAME` del resto es lo que permite que el llamante decida qué hacer con el
    destino en lugar de perder la información: un `CNAME` a un Proveedor conocido es un
    hallazgo, no un error.
    """

    resolver = dns.resolver.Resolver(configure=True)
    resolver.nameservers = PUBLIC_NAMESERVERS
    resolver.timeout = plazo
    resolver.lifetime = plazo
    try:
        respuestas = resolver.resolve(fqdn, record_type, raise_on_no_answer=False)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return (), None
    except dns.exception.DNSException:
        return (), None

    cname: str | None = None
    textos = [respuesta.to_text().strip().rstrip(".") for respuesta in respuestas]
    if record_type == "CNAME" and textos:
        cname = textos[0]
    return tuple(textos), cname


def _es_ip(valor: str) -> bool:
    """Si el valor es una dirección IP y no un nombre.

    Un `A` devuelve IPs, un `AAAA` también, y un `CNAME` devuelve nombres. Guardar un nombre
    en la columna `value` de un activo de tipo `IP_ADDRESS` produciría un inventario con IPs
    que no son IPs, y cualquier consumidor que lo consuma esperando una dirección fallaría.
    """

    try:
        ipaddress.ip_address(valor)
    except ValueError:
        return False
    return True


async def resolve_candidate(fqdn: str, plazo: float) -> ResolvedHost | None:
    """Resuelve un candidato. `None` si no existe o si el DNS falló.

    Devuelve `None` en ambos casos a propósito: un nombre inexistente y un DNS que no
    contesta son, para el inventario, lo mismo —no hay activo— y distinguirlos aquí
    obligaría a inventar un tipo de activo que significa "hubo un problema de red", que no
    es parte de la superficie de ataque del cliente.
    """

    direcciones: list[str] = []
    cname: str | None = None

    for record_type in DNS_RECORD_TYPES:
        try:
            textos, destino = await asyncio.wait_for(
                asyncio.to_thread(_resolver_sync, fqdn, record_type, plazo),
                timeout=plazo + 1.0,
            )
        except TimeoutError:
            return None
        except dns.exception.DNSException:
            return None
        if destino is not None and cname is None:
            cname = destino
        direcciones.extend(texto for texto in textos if _es_ip(texto))

    if not direcciones and cname is None:
        return None
    return ResolvedHost(
        fqdn=fqdn,
        addresses=tuple(dict.fromkeys(direcciones)),
        cname=cname,
    )


async def discover(domain_name: str) -> DiscoveryReport:
    """Enumera los subdominios de un dominio **verificado** por resolución DNS.

    ## Por qué la concurrencia está acotada y es configurable

    Porque 64 candidatos contra un resolver son 64 consultas, y hacerlas en serie con un
    plazo de 4 segundos cada una son hasta 256 segundos: más que el tope blando de la tarea,
    que la mataría y dejaría `last_scanned_at` sin tocar. Con concurrencia acotada caben en
    un solo ciclo de reintentos del resolver y la tarea termina con margen.

    El límite superior existe por la misma razón que en un escaneo: un resolver público
    corta las consultas por origen. Mandarle 1.000 a la vez no acelera el descubrimiento,
    lo hace fallar, y además es exactamente el patrón que un ataque de amplificación
    intenta provocar.

    ## Por qué los candidatos que fallan no se registran

    Porque un nombre que no resuelve no es un activo. Registrarlos llenaría el inventario de
    ruido y, peor, haría que la columna `value` dejara de ser una descripción de algo que
    existe. El informe de la ejecución sí cuenta los intentos, que es donde esa
    información aporta.
    """

    prefijos = candidate_prefixes()
    if not prefijos:
        logger.warning(
            "Descubrimiento de %s sin candidatos: ASSET_DISCOVERY_WORDLIST esta vacia",
            domain_name,
        )
        return DiscoveryReport(candidates_tried=0)

    plazo = settings.asset_discovery_dns_timeout_seconds
    concurrencia = settings.asset_discovery_dns_concurrency
    semaforo = asyncio.Semaphore(concurrencia)

    async def _uno(prefijo: str) -> ResolvedHost | None:
        fqdn = f"{prefijo}.{domain_name}"
        async with semaforo:
            return await resolve_candidate(fqdn, plazo)

    resultados = await asyncio.gather(
        *(_uno(prefijo) for prefijo in prefijos), return_exceptions=True
    )

    hosts: list[ResolvedHost] = []
    timeouts = 0
    errores = 0
    for prefijo, resultado in zip(prefijos, resultados, strict=True):
        if isinstance(resultado, ResolvedHost):
            hosts.append(resultado)
        elif isinstance(resultado, TimeoutError):
            timeouts += 1
        elif isinstance(resultado, BaseException):
            errores += 1
            logger.warning(
                "Fallo al resolver %s.%s: %s", prefijo, domain_name, resultado
            )

    return DiscoveryReport(
        candidates_tried=len(prefijos),
        hosts=tuple(hosts),
        timed_out=timeouts,
        errors=errores,
    )


def report_to_assets(
    report: DiscoveryReport,
) -> list[tuple[AssetTypeEnum, str, str | None, list[str]]]:
    """Convierte el informe en filas de activo listas para `merge_assets`.

    Cada host con dirección produce **dos** activos: el subdominio y cada una de sus
    direcciones. No son la misma cosa y el cliente las cuenta distinto —el subdominio es
    una superficie expuesta, la IP es una máquina que puede servir varios nombres—, así que
    colapsarlas en una fila perdería una de las dos.

    El `CNAME` no se guarda como activo propio. Es un **atributo**: encaja en la lista de
    tecnologías del subdominio que lo declara, y como activo suelto sería una fila más que
    el usuario tendría que filtrar para entender que en realidad es un alias.
    """

    filas: list[tuple[AssetTypeEnum, str, str | None, list[str]]] = []
    for host in report.hosts:
        if not host.has_address:
            continue
        tecnologias = [f"CNAME:{host.cname}"] if host.cname else []
        filas.append(
            (
                AssetTypeEnum.SUBDOMAIN,
                host.fqdn,
                None,
                tecnologias,
            )
        )
        for address in host.addresses:
            filas.append(
                (
                    AssetTypeEnum.IP_ADDRESS,
                    address,
                    None,
                    [f"CNAME:{host.fqdn}"],
                )
            )
    return filas


__all__ = [
    "DNS_RECORD_TYPES",
    "DiscoveryReport",
    "ResolvedHost",
    "candidate_prefixes",
    "discover",
    "report_to_assets",
    "resolve_candidate",
]
