"""Inventario histórico del runner contenedor, ajeno a la política del broker.

La política de producción se define en fenix_docker_broker/policy.py a partir del smoke
Strix 1.7.0. El texto siguiente documenta por qué se inventariaron las llamadas del
runner antiguo; no concede permisos al broker.

## Por que este fichero existe

El worker monta el socket de Docker del host, y el montaje va marcado `:ro`. Ese `:ro` **no
protege nada**: un socket unix es un punto de comunicacion, no un fichero, y `ro` solo
afecta a la escritura en ficheros regulares. El worker puede seguir haciendo `connect()` y
seguir mandando peticiones de escritura al demonio. Quien leyera el `docker-compose.prod.yml`
veria "solo lectura" y se creeria que el worker esta limitado.

Que este limitado **es** una decision de despliegue, y se toma con un proxy delante del
socket que solo deje pasar lo que el runner necesita. Para escribir esa lista blanca hace
falta saber cual es esa superficie exacta, y esa es la razon de este fichero: la fija.

## Que comprueba

Que el runner **solo** use siete superficies de la API:

| Superficie                                | Para que                        |
|-------------------------------------------|---------------------------------|
| `POST /containers/create`                 | `containers.run`                |
| `POST /containers/{id}/start`             | `containers.run`                |
| `GET  /containers/{id}/json`              | `containers.get`, y `wait`      |
| `POST /containers/{id}/kill`              | matar por timeout               |
| `DELETE /containers/{id}`                 | `container.remove(force=True)`  |
| `POST /networks/create`                   | una red dedicada por trabajo    |
| `GET`/`DELETE /networks/{name}`           | limpiar la red del trabajo      |

Y sobre todo, que **no** aparezca ninguna otra. En concreto las que permitirian a un worker
comprometido escalar a control del host:

- `POST /containers/{id}/exec`: ejecutar dentro de otro contenedor —leeria la clave LLM de
  otro trabajo, y la del propio backend si el socket es del host—.
- `POST /build` y `POST /images/create`: construir una imagen con lo que se quiera dentro.
- `POST /volumes/create`: montar cualquier ruta del host.
- `POST /containers/create` con `privileged: true` o `CapAdd`, que el proxy no puede
  distinguir de un contenedor normal: por eso el `cap_drop=ALL` y `no-new-privileges` del
  propio `containers.run` son la segunda linea y no un adorno.

## Por que leer el codigo y no un registro de API

Porque una superficie que **si** se usa no la delata ningun registro, y una que ha dejado de
usarse tampoco. Leer el `sandbox.py` completo es la unica forma de que la lista sea completa,
y se lee entero porque un `.client.` en un metodo que este test no visita es justo el caso que
se colaria.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "workers" / "runner"

# La superficie del runner, indexada por la llamada **sin parentesis**, que es como la
# encuentra el escaner. El valor es la superficie de la API que autoriza, que es lo que la
# lista blanca del proxy tiene que dejar pasar.
SUPERFICIE_PERMITIDA = {
    "containers.run": "POST /containers/create + POST /containers/{id}/start",
    "containers.get": "GET /containers/{id}/json",
    "container.kill": "POST /containers/{id}/kill",
    "container.wait": "POST /containers/{id}/wait",
    "container.remove": "DELETE /containers/{id}",
    "networks.create": "POST /networks/create",
    "networks.get": "GET /networks/{name}",
    "network.remove": "DELETE /networks/{name}",
}

# Superficies que permitirian a un worker comprometido llegar al host. Si alguna aparece en el
# codigo del runner, la lista blanca del proxy tiene que crecer y con ella la superficie de
# ataque: por eso estan en la prueba y no solo en un comentario.
SUPERFICIE_PELIGROSA = {
    "exec_run": "POST /containers/{id}/exec — ejecutar dentro de otro contenedor",
    "containers.exec": "POST /containers/{id}/exec",
    "images.build": "POST /build",
    "images.pull": "POST /images/create",
    "volumes.create": "POST /volumes/create",
    "containers.prune": "POST /containers/prune",
    "networks.prune": "POST /networks/prune",
    "client.api": "acceso crudo a la API, se salta cualquier lista blanca",
}

# Los metodos del cliente Docker y del objeto contenedor o red, por tipo. El escaner arma
# `<objeto>.<metodo>` y de ahi se separa el objeto del metodo.
OBJETOS = r"(?:containers?|networks?|images?|volumes?)"
METODOS_DOCKER = r"(?:run|get|create|list|prune|exec|connect)"


def llamadas_al_demonio(fuente: str) -> set[str]:
    """Las llamadas `objeto.metodo(` del runner, normalizadas a `objeto.metodo`."""

    encontradas: set[str] = set()
    for patron in (
        rf"{OBJETOS}\.{METODOS_DOCKER}\(",
        rf"{OBJETOS}\.(?:kill|remove|reload|logs|start|stop|wait|exec_run|inspect)\(",
        r"images?\.(?:build|pull|push|get)\(",
        r"volumes?\.(?:create|get|remove|list)\(",
        r"\.client\.api\b",
    ):
        for encontrado in re.finditer(patron, fuente):
            # Se quita el parentesis final para que "containers.run(" y "containers.run" sean
            # la misma clave. Sin esto el inventario nunca casa con la lista y la prueba
            # falla siempre, que es como una prueba de inventario inservible.
            llamadas = encontrado.group(0).rstrip("(")
            # `container.kill(` y `containers.kill(` son el mismo objeto visto desde el
            # singular y el plural, y el proxy no los distingue. Se normaliza el plural.
            encontradas.add(re.sub(r"^(containers|networks|images|volumes)\.", r"\1.", llamadas))
    return encontradas


def fuente_del_runner() -> str:
    partes = []
    for fichero in sorted(RUNNER.rglob("*.py")):
        if "__pycache__" in str(fichero):
            continue
        partes.append(f"# === {fichero.name} ===\n")
        crudo = open(fichero, encoding="utf-8", newline="").read()
        partes.append(crudo.replace("\r\n", "\n"))
    return "\n".join(partes)


def test_la_superficie_de_docker_del_runner_esta_inventariada() -> None:
    """Cada llamada al demonio que hace el runner esta en la lista o rompe la prueba."""

    llamadas = llamadas_al_demonio(fuente_del_runner())
    sin_documentar = llamadas - set(SUPERFICIE_PERMITIDA)

    assert not sin_documentar, (
        "el runner llama a superficies de Docker que no estan en SUPERFICIE_PERMITIDA: "
        f"{sorted(sin_documentar)}. Cada una anade una superficie de ataque al worker, que "
        "tiene el socket del demonio. Anadela aqui con su justificacion, y anadela tambien a "
        "la lista blanca del proxy que se despliega delante del socket."
    )


def test_el_runner_no_usa_superficies_que_rompen_el_aislamiento() -> None:
    """Ninguna llamada del runner permite alcanzar el host desde el worker."""

    fuente = fuente_del_runner()
    encontradas = {
        llamada: motivo
        for llamada, motivo in SUPERFICIE_PELIGROSA.items()
        if llamada in fuente
    }
    assert not encontradas, (
        "el runner usa superficies de Docker que permiten alcanzar el host: "
        f"{encontradas}. Con el socket montado, cualquiera de estas convierte al worker en el "
        "host, y entonces la garantia de aislamiento de R3 es una linea en un documento."
    )


def test_el_cliente_docker_se_conecta_por_el_entorno_y_no_por_el_socket() -> None:
    """`from_env()` es lo que permite poner un proxy delante sin tocar el codigo del runner.

    Si el cliente se construyera con `DockerClient(base_url='/var/run/docker.sock')`, el
    proxy del despliegue no tendria efecto: el worker seguiria hablando con el demonio
    directamente y la lista blanca no existiria. Esta prueba falla en cuanto alguien cambia
    la fabrica, que es el momento exacto en que se perderia el aislamiento sin que ninguna
    otra prueba se entere.
    """

    fabrica = open(RUNNER / "docker_client.py", encoding="utf-8", newline="").read()
    fabrica = fabrica.replace("\r\n", "\n")

    assert "from_env(" in fabrica, (
        "la fabrica del cliente Docker ya no usa docker.from_env(). Sin el, el worker se "
        "conecta al socket del demonio y el proxy de la lista blanca deja de existir: "
        "DOCKER_HOST se ignora."
    )
    assert "/var/run/docker.sock" not in fabrica, (
        "la fabrica del cliente Docker tiene el socket escrito a mano. Con eso DOCKER_HOST no "
        "se respeta y el proxy del despliegue no se usa."
    )


@pytest.mark.parametrize("metodo", sorted(SUPERFICIE_PERMITIDA))
def test_cada_superficie_documentada_se_usa_de_verdad(metodo: str) -> None:
    """Una superficie documentada que ya no se usa hace ruido en la lista blanca.

    Una lista blanca con permisos de mas no es una lista blanca: permitir de mas lo que ya
    no se necesita solo hace que un compromiso futuro tenga mas puertas abiertas. Esta
    prueba mantiene la lista ajustada.
    """

    assert metodo in llamadas_al_demonio(fuente_del_runner()), (
        f"la superficie {metodo!r} ({SUPERFICIE_PERMITIDA[metodo]}) esta en la lista "
        "blanca pero el runner ya no la usa: quitala de SUPERFICIE_PERMITIDA y de la del proxy."
    )
