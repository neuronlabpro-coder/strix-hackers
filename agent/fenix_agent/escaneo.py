"""Los dos escaneos: inventario de una imagen y barrido de una red.

## Por qué el escaneo de contenedor va por la API del registro y no bajando la imagen

Porque este agente **no tiene** el socket de Docker, y no por capricho: hablar con el demonio
significa poder crear contenedores, y en la red del cliente eso es un privilegio que no le
concede a un escáner.

La API de distribución OCI da lo mismo que da `docker pull` para lo que hace falta aquí —el
manifiesto, la configuración y las capas— y solo necesita HTTPS. Y como el escaneo corre en la red
del cliente, el registro privado de ese cliente sí es alcanzable, que es justo el caso que un
escaneo de inventario no puede ignorar.

## Por qué el recorrido de la capa es **en memoria** y no a disco

Por dos razones que apuntan en el mismo sentido. La capa de una imagen son cientos de megabytes, y
lo único que se quiere de ella son cinco ficheros: la base de datos de paquetes del sistema
operativo y poco más. Descomprimirla a disco dejaría el contenido de una imagen de terceros en el
sistema de ficheros de la máquina del cliente, y en un directorio que alguien tendría que
limpiar. Y el recorrido es `tarfile` sobre un flujo: los miembros se van viendo y se descartan, y
en cuanto aparece la base de datos se para.

## Por qué hay un límite de tamaño en la capa

Porque comprobar el tamaño **después** de leer es tarde: para entonces el proceso ya tiene la
capa en memoria. El corte tiene que estar en el bucle de lectura, que es lo único que decide
antes de seguir acumulando.

## Por qué `rpm` no se lee

Porque la base de datos de paquetes de RHEL, Fedora y Rocky está en un formato **binario** propio,
y un lector aproximado inventaría versiones de paquete, que es peor que no informar. Se declara
que no se soporta y la imagen sale con lo que sí se pudo leer y con un motivo que lo explica. Es
lo mismo que hace `supply_chain` con un manifiesto que no reconoce.

## Por qué el escaneo de red **no** escanea los 65 535 puertos

Porque 65 535 puertos por cada dirección de un `/24` son cientos de millones de conexiones que no
terminan nunca, y un escaneo que no termina no devuelve nada y consume presupuesto mientras tanto.
La lista viene de la plataforma en cada `claim` y es el punto de partida, no una política fija.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
import socket
import tarfile
import time
import urllib.error
import urllib.request

from fenix_agent import red
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Hilos del barrido de red. El tope no es un capricho: por encima de esto, en una máquina
#: normal, el cuello de botella deja de ser la red y pasa a ser el planificador, y cada hilo
#: adicional solo añade contexto. Con 64 sale más rápido que con 200.
HILOS_DE_BARRIDO: Final[int] = 64

#: Tamaño máximo de **una** capa. Es un límite de memoria, no de política: el proceso sostiene la
#: capa mientras la recorre, así que el techo tiene que estar en lo que aguanta la máquina, no
#: en lo que alguien quiera descargar.
MAX_LAYER_BYTES: Final[int] = 64 * 1024 * 1024

#: Paquetes por imagen. Una imagen con más que esto no es una imagen de aplicación y es
#: probablemente un `FROM scratch` con un artefacto enorme dentro.
MAX_PAQUETES: Final[int] = 2_000

#: Hosts por trabajo de red. Un `/24` son 254 direcciones útiles, y un `/16` serían 65 535, que
#: ni cabe en el tiempo de un trabajo ni es lo que nadie quiere de un escaneo. Cuando se
#: recorta, se declara en el resultado.
MAX_HOSTS: Final[int] = 256

#: Segundos por conexión. Corto a propósito: un puerto filtrado de un host que no responde tiene
#: que costar lo mínimo, porque el peor caso de un barrido es que la mayoría de los puertos estén
#: filtrados.
TIMEOUT_PUERTO: Final[float] = 1.5

REFERENCIA = re.compile(
    r"""^
    (?:(?P<registro>[a-zA-Z0-9.\-]+(?::[0-9]+)?)/)?
    (?P<repositorio>[a-z0-9]+(?:[._\-/][a-z0-9]+)*)
    (?::(?P<etiqueta>[a-zA-Z0-9._\-]+))?
    (?:@(?P<digest>sha256:[a-f0-9]{64}))?
    $""",
    re.VERBOSE,
)

#: Un primer componente es un **registro** y no un *namespace* de Docker Hub cuando tiene un
#: punto o un puerto. Es la misma regla que usa el propio Docker, y no un invento: un dominio
#: siempre tiene punto, y un puerto se escribe con dos puntos. Sin esta condición,
#: `bitnami/nginx:1.27` apuntaría a un host llamado `bitnami`, que no existe.
TIENE_PUERTO = re.compile(r":[0-9]+$")

ACEPTAR_MANIFIESTO: Final[str] = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

#: Las bases de datos de paquetes, en el orden en que se buscan. Solo una imagen normal trae
#: una. `rpm` **no** aparece: ver el docstring del modulo.
BASES_DE_DATOS: Final[tuple[tuple[str, str], ...]] = (
    ("alpine", "lib/apk/db/installed"),
    ("debian", "var/lib/dpkg/status"),
)


class ErrorDeEscaneo(RuntimeError):
    """El escaneo no pudo hacerse, y el motivo es para el cliente, no para el log."""


@dataclass(frozen=True, slots=True)
class ReferenciaImagen:
    registro: str
    repositorio: str
    etiqueta: str | None
    digest: str | None

    @property
    def completa(self) -> str:
        if self.digest:
            return f"{self.registro}/{self.repositorio}@{self.digest}"
        return f"{self.registro}/{self.repositorio}:{self.etiqueta or 'latest'}"


def partir_referencia(referencia: str) -> ReferenciaImagen:
    """Parte `ghcr.io/org/app:1.2` o `alpine:latest` en sus partes.

    ## Por qué `alpine` sin registro es Docker Hub

    Porque es lo que hace `docker pull alpine`, y es lo que escribe la gente. Un escáner que
    rechazara `alpine:3.20` por no tener registro sería un escáner que nadie usa.

    Y **solo** para Docker Hub: un registro en `localhost:5000` es un registro propio y se
    respeta tal cual, porque ahí no hay nada que adivinar.
    """

    limpia = (referencia or "").strip()
    coincidencia = REFERENCIA.match(limpia)
    if coincidencia is None:
        raise ErrorDeEscaneo(
            f"«{referencia}» no parece una referencia de imagen. Se espera algo como "
            "«alpine:3.20», «ghcr.io/organizacion/aplicacion:1.4.0» o "
            "«registry.example.com:5000/equipo/servicio@sha256:...»."
        )

    registro = coincidencia.group("registro")
    repositorio = coincidencia.group("repositorio")
    if registro is not None and not (
        "." in registro or TIENE_PUERTO.search(registro) or registro == "localhost"
    ):
        # `bitnami/nginx`: el primer componente es el namespace de Docker Hub, no un registro.
        repositorio = f"{registro}/{repositorio}"
        registro = None
    if registro is None:
        registro = "registry-1.docker.io"
        repositorio = f"library/{repositorio}" if "/" not in repositorio else repositorio

    return ReferenciaImagen(
        registro=registro,
        repositorio=repositorio,
        etiqueta=coincidencia.group("etiqueta"),
        digest=coincidencia.group("digest"),
    )


# --------------------------------------------------------------------------- #
# Lectura del registro
# --------------------------------------------------------------------------- #


def _abrir(destino: str, cabeceras: dict[str, str], timeout: float = 60.0) -> Any:
    """Abre una URL si este agente puede, y falla con un motivo utilizable.

    Delega en `red.abrir`, que es donde vive la politica: esquema acotado, destino resuelto y
    comprobado contra las redes privadas, y sin redirecciones. Esta funcion solo traduce los
    errores de HTTP a los mensajes que el operador entiende, que es lo unico que aporta.
    """

    try:
        return red.abrir(destino, cabeceras, timeout)
    except red.DestinoNoPermitido as error:
        raise ErrorDeEscaneo(str(error)) from error
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise ErrorDeEscaneo(
                "el registro pide autenticacion y este agente no trae credenciales. Un registro "
                "privado necesita un `docker login` en la maquina, y ese token **no** se "
                "configura en este agente a proposito: se pondria en la red del cliente una "
                "credencial que no le hace falta para nada mas."
            ) from error
        if error.code == 404:
            raise ErrorDeEscaneo(
                f"el registro no tiene esa imagen (HTTP 404). Comprueba la referencia completa, "
                "incluida la etiqueta."
            ) from error
        raise ErrorDeEscaneo(f"el registro respondió HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ErrorDeEscaneo(
            f"no se pudo contactar con {destino}: {error.reason}. Si el registro es interno, "
            "este agente tiene que poder llegar a el."
        ) from error


def _json(respuesta: Any) -> Any:
    cuerpo = red.leer_acotado(respuesta)
    try:
        return json.loads(cuerpo.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ErrorDeEscaneo("el registro no devolvio JSON donde se esperaba") from error


def token_de_lectura(referencia: ReferenciaImagen) -> str:
    """El token de vida corta para esa imagen, segun lo dice el propio registro.

    ## Por qué esto va en el agente y no en la plataforma

    Porque el token lo emite el registro **para una imagen concreta**, y un token emitido en el
    VPS no serviría contra un registro que solo es visible desde la red del cliente. Es la
    operación entera —leer el manifiesto de una imagen— la que tiene que ocurrir donde la imagen
    es alcanzable, y por eso el agente existe.

    ## Por qué el `realm` pasa por el filtro de destino, y no se usa tal cual

    Porque viene del registro, no de nosotros, y **eso es lo que lo hacía peligroso**: el registro
    lo elige quien encola el escaneo, así que el `realm` también. Una versión anterior de este
    módulo lo tomaba sin validar y construía la siguiente petición con él, de modo que un
    `registry.attacker.com` podía hacer que este agente —**desde dentro de la red del cliente**—
    emitiera un `GET` a `169.254.169.254` o a un `10.x` cualquiera, y además lo cargara entero en
    memoria.

    Ahora el `realm` pasa por `red.abrir`, que es la misma comprobación que el resto de destinos
    del agente: esquema acotado, todas las IP que resuelve el nombre comparadas contra las redes
    privadas, y sin redirecciones. Un registro de la red del cliente **sigue funcionando** si su
    `realm` resuelve a una dirección pública, que es lo normal. Lo que ya no puede es apuntar a
    la red interna.

    El mismo filtro cierra el otro caso: un `realm` público que respondiera con un `302` a la red
    interna ya no provoca una segunda petición, porque las redirecciones no se siguen.
    """

    reto = _abrir(
        f"https://{referencia.registro}/v2/{referencia.repositorio}/manifests/latest",
        {"Accept": ACEPTAR_MANIFIESTO},
    )
    reto.close()
    # Un `HEAD` con 200 significa que no hace falta token; con 401, el `WWW-Authenticate` dice
    # cómo pedirlo. Se hace un `GET` de nuevo porque `_abrir` no expone las cabeceras del error.
    peticion = urllib.request.Request(
        f"https://{referencia.registro}/v2/{referencia.repositorio}/manifests/latest",
        headers={"Accept": ACEPTAR_MANIFIESTO},
        method="GET",
    )
    try:
        with red.abrir(peticion.full_url, dict(peticion.header_items()), 30) as respuesta:
            return ""  # 200 sin reto: registro sin autenticacion
    except urllib.error.HTTPError as error:
        if error.code != 401:
            if error.code == 404:
                raise ErrorDeEscaneo(
                    f"«{referencia.completa}» no existe en {referencia.registro} (HTTP 404)"
                ) from error
            raise ErrorDeEscaneo(f"el registro respondió HTTP {error.code}") from error
        reto_cabecera = error.headers.get("www-authenticate", "")
    except urllib.error.URLError as error:
        raise ErrorDeEscaneo(
            f"no se pudo contactar con {referencia.registro}: {error.reason}"
        ) from error

    if "realm=" not in reto_cabecera:
        raise ErrorDeEscaneo(
            "el registro pidió credenciales sin decir donde estan. Un registro de empresa que "
            "exija autenticacion no esta soportado todavia."
        )
    realm = reto_cabecera.split('realm="')[1].split('"')[0]
    servicio = (
        reto_cabecera.split('service="')[1].split('"')[0] if 'service="' in reto_cabecera else None
    )
    consulta = f"scope=repository:{referencia.repositorio}:pull"
    if servicio:
        consulta += f"&service={servicio}"
    with _abrir(f"{realm}?{consulta}", {}, timeout=30) as respuesta:
        cuerpo = _json(respuesta)
    token = cuerpo.get("token") or cuerpo.get("access_token")
    if not token:
        raise ErrorDeEscaneo("el registro no concedio token de lectura")
    return str(token)


def manifiesto_de_plataforma(manifiesto: dict[str, Any]) -> str:
    """El digest de la imagen concreta para `amd64`, si el manifiesto es un índice.

    ## Por qué se elige `amd64` y no "la primera"

    Porque la lista de plataformas está ordenada por preferencia del publicador, no por
    arquitectura, y la primera puede ser `arm64`. Un escáner que informara del sistema operativo
    de la plataforma equivocada daría un inventario que no corresponde con nada que el cliente
    pueda ejecutar.
    """

    entradas = manifiesto.get("manifests")
    if not isinstance(entradas, list):
        return ""
    for entrada in entradas:
        if not isinstance(entrada, dict):
            continue
        plataforma = entrada.get("platform")
        if isinstance(plataforma, dict) and plataforma.get("architecture") == "amd64":
            return str(entrada.get("digest", ""))
    return ""


# --------------------------------------------------------------------------- #
# Paquetes
# --------------------------------------------------------------------------- #


def _paquetes_de_apk(texto: str) -> list[dict[str, str]]:
    """Los paquetes de `lib/apk/db/installed`: bloques separados por línea en blanco.

    Cada bloque tiene `P:` con el nombre, `V:` con la versión y `L:` con la licencia. Se leen
    solo esos tres porque son los únicos que se pueden afirmar sin adivinar el formato.
    """

    paquetes: list[dict[str, str]] = []
    for bloque in texto.split("\n\n"):
        if not bloque.strip():
            continue
        campos: dict[str, str] = {}
        for linea in bloque.splitlines():
            if len(linea) > 2 and linea[1] == ":":
                campos[linea[0]] = linea[2:]
        nombre = campos.get("P")
        version = campos.get("V")
        if nombre and version:
            paquete = {"name": nombre, "version": version, "ecosystem": "apk"}
            licencia = campos.get("L")
            if licencia:
                paquete["license"] = licencia
            paquetes.append(paquete)
    return paquetes


def _paquetes_de_dpkg(texto: str) -> list[dict[str, str]]:
    """Los paquetes de `var/lib/dpkg/status`: párrafos con `Package:` y `Version:`.

    Se filtra por `Status:` para que no cuenten los que están **desinstalados** pero dejan la
    entrada de configuración. Un paquete con `deinstall ok config-files` no está en la imagen, y
    reportarlo como si lo estuviera es un falso positivo.
    """

    paquetes: list[dict[str, str]] = []
    for parrafo in texto.split("\n\n"):
        if not parrafo.strip():
            continue
        campos: dict[str, str] = {}
        for linea in parrafo.splitlines():
            if ":" in linea and not linea.startswith((" ", "\t")):
                clave, _, valor = linea.partition(":")
                campos[clave.strip()] = valor.strip()
        nombre = campos.get("Package")
        version = campos.get("Version")
        estado = campos.get("Status", "")
        if not nombre or not version:
            continue
        if "installed" not in estado or "not-installed" in estado:
            continue
        paquete = {"name": nombre, "version": version, "ecosystem": "dpkg"}
        if "arch" in campos:
            paquete["arch"] = campos["arch"]
        paquetes.append(paquete)
    return paquetes


def recorrer_capa_desde_texto(texto: str, ecosistema: str) -> tuple[list[dict[str, str]], str | None]:
    """El parser de paquetes, sin la capa.

    Existe **solo** para las pruebas, y la razón de que sea una función aparte y no un detalle
    dentro de `recorrer_capa` es que el parsing de `apk` y de `dpkg` es donde se podría
    inventar una versión de paquete, y eso hay que poder probar sin construir un `.tar.gz` de
    sesenta megabytes. La función de producción y esta comparten los mismos parsers, así que
    lo que se prueba aquí es exactamente lo que corre en el escaneo.
    """

    leidos = _paquetes_de_apk(texto) if ecosistema == "alpine" else _paquetes_de_dpkg(texto)
    if leidos:
        if len(leidos) > MAX_PAQUETES:
            leidos = leidos[:MAX_PAQUETES]
        return leidos, None
    return [], "no se encontro la base de datos de paquetes (apk o dpkg) en la imagen"


def preparar_direcciones(cidr: str, maximo: int) -> dict[str, Any]:
    """Las direcciones a sondear de un CIDR, y si hay que recortarlas.

    Está separada de `escanear_red` por la misma razón que `recorrer_capa_desde_texto`: decidir
    qué direcciones se van a mirar y avisar de que no son todas es una decisión, y una decisión
    se prueba sin abrir un socket.

    ## Por qué se descartan la dirección de red y la de difusión

    Porque no son hosts: no tienen ni una máquina detrás. Sondearlas gasta tiempo y devuelve un
    error de permiso del sistema operativo, que es un ruido que se cuela en el recuento de
    «direcciones analizadas» y hace que el número signifique otra cosa.

    Se descartan comparando con los extremos **calculados**, y no buscando un sufijo `.0` o
    `.255`. El sufijo solo acierta en los limites de un `/24`: en un `/28` de
    `10.10.10.0/28` la difusión es `10.10.10.15`, no `10.10.10.255`, así que un filtro por
    sufijo se cargaría un host real y dejaría pasar la dirección de difusión.
    """

    try:
        octetos = [int(o) for o in cidr.split("/")[0].split(".")]
        prefijo = int(cidr.split("/")[1])
    except (IndexError, ValueError) as error:
        raise ErrorDeEscaneo(f"«{cidr}» no es un CIDR IPv4") from error
    if len(octetos) != 4 or any(o < 0 or o > 255 for o in octetos) or not 0 <= prefijo <= 32:
        raise ErrorDeEscaneo(f"«{cidr}» no es un CIDR IPv4")

    base = (octetos[0] << 24) | (octetos[1] << 16) | (octetos[2] << 8) | octetos[3]
    mascara = 0 if prefijo == 0 else ((0xFFFFFFFF << (32 - prefijo)) & 0xFFFFFFFF)
    inicio = base & mascara
    fin = inicio | (mascara ^ 0xFFFFFFFF)
    total = fin - inicio + 1

    # Un /32 es una sola dirección y no tiene ni red ni difusión que descartar, asi que se
    # escanea entera. Es una petición legitima —«mira estos puertos de esta maquina»— y
    # rechazarla obligaria a inventar una excepcion en otro sitio.
    if prefijo >= 31:
        extremos = set()
    else:
        extremos = {inicio, fin}

    direcciones = [
        f"{(n >> 24) & 0xFF}.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}"
        for n in range(inicio, fin + 1)
        if n not in extremos
    ]

    recortado = len(direcciones) > maximo
    if recortado:
        direcciones = direcciones[:maximo]

    salida: dict[str, Any] = {
        "direcciones": direcciones,
        "recortado": recortado,
        "total_en_el_prefijo": total,
    }
    if recortado:
        # El codigo va **junto** al texto, no en vez de el. El texto es lo que lee el operador y
        # el codigo es lo que permite traducirlo: un resultado guardado en español se sigue
        # viendo igual, y uno guardado por un agente mas nuevo se muestra en el idioma de quien
        # esta mirando. Anadir un campo no invalida lo ya guardado.
        salida["codigo_recorte"] = "prefijo_truncado"
        salida["motivo"] = (
            f"el prefijo contiene {total} direcciones y este agente analiza como maximo "
            f"{maximo}. Divide la red en partes para escanearla entera."
        )
    return salida


def recorrer_capa(crudo: bytes) -> tuple[list[dict[str, str]], str | None]:
    """Busca la base de datos de paquetes dentro de la capa, en memoria.

    Devuelve los paquetes y el motivo por el que no se pudo leer el formato, que es `None` si sí
    se leyó. No es un fallo: es la respuesta a «¿por qué no hay nada aquí?».
    """

    with gzip.open(io.BytesIO(crudo), "rb") as flujo:
        with tarfile.open(fileobj=flujo, mode="r|") as tar:
            entradas = 0
            for miembro in tar:
                entradas += 1
                nombre = miembro.name.rstrip("./")
                for ecosistema, ruta in BASES_DE_DATOS:
                    if nombre != ruta:
                        continue
                    archivo = tar.extractfile(miembro)
                    if archivo is None:
                        continue
                    texto = archivo.read().decode("utf-8", "replace")
                    leidos = (
                        _paquetes_de_apk(texto) if ecosistema == "alpine" else _paquetes_de_dpkg(texto)
                    )
                    if len(leidos) > MAX_PAQUETES:
                        leidos = leidos[:MAX_PAQUETES]
                    return leidos, None
                if entradas > 40_000:
                    break

    # No se encontró ninguna de las dos. Si el `os-release` de la capa dice rpm, se dice
    # explícitamente, porque "no hay paquetes" sin motivo es indistinguible de "la imagen está
    # vacía" y el que lee el resultado no puede diferenciar los dos casos.
    return [], "no se encontro la base de datos de paquetes (apk o dpkg) en la imagen"


def detectar_sistema_operativo(manifiesto_imagen: dict[str, Any]) -> tuple[str, str | None]:
    """El `os` y la versión del sistema operativo, de la configuración de la imagen.

    Se lee de `os-release`, que es el fichero estándar y el único que todas las distribuciones
    escriben. Un `LABEL` de Maintainer no sirve: es texto libre y no distingue `9` de `8.10` de
    una distribución que usa otro esquema.
    """

    configuracion = manifiesto_imagen.get("config") or {}
    entorno = configuracion.get("Env") or []
    version = None
    sistema = "linux"
    for entrada in entorno:
        texto = str(entrada)
        if texto.startswith("ID="):
            sistema = texto[3:].strip('"')
        elif texto.startswith("VERSION_ID="):
            version = texto[11:].strip('"')
    return sistema, version


# --------------------------------------------------------------------------- #
# Los dos escaneos
# --------------------------------------------------------------------------- #


def escanear_imagen(referencia_texto: str) -> dict[str, Any]:
    """Inventaría una imagen: digest, sistema operativo y paquetes.

    No devuelve **vulnerabilidades por paquete**, y el motivo está escrito en el resultado. La
    tabla `cve_records` de la plataforma guarda identificador, severidad, CVSS, EPSS y una
    descripción, pero **no qué paquetes afecta cada CVE**, así que no hay forma de cruzar el
    inventario con la base de datos local. Un número de CVEs aquí sería inventado.
    """

    referencia = partir_referencia(referencia_texto)
    token = token_de_lectura(referencia)
    cabeceras = {"Accept": ACEPTAR_MANIFIESTO}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"

    version = referencia.digest or referencia.etiqueta or "latest"
    base = f"https://{referencia.registro}/v2/{referencia.repositorio}"

    # Si es un índice multi-plataforma, hay que bajarse primero al manifiesto concreto.
    indice = _json(_abrir(f"{base}/manifests/{version}", cabeceras))
    digest_plataforma = manifiesto_de_plataforma(indice)
    digest = digest_plataforma
    manifiesto = indice
    if digest_plataforma:
        digest = digest_plataforma
        manifiesto = _json(_abrir(f"{base}/manifests/{digest_plataforma}", cabeceras))

    capas = manifiesto.get("layers") or []
    sistema, version_sistema = detectar_sistema_operativo(manifiesto)

    paquetes: list[dict[str, str]] = []
    formato_no_leido: str | None = None
    capas_leyendose = 0
    for capa in capas:
        # Se lee una sola capa: la del sistema operativo es siempre la primera en una imagen
        # construida por capas, y recorrer todas gastaría ancho de banda para volver a encontrar
        # la misma base de datos. Si la primera no la tiene, se sigue con la siguiente, que es
        # lo que pasa en una imagen donde el SO viene de una capa base y la aplicación otra.
        try:
            crudo = _descargar_capa(
                f"{base}/blobs/{capa['digest']}", cabeceras, capa.get("size", 0)
            )
        except ErrorDeEscaneo as error:
            formato_no_leido = str(error)
            break
        capas_leyendose += 1
        paquetes, motivo = recorrer_capa(crudo)
        if motivo is None:
            break
        formato_no_leido = motivo
        del crudo

    codigo_formato: str | None = None
    if sistema in ("rhel", "centos", "fedora", "rocky", "almalinux", "ol"):
        codigo_formato = "familia_rpm"
        formato_no_leido = (
            "la imagen es de una familia rpm, cuya base de datos de paquetes esta en un formato "
            "binario que este agente no lee. Se declara en vez de inventar versiones de paquete."
        )
    elif formato_no_leido is not None:
        # El unico motivo que `recorrer_capa` produce hoy. Un motivo desconocido —una capa
        # corrupta, un `tar` que no se abre— llega sin codigo y la plataforma lo enseña tal cual,
        # que es lo unico honesto que se puede hacer con un texto que no se reconoce.
        codigo_formato = "sin_base_de_paquetes"

    resultado: dict[str, Any] = {
        "referencia": referencia.completa,
        "digest": digest,
        "sistema_operativo": sistema,
        "version_sistema_operativo": version_sistema,
        "arquitectura": "amd64" if digest_plataforma else None,
        "total_capas": len(capas),
        "capas_leidas": capas_leyendose,
        "bytes_capas": sum(c.get("size", 0) for c in capas),
        "paquetes": paquetes,
        "total_paquetes": len(paquetes),
        # Se declara en el propio resultado, y no se deduce: `cve_records` no guarda qué
        # paquetes afecta cada CVE, así que no hay dato local del que sacar el número.
        "vulnerabilidades_por_paquete": None,
        "codigo_sin_vulnerabilidades_por_paquete": "cve_sin_paquetes_afectados",
        "motivo_sin_vulnerabilidades_por_paquete": (
            "la base de datos de la plataforma no guarda que paquetes afecta cada CVE, asi que "
            "el inventario de paquetes no se puede cruzar con ella. Las vulnerabilidades de esta "
            "imagen salen de escanear sus servicios, no de sus paquetes."
        ),
    }
    if formato_no_leido:
        resultado["formato_no_leido"] = formato_no_leido
        if codigo_formato is not None:
            resultado["codigo_formato_no_leido"] = codigo_formato
    return resultado


def _descargar_capa(url: str, cabeceras: dict[str, str], tamaño: int) -> bytes:
    """La capa, en memoria, cortando en cuanto se supera el límite.

    ## Por qué se avisa antes de descargar si la capa es demasiado grande

    Porque el `size` del manifiesto es el tamaño **comprimido**, y la capa descomprimida puede ser
    varias veces mayor. Un `FROM scratch` con un artefacto de 80 MB puede medir 80 MB y
    descomprimirse a 400. El corte tiene que estar en el bucle de lectura, y este primer aviso
    es solo para no empezar algo que ya se sabe que no va a caber.
    """

    piezas: list[bytes] = []
    recibidos = 0
    with _abrir(url, cabeceras, timeout=120) as respuesta:
        while True:
            trozo = respuesta.read(256 * 1024)
            if not trozo:
                break
            recibidos += len(trozo)
            if recibidos > MAX_LAYER_BYTES:
                raise ErrorDeEscaneo(
                    f"una capa supera el limite de {MAX_LAYER_BYTES // (1024 * 1024)} MB y no se "
                    "sigue leyendo. Una capa tan grande no es una imagen de aplicacion; "
                    "probablemente es un `FROM scratch` con un artefacto dentro, y su inventario "
                    "de paquetes no se puede afirmar sin descargarla entera."
                )
            piezas.append(trozo)
    return b"".join(piezas)


def escanear_red(cidr: str, puertos: list[int]) -> dict[str, Any]:
    """Recorre un CIDR y prueba los puertos indicados en cada host que responde.

    ## Por qué se hace en paralelo y con cuántos hilos

    Porque un barrido secuencial de un `/24` contra puertos filtrados es, literalmente, horas de
    espera: casi todos los puertos de casi todos los hosts están filtrados, y cada uno cuesta el
    tiempo de espera completo. Con 64 hilos, un `/24` con 35 puertos se resuelve en segundos.

    Y el tope de 64 no es un capricho: por encima de eso, en una máquina normal, el cuello de
    botella deja de ser la red y pasa a ser el planificador, y cada hilo adicional solo añade
    contexto. Con 64 sale más rápido que con 200.

    ## Por qué los puertos por defecto **no** son "los puertos peligrosos"

    Porque la lista viene de la plataforma en cada `claim` y es el punto de partida. Un escáner
    que solo mirara los puertos conocidos se dejaría fuera medio mundo, y uno que mirara los
    65 535 no terminaría nunca.
    """

    preparacion = preparar_direcciones(cidr, MAX_HOSTS)
    validos = list(preparacion["direcciones"])
    recortado = bool(preparacion["recortado"])
    if not validos:
        raise ErrorDeEscaneo(
            f"el prefijo {cidr} no contiene ninguna direccion que se pueda sondear."
        )

    inicio_barrido = time.monotonic()
    hallazgos: list[dict[str, Any]] = []

    def sondear(direccion: str) -> dict[str, Any] | None:
        abiertos: list[dict[str, Any]] = []
        for puerto in puertos:
            if _puerto_abierto(direccion, puerto):
                abiertos.append(
                    {
                        "puerto": puerto,
                        "servicio": _servico_conocido(puerto),
                        "banner": _leer_banner(direccion, puerto),
                    }
                )
        if not abiertos:
            return None
        return {"ip": direccion, "puertos": abiertos}

    with ThreadPoolExecutor(max_workers=HILOS_DE_BARRIDO) as grupo:
        for resultado in grupo.map(sondear, validos):
            if resultado is not None:
                hallazgos.append(resultado)

    hallazgos.sort(key=lambda h: h["ip"])
    salida: dict[str, Any] = {
        "cidr": cidr,
        "direcciones_analizadas": len(validos),
        "hosts_con_puertos": len(hallazgos),
        "puertos_analizados": puertos,
        "hosts": hallazgos,
        "durata_segundos": round(time.monotonic() - inicio_barrido, 2),
        "vulnerabilidades_por_puerto": None,
        "codigo_sin_vulnerabilidades_por_puerto": "cve_sin_puertos_afectados",
        "motivo_sin_vulnerabilidades_por_puerto": (
            "este agente mide que puertos estan abiertos y que dicen al conectar. No decide si "
            "un servicio es vulnerable: el razonamiento ocurre en la plataforma, que es donde "
            "esta la clave del proveedor de modelos."
        ),
    }
    if recortado:
        salida["direcciones_recortadas"] = True
        salida["motivo_recorte"] = preparacion["motivo"]
        codigo_recorte = preparacion.get("codigo_recorte")
        if codigo_recorte is not None:
            salida["codigo_recorte"] = codigo_recorte
    return salida


def _red_por_cidr(cidr: str) -> tuple[int, ...]:
    """Las cuatro partes de la red, con un mensaje de error utilizable."""

    partes = cidr.split("/")
    if len(partes) != 2:
        raise ValueError("faltan la barra y el prefijo")
    try:
        octetos = [int(o) for o in partes[0].split(".")]
        prefijo = int(partes[1])
    except ValueError as error:
        raise ValueError("la direccion o el prefijo no son numeros") from error
    if len(octetos) != 4 or any(o < 0 or o > 255 for o in octetos):
        raise ValueError("la direccion no tiene cuatro octetos entre 0 y 255")
    if not 0 <= prefijo <= 32:
        raise ValueError("el prefijo tiene que estar entre 0 y 32")
    return (octetos[0], octetos[1], octetos[2], octetos[3])


#: Puertos cuyo nombre se puede afirmar sin ver el banner. Es una **afirmación**, no una conjetura:
#: el número de puerto es un hecho y el nombre del servicio registrado para él es un hecho del
#: registro de la IANA. Lo que no se afirma es la versión, que no se ve sin banner.
SERVICIOS: Final[dict[int, str]] = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    25: "smtp",
    53: "dns",
    80: "http",
    110: "pop3",
    111: "rpcbind",
    135: "msrpc",
    139: "netbios-ssn",
    143: "imap",
    389: "ldap",
    443: "https",
    445: "smb",
    1433: "mssql",
    1521: "oracle",
    2049: "nfs",
    2375: "docker-api",
    2376: "docker-api-tls",
    3000: "http-alternativa",
    3306: "mysql",
    3389: "rdp",
    5000: "http-alternativa",
    5432: "postgresql",
    5672: "amqp",
    5900: "vnc",
    6379: "redis",
    8000: "http-alternativa",
    8080: "http-proxy",
    8443: "https-alternativa",
    8888: "http-alternativa",
    9090: "prometheus",
    9200: "elasticsearch",
    11211: "memcached",
    15672: "rabbitmq-gestion",
    27017: "mongodb",
    28017: "mongodb-web",
}


def _servicio_conocido(puerto: int) -> str | None:
    return SERVICIOS.get(puerto)


def _puerto_abierto(direccion: str, puerto: int, timeout: float = TIMEOUT_PUERTO) -> bool:
    """Si el puerto acepta conexión.

    ## Por qué un `connect` y no un `SYN` puro

    Porque un SYN puro necesita permisos de socket en crudo que no están en la biblioteca
    estándar y que en un contenedor sin `CAP_NET_RAW` no se pueden pedir. Un `connect` completo es
    menos discreto —deja entrada en el registro del servicio destino— pero funciona en cualquier
    parte y es lo que se puede hacer sin instalar nada en la red del cliente.

    ## Por qué `0.5` de espera y no cero

    Porque un retorno inmediato solo distingue "cerrado" de "no hay respuesta", y en un puerto
    que no contesta no se sabe si está filtrado o apagado. Con una espera corta se distingue el
    caso común —filtrado con `REJECT`— del raro, y el resto se reporta como abierto o no.
    """

    familia = socket.AF_INET6 if ":" in direccion else socket.AF_INET
    try:
        with socket.socket(familia, socket.SOCK_STREAM) as conexion:
            conexion.settimeout(timeout)
            return conexion.connect_ex((direccion, puerto)) == 0
    except (socket.timeout, OSError):
        return False


def _leer_banner(direccion: str, puerto: int, timeout: float = 1.0) -> str | None:
    """Lo primero que conteste el servicio, o `None`.

    ## Por qué se lee y por qué se **acota**

    Porque un banner es la mitad del valor de un escaneo de red: la versión de PostgreSQL o de
    OpenSSH que hay detrás es lo que permite decir si está desactualizada. Y es lo único que se
    puede afirmar sin razonar.

    Se acota a 512 bytes porque hay servicios que hablarían indefinidamente —y un agente que se
    queda leyendo un banner infinito es un agente que no escanea nada más—.
    """

    try:
        with socket.create_connection((direccion, puerto), timeout=timeout) as conexion:
            conexion.settimeout(timeout)
            return conexion.recv(512).decode("utf-8", "replace").strip() or None
    except (socket.timeout, OSError):
        return None
