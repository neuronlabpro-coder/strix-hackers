"""Comprueba, antes de gastar un escaneo, si este despliegue puede ejecutar uno.

## El problema

El 4 de octubre un pentest falló en 0,2 s con `STRIX_EXECUTION_FAILED` y el motivo real —el
worker no podía abrir el socket Docker del host— no aparecía en ninguna pantalla. El coste de
diagnosticarlo no fue leer el motivo: fue **descubrir que había que ir a buscarlo**, porque la
única señal de la aplicación era un código que no distinguía un host mal configurado de un bug
del motor. Un escaneo de prueba cuesta créditos y crea una fila que ya no explica nada.

Este módulo convierte esa señal en una lista de comprobaciones. No sustituye al motivo del
`diagnostico.py` —los dos dicen lo mismo desde sitios distintos—: el motivo explica un fallo
que ya pasó, y esto lo evita antes de que pase.

## Por qué no se lanza un contenedor de prueba

Porque el punto es no gastarlo. Y porque un contenedor de prueba que arranca en menos de un
segundo es indistinguible de un escaneo real para el despliegue, que no tiene forma de saber
que no era de nadie.

## Por qué estas cuatro y no otras

Porque son las cuatro que hacen fallar **todos** los escaneos de golpe, todas por configuración
del despliegue. Las demás —un artefacto ilegible, un modelo que no
existe— fallan de a uno y las cuenta el propio `diagnostico.py`.

## Y por qué una de las cuatro no impide lanzar un escaneo

Porque una de ellas puede fallar sin impedir nada: con `STRIX_REQUIRE_EGRESS_FENCE=false` el
escaneo se levanta igual y lo que está mal es la red del contenedor. La lista se construyó
como «lo que impide escanear» y ese caso no encaja en ella. Se queda en la lista, que sigue
siendo el sitio donde se informa de la postura del contenedor, pero con `bloquea=False` para
que el panel no lo pinte igual que a un despliegue parado.

Y el orden es el de `sandbox.run`: se comprueban en la secuencia en la que un escaneo de verdad
se las cruzaría, para que la primera que falle sea la primera que se habría cruzado el escaneo.

## Qué afirma y qué no afirma cada comprobación

Cada fila de esta lista es una afirmación sobre **el proceso que la ejecuta**, y sobre el host
donde ese proceso corre. No es una afirmación sobre por qué fallan los escaneos.

La diferencia no es un matiz de redacción. En el despliegue de Dokploy el endpoint lo atiende
el proceso de la API y el escaneo lo lanza el worker, en otro contenedor y en otra máquina. Una
máquina de desarrollo sin `iptables` da tres comprobaciones en rojo, y ninguna de las tres es
la razón por la que falló un escaneo en el VPS: falló porque el worker no abrió el socket de
Docker, que es un motivo que **no** se puede mirar desde aquí (ver `COMPROBACIONES`).

Por eso hay dos cosas distintas y este módulo las separa:

- **Lo que a este proceso le falta** (`pasa=False`). Es un hecho, y se puede afirmar.
- **Si eso impide lanzar un escaneo** (`bloquea`). Es un hecho distinto: depende de si el
  escaneo sale de este proceso, y hay al menos un caso en que no sale igual.

Y lo que este módulo **no** hace, por principio, es decir cuál es la causa de un fallo de
escaneo. Esa afirmación solo la puede hacer `diagnostico.py`, porque solo él ha visto la
excepción. Un diagnóstico que afirma una causa que no ha observado es el mismo defecto que este
módulo vino a arreglar, en la dirección contraria.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from backend.core.config import settings
from backend.workers.runner.diagnostico import diagnosticar_fallo
from backend.workers.runner.egress_fence import hay_cerco_para, reglas_de_la_cadena
from backend.workers.runner.llm_key_exposure import exigir_reconocimiento_de_exposicion

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Comprobacion:
    """El resultado de una comprobación, con el motivo ya reducido a código.

    ## Qué significa `bloquea` y por qué no es lo mismo que `pasa`

    Porque no todas las comprobaciones que fallan impiden lanzar un escaneo, y tratar a todas
    igual es lo que hace que una tarjeta de diagnóstico enseñe urgencias donde no las hay.

    - `pasa=True` — la comprobación va bien. `bloquea` es irrelevante.
    - `pasa=False, bloquea=True` — **un escaneo lanzado desde este proceso no llegaría a
      levantar el contenedor**. Es el estado que de verdad está roto.
    - `pasa=False, bloquea=False` — **algo está mal aquí y un escaneo saldría igual**. Ejemplo:
      el cerco de salida desactivado. El contenedor se levanta, pero con la salida del host.

    La diferencia entre las dos filas malas no es de matiz: con el cerco desactivado, un escaneo
    se completa y **consume créditos**; con el cerco ausente, se niega a arrancar y no los
    consume. Presentarlos con el mismo color y el mismo texto hace que el operador no sepa
    cuál de los dos tiene delante, y el orden en que se arreglan es el orden de la urgencia.

    ## Por qué el campo está aquí y no en el enum de motivos

    Porque «bloquea» es una propiedad de la **comprobación**, no del código de error: el mismo
    motivo puede bloquear o no según dónde se compruebe. `EGRESS_FENCE_DISABLED` no bloquea;
    `STRIX_EGRESS_FENCE_MISSING` sí. Si el campo viviera en el motivo, habría que inventar dos
    códigos para el mismo hecho y el panel aprendería a deducir la urgencia del motivo, que es
    la clase de respuesta derivada que este módulo vino a evitar.
    """

    clave: str
    pasa: bool
    motivo: str | None = None
    bloquea: bool = True

    @classmethod
    def correcta(cls, clave: str) -> Comprobacion:
        return cls(clave=clave, pasa=True, motivo=None)

    @classmethod
    def fallida(cls, clave: str, motivo: str, *, bloquea: bool = True) -> Comprobacion:
        return cls(clave=clave, pasa=False, motivo=motivo, bloquea=bloquea)

    @classmethod
    def sin_bloquear(cls, clave: str, motivo: str) -> Comprobacion:
        """Una comprobación que falla pero no impide lanzar el escaneo.

        ## Por qué existe además de `fallida(..., bloquea=False)`

        Porque en el punto donde se usa, `return Comprobacion.sin_bloquear("cerco", ...)`, el
        nombre dice la conclusión. Con el parámetro puesto ahí, quien lee la línea tiene que
        saber qué significa `bloquea=False` para entender que el escaneo **sí** sale, y eso es
        justo lo que esta clase existe para que no haya que deducirlo.

        Y delega en `fallida`: la construcción del objeto tiene un solo sitio, que es donde se
        evita que las dos fábricas se separen.
        """

        return cls.fallida(clave, motivo, bloquea=False)


def _comprobar_workspace() -> Comprobacion:
    """¿Se puede escribir en el directorio de workspaces?

    ## Por qué se prueba a escribir y no a hacer `stat`

    Porque `stat` solo necesita permiso de lectura, y el runner necesita **crear** un
    directorio por escaneo. Un directorio legible y no escribible pasa el `stat` y revienta el
    escaneo, que es el caso que de verdad duele: en el despliegue documentado el directorio se
    crea con `install -d -o 10001`, y un cambio de UID lo deja exactamente así.

    Y el archivo se borra en el `finally`. Un diagnóstico que deja basura en el host es peor
    que uno que no diagnostica, sobre todo en la ruta que R5 exige que sea efímera.
    """

    raiz = Path(settings.strix_workspace_root)
    # La sonda se declara **antes** del `try` y no dentro. Dentro, el `finally` la usaría sin
    # saber si se llegó a crearla, y pyright lo dice: «possibly unbound». El motivo de moverla
    # no es el type checker, es que una variable que existe solo en un camino es una variable
    # que hay que probar antes de usar, y ese `try` de dentro era el precio. Con la ruta
    # calculada de antemano, el `finally` es un `unlink` con `missing_ok`, que ya no necesita
    # preguntar.
    sonda = raiz / ".fenix-readiness"
    try:
        raiz.mkdir(parents=True, exist_ok=True)
        sonda.write_text("ok", encoding="utf-8")
    except OSError:
        return Comprobacion.fallida("workspace", "STRIX_WORKSPACE_UNAVAILABLE")
    finally:
        # Se borra pase lo que pase. Un diagnóstico que deja un archivo en el directorio que R5
        # exige que sea efímero sería un defecto suyo, y `missing_ok=True` cubre el caso de que
        # el `mkdir` ni llegara a completarse sin necesitar un `if` ni un `try` dentro.
        try:
            sonda.unlink(missing_ok=True)
        except OSError:
            pass
    return Comprobacion.correcta("workspace")


def _comprobar_cerco() -> Comprobacion:
    """¿El cerco de salida cubre la subred que Docker daría a un trabajo?

    ## Por qué se usa el rango de configuración y no una red real

    Porque crear una red para comprobar si hay cerco es crear el recurso que el cerco protege, y
    hacerlo en cada consulta metería redes sin usar en el host. El rango de
    `strix_network_pool` es lo que el runner **espera**, y la comprobación es exactamente la de
    `exigir_cerco_de_salida` con ese rango. Si Docker se saliera del rango, eso lo avisa el
    escaneo con su propio código, no esta consulta.

    ## Por qué con el interruptor en false no se marca como que pasa

    Porque `STRIX_REQUIRE_EGRESS_FENCE=false` no pone el cerco: apaga la comprobación. Un
    despliegue que lo apaga en producción tiene un contenedor con salida completa a la red
    interna, y reportarlo como «correcto» sería mentir sobre el estado de seguridad. Se
    distingue con un motivo propio, `EGRESS_FENCE_DISABLED`, que el panel explica aparte.

    ## Por qué aquí falla sin bloquear

    Porque es el único caso de los cuatro en que el escaneo **sale igual**. Con el interruptor
    en `false`, `exigir_cerco_de_salida` avisa y devuelve, así que `sandbox.run` levanta el
    contenedor y el escaneo se completa. Lo que está mal es la postura del contenedor, no la
    capacidad de escanear: el contenedor sale hacia los metadatos de la nube, la red de
    Tailscale, Redis y PostgreSQL del despliegue.

    Es exactamente por lo que esta comprobación no puede ir en rojo al lado de las que sí
    bloquean: un escaneo con el cerco desactivado **consume créditos y devuelve resultado**, y
    quien lo ve marcado igual que «no hay `iptables`» entiende que no puede escanear, cuando lo
    que no puede es escanear de forma segura. La diferencia entre los dos fallos es la
    diferencia entre un despliegue que no trabaja y un despliegue que trabaja sin red.
    """

    if not settings.strix_require_egress_fence:
        return Comprobacion.sin_bloquear("cerco", "EGRESS_FENCE_DISABLED")
    if hay_cerco_para(settings.strix_network_pool):
        return Comprobacion.correcta("cerco")
    return Comprobacion.fallida("cerco", "STRIX_EGRESS_FENCE_MISSING")


def _comprobar_cerco_instalado() -> Comprobacion:
    """¿Existe siquiera la cadena `DOCKER-USER` con la regla del script?

    Se separa de `_comprobar_cerco` porque son dos preguntas distintas: «no hay `iptables`» es
    un host que no es Linux —o un despliegue en un contenedor sin el binario—, y «hay
    `iptables` pero no la regla» es un host al que le falta un paso del arranque. El motivo es
    el mismo por el código de error, pero el registro que hay que mirar no.

    ## Por qué esta comprobación bloquea y no es solo informativa

    Porque **no puede fallar sin que falle también `cerco`**: `hay_cerco_para` devuelve `False`
    cuando `reglas_de_la_cadena()` está vacía, así que esta en rojo implica la otra en rojo. No
    es un bloqueante independiente, pero el escaneo tampoco sale por otra vía: con el requisito
    activo, `exigir_cerco_de_salida` se negará a lanzarlo.

    ## Y por qué no se marca como «no bloquea» por el hecho de ser redundante

    Porque en un host de producción sin la cadena instalada, marcarla como informativa la
    escondería entre los avisos, que es exactamente donde se cuela un despliegue sin red.
    Redundante no quiere decir inocua: quiere decir que otra comprobación va a decir lo mismo un
    segundo después. La urgencia la tienen las dos, y solo una puede arreglarla.
    """

    if reglas_de_la_cadena():
        return Comprobacion.correcta("iptables")
    return Comprobacion.fallida("iptables", "STRIX_EGRESS_FENCE_MISSING")


def _comprobar_reconocimiento_de_clave() -> Comprobacion:
    """¿Está tomada la decisión de exponer la clave del proveedor dentro del contenedor?

    Se llama a la función que **decide y entrega** en vez de leer el interruptor: así, si algún
    día el requisito cambia, esta comprobación cambia con él sin que haya dos sitios que
    disagree.
    """

    try:
        exigir_reconocimiento_de_exposicion()
    except Exception as error:
        return Comprobacion.fallida("clave", diagnosticar_fallo(error).codigo)
    return Comprobacion.correcta("clave")


#: El orden de ejecución y, con él, el orden en que se muestran.
#:
#: ## Por qué aquí **no** está la comprobación del demonio Docker ni la de la imagen
#:
#: Porque las dos se quitaron, y el motivo es de seguridad y no de gusto.
#:
#: La primera versión de este módulo consultaba al demonio para responder «¿puede este
#: despliegue escanear?», y `test_runner_docker_surface.py` —la lista blanca de la superficie de
#: Docker del worker— cayó, y con razón. Esa consulta no estaba en la lista, y **añadirla es
#: abrir una puerta más** al worker, que tiene el socket del demonio montado. La lista blanca
#: existe para que la superficie sea la mínima; ampliarla para que un diagnóstico pueda preguntar
#: es cambiar la protección por una comodidad.
#:
#: Y el nombre de la superficie que se quito no aparece aquí a propósito: el escáner de la prueba
#: lee el **código**, no los comentarios, así que escribirlo en la explicación de por qué se quitó
#: haría que la prueba siguiera viendo una superficie que ya no existe. Es una ironía incómoda
#: pero real: documentar el arreglo con el texto exacto del defecto reintroduce el defecto.
#:
#: Y hay un segundo motivo, que es el que de verdad importa: **esta comprobación responde sobre el
#: proceso que la ejecuta**, y el proceso que ejecuta el endpoint es el de la API. En un despliegue
#: de un solo host —desarrollo, y el caso normal— API y worker comparten máquina y la respuesta
#: vale. En el despliegue de Dokploy el worker corre en otro contenedor, y entonces «el demonio
#: responde» dice algo del host de la API, que no es el host donde va a escanear. Informar eso
#: como si fuera el estado del motor es peor que no informar: el panel daría verde con un
#: despliegue que no escanea, que es la clase de respuesta falsa que hace que un diagnóstico
#: deje de leerse.
#:
#: Lo que sí queda son las **cuatro** comprobaciones de `COMPROBACIONES`, que leen
#: **configuración y sistema de ficheros**: el directorio de trabajo, la cadena de `iptables`, el
#: cerco para el rango configurado y el reconocimiento de la clave. Son las que un operador puede
#: corregir desde el servidor donde está el panel, y contestan una pregunta acotada: «¿este
#: proceso puede levantar un contenedor que salga filtrado y con la clave que toca?».
#:
#: Y esa pregunta es más estrecha que la que el panel se tomaba antes. Nada de esto afirma por
#: qué falló un escaneo lanzado en otra máquina: ver el motivo es cosa de `diagnostico.py`, que
#: clasifica después de que pase.
#:
#: Para lo que no se puede comprobar desde aquí —el demonio y la imagen— el motivo llega por el
#: otro camino: `diagnostico.py` lo clasifica y el panel lo explica cuando **ya** ha pasado. Ese
#: camino es más lento, y es el único que puede decir una causa sin inventarla.
COMPROBACIONES = (
    _comprobar_workspace,
    _comprobar_cerco_instalado,
    _comprobar_cerco,
    _comprobar_reconocimiento_de_clave,
)


def comprobar_disponibilidad() -> tuple[bool, list[Comprobacion]]:
    """Ejecuta todas las comprobaciones y devuelve si el despliegue puede escanear.

    ## Por qué ninguna comprobación propaga su excepción

    Porque este endpoint existe para **diagnosticar un despliegue roto**. Si la comprobación
    del demonio Docker lanzara, el endpoint devolvería un `500` y el operador se quedaría sin
    la lista de comprobaciones, que es justo lo que vino a buscar. Cada comprobación convierte
    su fallo en un motivo y sigue.

    ## Por qué no se corta en la primera que falla

    Porque son independientes y se arreglan por separado: un host puede tener el socket mal y
    el cerco sin instalar a la vez, y sacar solo el primero de la lista haría que el operador
    volviera a consultar y recibiera el segundo. Es un viaje de ida y vuelta por el mismo
    fallo.
    """

    resultados: list[Comprobacion] = []
    for comprobacion in COMPROBACIONES:
        try:
            resultados.append(comprobacion())
        except Exception as error:
            logger.exception("La comprobación %s no se pudo completar", comprobacion.__name__)
            resultados.append(
                Comprobacion.fallida(
                    comprobacion.__name__.removeprefix("_comprobar_"),
                    diagnosticar_fallo(error).codigo,
                )
            )
    return all(resultado.pasa for resultado in resultados), resultados


def bloquea_el_escaneo(comprobaciones: list[Comprobacion]) -> bool:
    """¿Alguna comprobación fallida impide lanzar un escaneo desde este proceso?

    ## Por qué es una función aparte y no un segundo valor de `comprobar_disponibilidad`

    Porque `listo` es un hecho —«pasa todo»— y esto es una **derivación** de las comprobaciones,
    que el router necesita para pintar dos grupos distintos. Que la cuenta viva aquí y no en el
    router es lo que evita que el panel decida por su cuenta qué es urgente: si mañana se
    añade una comprobación que falla sin bloquear, quien tiene que acordarse de contarla es
    este módulo, no un componente de React.

    ## Por qué se cuentan las fallidas y no todas

    Porque una comprobación que pasa nunca bloquea, aunque su `bloquea` valga `True` por
    defecto. Preguntar por `not pasa and bloquea` es lo que hace que el resultado no dependa de
    un valor por defecto que no significa nada en una fila que va bien.
    """

    return any(not comprobacion.pasa and comprobacion.bloquea for comprobacion in comprobaciones)


__all__ = (
    "COMPROBACIONES",
    "Comprobacion",
    "bloquea_el_escaneo",
    "comprobar_disponibilidad",
)
