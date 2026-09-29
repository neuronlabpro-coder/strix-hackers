"""Comprueba que el cerco de salida del sandbox esta puesto antes de lanzar un escaneo.

## El problema

Cada trabajo de escaneo crea su red bridge propia y el contenedor sale de ella con la
misma salida que el host: los metadatos de la nube, la red de Tailscale, Redis, PostgreSQL del
despliegue. El motor **necesita** salir —clonar repositorios, resolver nombres, llamar al
proveedor de inferencia—, asi que la red no se puede declarar `internal`. Lo que se quiere es
que solo salga a DNS y a HTTPS.

## Por que no se instala desde aqui

Por el orden. La subred del trabajo la elige **Docker**, y solo se conoce despues de crear la
red; la regla de filtrado tiene que existir **antes** de que haya trafico. Y el runner no
puede fijar la subred de antemano porque `networks.create` —la API de Docker que usa el worker,
`docker-py`— no acepta configuracion de IPAM. Invertir ese orden exigiria crear la red por
otro camino, y ese camino es exactamente el acceso crudo a la API que la lista blanca de
`test_runner_docker_surface.py` prohibe.

De ahi la division de trabajo: `scripts/harden_runner_egress.sh` instala las reglas en el host
durante el arranque, y este modulo **comprueba** que estan.

## Por que comprobar y no avisar en un log

Porque el aviso en el log es el que nadie lee. Un despliegue que arranca sin el script tiene
un contenedor con salida completa a la red interna, y si eso solo produce una linea en un
registro, la mitad de las veces la proteccion estara ausente sin que nadie lo sepa.

Negarse a ejecutar es la unica forma de que "el cerco esta puesto" y "hay escaneos corriendo"
sean la misma afirmacion. Sale cara: un despliegue mal configurado no acepta trabajos. Es
preferible a un despliegue que acepta trabajos sin proteccion creyendose protegido.

## Lo que NO cubre

La **entrada** al contenedor desde el host. `DOCKER-USER` gobierna el trafico que cruza el
puente; para la entrada hay que escribir en la cadena de la interfaz del host, y ese camino
tiene su propia politica de cortafuegos. El contenedor corre con `cap_drop=ALL` y
`no-new-privileges`, asi que no puede abrir un puerto de escucha que se vea desde fuera, pero
un proceso del host si puede alcanzar al contenedor.
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
from functools import lru_cache

from backend.core.config import settings

logger = logging.getLogger(__name__)

# El comentario que el script deja en las reglas. Es lo que se busca en la cadena.
COMENTARIO_DEL_SCRIPT = "fenix: egress del runner"
CADENA = "DOCKER-USER"

#: Rutas **absolutas** donde vive `iptables`, por orden de preferencia. Con un nombre suelto,
#: la busqueda en `PATH` la decide el entorno, y un `PATH` manipulado podria ejecutar otro
#: binario. Ademas, en Debian y derivadas el binario esta en `/usr/sbin`, que no forma parte
#: del `PATH` de un usuario normal: por eso hace falta la ruta entera.
#:
#: Se prueban las dos porque la distribucion del host no se controla desde aqui: `/usr/sbin` en
#: Debian y Ubuntu, `/sbin` en RHEL, Fedora y Alpine. Si ninguna existe —un host de
#: desarrollo, o uno que solo tenga `nft`— la comprobacion devuelve que no hay cerco, que es lo
#: correcto: sin `iptables` no se puede afirmar que el trafico este filtrado.
RUTAS_DE_IPTABLES = ("/usr/sbin/iptables", "/sbin/iptables")

# El prefijo de la regla de DROP del script. Su presencia es lo que dice que hay un cerco.
PREFIJO_DROP = "-j DROP"


class EgressFenceMissingError(RuntimeError):
    """El cerco de salida no esta instalado, y se ha negativo a lanzar el escaneo."""


def subred_de_la_red(network) -> str | None:
    """La subred que Docker le dio a la red, o `None` si no la redujo a mano.

    Docker devuelve la asignacion en `attrs["IPAM"]["Config"]`, y a veces viene vacia si la
    red se creo sin pool explicito. Se devuelve `None` en vez de suponer: una subred inventada
    haria que la comprobacion siguiente mirase el rango equivocado y pasara.
    """

    try:
        configuracion = network.attrs["IPAM"]["Config"]
    except (AttributeError, KeyError, TypeError):
        return None
    if not configuracion:
        return None
    subred = configuracion[0].get("Subnet")
    return str(subred) if subred else None


@lru_cache(maxsize=1)
def reglas_de_la_cadena() -> tuple[str, ...]:
    """El texto de `iptables -S DOCKER-USER`, cacheado por proceso.

    Se cachea porque se consulta una vez por escaneo y lanzar `iptables` por cada comprobacion
    en un worker con concurrencia seria carga de mas para lo que es una lectura de texto. Se
    cachea **por proceso** y no con TTL porque las reglas las instala el arranque del host: si
    cambian, cambian reiniciando el worker, que reinicia el proceso y por tanto la cache.
    """

    for ejecutable in RUTAS_DE_IPTABLES:
        try:
            resultado = subprocess.run(  # noqa: S603
                # Sin shell y con los argumentos en lista: la salida de `iptables` se imprime
                # tal cual en logs y en el mensaje de error, y con `shell=True` un nombre de
                # cadena con espacios se partiria en dos argumentos. `-S` solo **lista**, no
                # modifica nada, que es lo unico que este modulo necesita hacer.
                [ejecutable, "-t", "filter", "-S", CADENA],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except OSError:
            # `FileNotFoundError` entra aqui: esta distribucion no lo tiene en esa ruta, y se
            # prueba la siguiente. Cualquier otro `OSError` tambien es "no lo puedo ejecutar",
            # que para este modulo es lo mismo que no tenerlo.
            continue
        except subprocess.SubprocessError:
            return ()

        if resultado.returncode != 0:
            # Esta existe pero no se puede listar la cadena. No se sigue probando: si el
            # binario esta y falla, probar otro no va a dar una lectura mejor, y fingir que no
            # hay `iptables` esconde un problema de permisos que hay que ver.
            return ()
        return tuple(
            linea.strip() for linea in resultado.stdout.splitlines() if linea.strip()
        )
    return ()


def hay_cerco_para(subred: str, reglas: tuple[str, ...] | None = None) -> bool:
    """¿Hay una regla de DROP que cubra la subred del trabajo?

    ## Por que se busca un DROP y no una regla de RETURN

    Porque el script instala las reglas de permitidos con `RETURN` y, **al final**, un `DROP`
    de todo lo que venga de la red. Un `RETURN` solo por si mismo no acota nada: en una cadena
    `DOCKER-USER` sin regla de aceptacion propia, `RETURN` deja pasar el trafico hacia las
    reglas de Docker, que aceptan. Lo que acota es el `DROP` de la cola.

    Y se comprueba que el `DROP` tenga el **mismo prefijo de subred** que la red del trabajo, no
    que este dentro de un rango mas amplio: si el cerco esta puesto para `172.31.0.0/16` y el
    trabajo cae en `172.20.0.0/16`, ese trabajo **no** esta protegido, y darlo por protegido
    porque "hay una regla" seria el fallo mas grave posible de esta comprobacion.

    ## Por que el rango mas amplio si protege

    Al contrario que el caso anterior y por el mismo criterio, leido al reves: si la regla
    cubre `172.16.0.0/12` y el trabajo cae en `172.31.0.0/24`, ese trabajo **si** esta dentro
    de lo que la regla corta, y declararlo desprotegido seria un falso positivo que llevaria a
    desactivar el interruptor "para no bloquear escaneos". Lo que no se acepta es lo
    contrario: una regla que no cubre la subred del trabajo.
    """

    lineas = reglas if reglas is not None else reglas_de_la_cadena()
    if not lineas:
        return False

    # Se normaliza la subred para comparar por red y no por cadena: `172.31.0.1/24` y
    # `172.31.0.0/24` son la misma red y se comparan distinto.
    try:
        objetivo = ipaddress.ip_network(subred, strict=False)
    except ValueError:
        return False

    for linea in lineas:
        if PREFIJO_DROP not in linea or COMENTARIO_DEL_SCRIPT not in linea:
            continue
        # `-s` y su valor son **dos campos** al partir por espacios, asi que el valor es el
        # siguiente y no lo que queda tras la bandera. Se recorre con indice por eso: tomar
        # `campo[2:]` de `-s` devuelve cadena vacia, que `ip_network` rechaza, y la comprobacion
        # daba "sin cerco" con un cerco puesto.
        campos = linea.split()
        for posicion, campo in enumerate(campos[:-1]):
            if campo != "-s":
                continue
            try:
                red_candidata = ipaddress.ip_network(campos[posicion + 1], strict=False)
            except ValueError:
                continue
            if red_candidata.version != objetivo.version:
                continue
            if isinstance(objetivo, ipaddress.IPv4Network) and isinstance(
                red_candidata, ipaddress.IPv4Network
            ):
                if objetivo.subnet_of(red_candidata):
                    return True
    return False


def exigir_cerco_de_salida(
    subred: str | None,
    *,
    requerido: bool | None = None,
    reglas: tuple[str, ...] | None = None,
) -> None:
    """Se niega a continuar si el cerco no cubre la red del trabajo.

    ## Por que el interruptor y las reglas son **parametros** y no se leen del global

    Porque `Settings` es **congelado**, y un objeto congelado no admite sobreescritura. Es la
    misma razon por la que `llm_router.client.build_chat_url` recibe la base como parametro: leer
    del global obligaria a la prueba a mutarlo con `object.__setattr__`, que es un truco que se
    cuela en el codigo de produccion y acaba debilitando la garantia de inmutabilidad que lo
    motivo.

    Con la base como parametro, la prueba pasa un valor y el codigo de produccion pasa
    `settings.strix_require_egress_fence`. La funcion deja de depender del entorno, que es lo
    que la vuelve comprobable sin trucos.

    Con `STRIX_REQUIRE_EGRESS_FENCE=false` la comprobacion pasa a ser un aviso. Eso es lo que
    hay que usar en desarrollo, donde no hay `DOCKER-USER` porque no hay demonio de Docker: sin
    el interruptor, la maquina de desarrollo no podria lanzar ni un escaneo de prueba, y la
    proteccion se desactivaria sola por no haber donde probarla.
    """

    if requerido is None:
        requerido = settings.strix_require_egress_fence

    if not requerido:
        logger.warning(
            "Cerco de salida NO comprobado: STRIX_REQUIRE_EGRESS_FENCE esta en false. El "
            "contenedor sale con los permisos de red del host."
        )
        return

    if subred is None:
        raise EgressFenceMissingError(
            "Docker no devolvio la subred de la red del trabajo, asi que no se puede comprobar "
            "si el cerco de salida la cubre. Sin ese dato no se sabe que el contenedor sale "
            "solo a DNS y HTTPS, y no se lanza. Instala el cerco con "
            "'sudo bash scripts/harden_runner_egress.sh'."
        )

    if not hay_cerco_para(subred, reglas):
        raise EgressFenceMissingError(
            f"El cerco de salida no cubre la subred {subred} del trabajo, y no se lanza el "
            "escaneo. Sin el, el contenedor sale con los permisos de red del host: los "
            "metadatos de la nube, la red de Tailscale, Redis y PostgreSQL del despliegue. "
            "Instalalo con 'sudo bash scripts/harden_runner_egress.sh' y ponlo en el arranque "
            "del host, porque las reglas de iptables no sobreviven a un reinicio. Para "
            "desarrollar en local, pon STRIX_REQUIRE_EGRESS_FENCE=false."
        )

    logger.info("Cerco de salida comprobado para la subred %s", subred)
