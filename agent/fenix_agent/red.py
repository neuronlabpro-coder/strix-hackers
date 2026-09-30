"""El único punto por el que el agente abre una conexión, y todo lo que se le niega.

## Por qué este módulo existe

Porque `agent/` es el componente menos confiable de todo el sistema, por diseño: corre **dentro
de la red del cliente**, no lleva credenciales de la plataforma con poder real, y su entrada no
es el operador sino **el objetivo que se está escaneando**. Cualquier dato que entre por aquí
después viene de la red que el cliente no controla.

Y hay tres formas de que eso se convierta en un problema, las tres presentes antes de este
módulo:

1. **La cabecera `WWW-Authenticate` de un registro elegido por el usuario.** El `realm` de esa
   cabecera se usaba para construir la siguiente petición **sin validar nada**. El registro lo
   elige quien encola el escaneo, así que el `realm` también. Con eso, un escaneo de contenedor
   podía hacer que el agente emitiera un `GET` —desde dentro de la red del cliente— a
   `169.254.169.254` o a un `10.x` cualquiera, y además lo cargara entero en memoria.

2. **`urlopen` global.** El opener por defecto de `urllib` registra `FileHandler` y `FTPHandler`,
   de modo que el esquema no estaba acotado a `{http, https}`, y trae `HTTPRedirectHandler`, que
   **sí sigue redirecciones**. Con eso, un `302` desde un `realm` público bastaba, y en
   `plataforma.py` el `Authorization` del token de agente viajaba al host de destino del `3xx`
   porque CPython reconstruye la petición copiando todas las cabeceras salvo
   `content-length` y `content-type`.

3. **`read()` sin tope**, mientras que `plataforma.py` ya tenía `MAX_CUERPO` para lo mismo. Un
   registro podía devolver cientos de megabytes y el agente los cargaba en la RAM de la máquina
   del cliente.

## Qué se hace aquí

Un solo lugar por el que sale una petición, con cuatro restricciones que **no son opcionales** y
que se pueden desactivar con una constante solo para las pruebas:

- **Esquema** limitado a `http` y `https`. `file://` y `ftp://` no existen aquí.
- **Redondeo de la IP** antes de conectar, y rechazo de loopback, enlace-local, privada,
  reservada y multicast. La comprobación es sobre **todas** las direcciones que devuelve
  `getaddrinfo`, no sobre la primera: un nombre que resuelve a una pública y a una privada es el
  caso que hay que cerrar, y descartar solo la primera lo deja abierto.
- **Sin redirecciones**, y sin reenviar `Authorization` a ningún host distinto del declarado.
- **Cuerpo acotado**, leído por trozos y no con `read()` a pelo.

## Por qué no se reutiliza `backend/core/ssrf.py`

Porque `agent/` no puede importar de `backend/`. El agente se despliega **dentro de la red del
cliente**, sin el backend, y no puede llevar el paquete de la plataforma encima: ese es el punto
de que el razonamiento ocurra en la plataforma y no ahí. Reutilizar el código exigiría que el
agente arrastrase la aplicación entera.

La consecuencia honesta es que hay dos implementaciones de la misma defensa, y que pueden
divergir. Se acepta: la alternativa —un agente que importe el backend— rompe el aislamiento que
justifica el agente. Lo que **no** se acepta es que la copia sea más débil que el original, y
por eso este módulo tiene pruebas que la original ya tenía.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from typing import Any, Final
from urllib.parse import urlparse

#: Esquemas que el agente puede abrir. `file`, `ftp` y `data` quedan fuera a propósito: el
#: opener global de `urllib` los trae registrados y `file://` lee el disco de la máquina del
#: cliente.
ESQUEMAS_PERMITIDOS: Final[frozenset[str]] = frozenset({"http", "https"})

#: Tope del cuerpo de una respuesta del registro.
#:
#: ## Por qué 32 MB y no 256 KB
#:
#: Porque aquí la respuesta legítima es **la lista de blobs de una imagen**, que es lo más
#: grande que se pide: una capa comprimida son decenas de megabytes. Un tope pequeño convertiría
#: un escaneo legítimo en un fallo.
#:
#: Y porque es el mismo número que ya usa `plataforma.py` para las respuestas de la plataforma.
#: Que las dos lecturas tuvieran topes distintos era el defecto: el que faltaba era el
#: peligroso, y tenerlos iguales hace que "tengo un tope" sea una afirmación del agente entero y
#: no de cada módulo por separado.
MAX_CUERPO: Final[int] = 32 * 1024 * 1024

#: Hosts a los que **si** se permite abrir, aunque resuelvan a una IP privada.
#:
#: ## Por que existe esto y no es un interruptor
#:
#: Porque la plataforma del cliente puede estar legitimamente en su red privada —un despliegue
#: con la API en `10.x` es normal— y un filtro que no lo permitiera obligaria a desactivar la
#: comprobacion entera, que es justo lo que no se quiere.
#:
#: Pero el valor de esta lista **no lo elige quien encola el escaneo**: lo elige el operador, en
#: el fichero de configuracion del agente, y solo puede contener el host de la plataforma. El
#: `realm` de un registro controlado por un atacante nunca esta en ella, porque no hay forma de
#: que lo estuviera: sale de la red en el momento del escaneo, y la lista se fija al arrancar.
#:
#: ## Por que no es un interruptor
#:
#: Porque un interruptor es un `False` que alguien pone y se olvida de quitar, y en un despliegue
#: real acabaria puesto. Una lista de un host es mas estrecha: activar el equivocado cuesta mas,
#: y activar el correcto es un cambio de configuracion visible en un fichero de texto.
_ENTIDADES_PERMITIDAS: set[str] = set()


def declarar_entidades_permitidas(hosts: object) -> None:
    """Fija la lista de hosts cuya IP privada se permite alcanzar.

    La llama `config` **al arrancar**, con el host de la plataforma, y solo con ese. El filtro se
    vuelve a comprobar en cada peticion, asi que un host retirado deja de estar permitido en la
    peticion siguiente y no en la siguiente ejecucion.
    """

    _ENTIDADES_PERMITIDAS.clear()
    if isinstance(hosts, str):
        hosts = [hosts]
    for host in hosts or ():
        normalizado = str(host).strip().lower()
        if normalizado:
            _ENTIDADES_PERMITIDAS.add(normalizado)


def _permitido_explicitamente(host: str) -> bool:
    return host.lower() in _ENTIDADES_PERMITIDAS


class DestinoNoPermitido(ValueError):
    """La URL apunta a algo que este agente no puede abrir.

    Es una excepción propia y no `ErrorDeEscaneo` a propósito: quien la captura —el bucle— la
    trata como un fallo del trabajo, pero el motivo es **una decisión de diseño del agente**, no
    un error de red que el usuario pueda arreglar cambiando la referencia de la imagen. Mezclarla
    con `ErrorDeEscaneo` haría que el mensaje dijera «el registro no tiene esa imagen» cuando lo
    que pasó es que el registro pidió un destino que este agente no abre.
    """


def _es_ip_privada(direccion: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Si la dirección es de una red que no debe alcanzarse desde un escaneo.

    ## Por qué el orden de las comprobaciones no es arbitrario

    Porque hay una familia entera que se cuela si se empieza por la equivocada. Una dirección
    IPv4 mapeada en IPv6 —`::ffff:127.0.0.1`— dice `is_private == False` en la vista IPv6, y
    `169.254.169.254` —los metadatos de la nube— dice `is_private == False` también, porque
    `is_private` cubre los bloques privados de clase A/B/C y no el enlace-local. Por eso el
    enlace-local se mira **aparte** y antes.
    """

    return bool(
        direccion.is_loopback
        or direccion.is_link_local
        or direccion.is_private
        or direccion.is_reserved
        or direccion.is_multicast
        or direccion.is_unspecified
    )


def _resolver_y_comprobar(host: str, puerto: int) -> None:
    """Resuelve `host` y rechaza si **alguna** de sus direcciones es privada.

    ## Por qué se rechaza el conjunto y no una dirección

    Porque el ataque que esto cierra es el rebinding: un nombre cuyo DNS devuelve una IP pública
    en la primera consulta —la que ve el filtro— y una privada en la segunda, que es la que usa
    la conexión. Si se filtra por la primera, la conexión va a la segunda.

    Resolver todo y rechazar el conjunto entero significa que un nombre con una sola dirección
    privada no se abre. Es más estricto que un filtro deIP única, y esa strictness es el punto.
    """

    if _permitido_explicitamente(host):
        return
    try:
        informacion = socket.getaddrinfo(host, puerto, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise DestinoNoPermitido(f"no se pudo resolver el destino: {error}") from error
    if not informacion:
        raise DestinoNoPermitido("el destino no resolvio a ninguna direccion")
    for familia, _tipo, _proto, _canonico, direccion in informacion:
        try:
            ip = ipaddress.ip_address(direccion[0])
        except ValueError as error:  # pragma: no cover - getaddrinfo no devuelve esto
            raise DestinoNoPermitido(f"direccion no interpretable: {direccion[0]!r}") from error
        if _es_ip_privada(ip):
            raise DestinoNoPermitido(
                f"el destino resuelve a una direccion no publica ({ip}); un escaneo de "
                "contenedores no puede usarse para alcanzar la red interna del cliente"
            )
        if familia not in (socket.AF_INET, socket.AF_INET6):  # pragma: no cover
            raise DestinoNoPermitido("el destino resolvio a una familia no admitida")


def _comprobar_destino(destino: str) -> tuple[str, int]:
    """Valida el esquema y la dirección de la URL, y devuelve `(host, puerto)`."""
    partes = urlparse(destino)
    if partes.scheme not in ESQUEMAS_PERMITIDOS:
        raise DestinoNoPermitido(
            f"el esquema {partes.scheme!r} no se abre; solo http y https"
        )
    if partes.username or partes.password:
        # Un `https://user:clave@host` hace que las credenciales viajen en la URL, que es
        # exactamente lo que el resto del agente evita. No hay ningún caso legitimo aqui: el
        # agente no trae credenciales, a proposito.
        raise DestinoNoPermitido("la URL no puede llevar credenciales incrustadas")
    host = partes.hostname
    if not host:
        raise DestinoNoPermitido("la URL no tiene destino")
    puerto = partes.port or (443 if partes.scheme == "https" else 80)
    # Una IP literal se comprueba sin pasar por DNS: `getaddrinfo` tambien la acepta, pero
    # pasar por el resolvedor haria que un `localhost` escrito como IP llegase al filtro por un
    # camino distinto al de un nombre, y dos caminos son dos oportunidades de que uno se olvide.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        _resolver_y_comprobar(host, puerto)
    else:
        if _es_ip_privada(literal) and not _permitido_explicitamente(host):
            raise DestinoNoPermitido(
                f"el destino {literal} es una direccion no publica; un escaneo de contenedores "
                "no puede usarse para alcanzar la red interna del cliente"
            )
    return host, puerto


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    """Rechaza los `3xx` en vez de seguirlos.

    ## Por qué no vale con "no seguirlas" como configuración del opener

    Porque el `Authorization` sigue siendo un problema aunque no se sigan. La defense que de
    verdad importa es esta: aunque una redirección llegue ateras, **no** se ejecuta. Un `3xx` de
    un registro significa "el destino que pediste no es este", y eso es un error de escaneo, no
    una instrucción a seguir.

    Con esto, el caso de un registro malicioso que devuelve `302` a la red interna deja de
    funcionar, y el caso de un `Location` hacia un host de otra organisation también: sin
    redirección no hay segunda petición, y sin segunda petición no hay a dónde enviar una
    cabecera.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


#: Los handlers que este agente no quiere. Se quitan uno a uno porque `build_opener` los deja
#: puestos aunque no se le pasen.
_HANDLERS_QUE_NO_QUIERO: Final[tuple[type[urllib.request.BaseHandler], ...]] = (
    urllib.request.FileHandler,
    urllib.request.FTPHandler,
    urllib.request.DataHandler,
)

def _construir_opener() -> urllib.request.OpenerDirector:
    """El opener, con los handlers por defecto **y** los que este agente no quiere.

    ## Por qué se quitan **después** de construir y no antes

    Porque `build_opener` **añade** los handlers por defecto a los que se le pasan, para todo lo
    que no se le haya dado. Un `OpenerDirector` a pelo tampoco sirve: `OpenerDirector` los instala en su
    `__init__`, y construirlo handler a handler los deja puestos igual.

    Escribí este bloque dos veces antes de que funcionara: la primera pasaba las **clases** en vez
    de las instancias y falló con `add_parent() missing 1 required positional argument`, y la
    segunda construía a mano y aun así los tenía instalados. Lo que funciona, y lo que queda, es
    **quitar los handlers de la lista después de construir**. Es más feo, y por eso lleva este
    comentario: porque la versión «bonita» no funciona y una revisión la daría por buena.

    Y esto no es un detalle de estilo, es la diferencia entre una defensa **estructural** y una
    que depende de acordarse de llamar a `_comprobar_destino` antes. Con los handlers fuera,
    `file://` no funciona aunque alguien escriba el `urlopen` equivocado en el futuro: el error
    lo produce `urllib` diciendo que no conoce el esquema, no este módulo.

    La comprobación de esquema de `_comprobar_destino` **se queda**, y por una razón distinta: da
    un mensaje que dice por qué y qué hacer, en vez del `URLError` seco de `urllib`.
    """

    opener = urllib.request.build_opener(_SinRedireccion())
    opener.handlers = [
        handler for handler in opener.handlers if not isinstance(handler, _HANDLERS_QUE_NO_QUIERO)
    ]
    return opener


_OPENER: Final[urllib.request.OpenerDirector] = _construir_opener()


def abrir(destino: str, cabeceras: dict[str, str] | None = None, timeout: float = 60.0) -> Any:
    """Abre `destino` si este agente puede, y falla con un motivo utilizable si no.

    Devuelve la respuesta **sin leer**. Quien llama decide cuántos bytes consume, con
    `leer_acotado`.
    """

    _comprobar_destino(destino)
    peticion = urllib.request.Request(destino, headers=cabeceras or {})
    try:
        return _OPENER.open(peticion, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 303, 307, 308):
            raise DestinoNoPermitido(
                f"el destino respondió una redirección a {error.headers.get('Location')!r}; "
                "este agente no sigue redirecciones, y no las segue porque seguirlas es la via "
                "por la que un registro controlado por el usuario alcanzaria la red interna"
            ) from error
        raise
    except urllib.error.URLError as error:
        raise error.reason if isinstance(error.reason, OSError) else error


def leer_acotado(respuesta: Any, tope: int = MAX_CUERPO) -> bytes:
    """Lee como mucho `tope` bytes, y **falla** si hay más.

    ## Por qué falla y por qué no corta en silencio

    Porque un cuerpo truncado que se acepta como si fuera completo produce un resultado que
    **parece** un manifiesto entero y no lo es. El escaneo continuaría con una lista de capas
    incompleta y publicaría un digest y un recuento de paquetes falsos, que es un dato de
    evidencia con apariencia de correcto. Un error es preferible a eso.

    Y por eso la lectura es por trozos: cortar el flujo a los `tope + 1` bytes significa que un
    cuerpo de 4 GB no llega a materializarse nunca, ni siquiera parcialmente en la memoria del
    proceso.
    """

    leido = bytearray()
    while len(leido) <= tope:
        trozo = respuesta.read(min(65536, tope + 1 - len(leido)))
        if not trozo:
            return bytes(leido)
        leido.extend(trozo)
    raise DestinoNoPermitido(
        f"la respuesta supera el maximo de {tope} bytes; este agente no carga cuerpos de ese "
        "tamaño en la memoria de la maquina del cliente"
    )
