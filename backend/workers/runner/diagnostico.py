"""Traduce el fallo del runner a un **código estable** que el panel sabe explicar.

## El problema que resuelve

`StrixSandboxManager.run` envuelve casi todo en `SandboxExecutionError("Falló la
ejecución del sandbox Strix")`, y la tarea de Celery que lo lanza terminaba escribiendo
`STRIX_EXECUTION_FAILED` en `pentest_runs.error_message`. Con eso, un despliegue sin el
cerco de salida instalado, uno sin `STRIX_LLM_KEY_EXPOSURE_ACK` y uno sin la imagen del
sandbox **producian exactamente la misma línea**: cuatro palabras que no dicen qué hacer.

Es el peor sitio posible para perder el motivo. Los dos motivos más frecuentes —cerco y
reconocimiento— son de **configuración del despliegue**, no de código: se arreglan en el
host, no con un commit. Un código que no distingue entre «el código está mal» y «al host le
falta una regla de `iptables»» manda al operador a mirar el repositorio en lugar de mirar
el servidor, y el escaneo sigue sin funcionar.

## Por qué un código y no el texto de la excepción

Tres razones, y las tres son reglas del proyecto:

1. **No se filtran secretos.** `str()` de una `APIError` de Docker o de un `OSError` puede
   llevar una ruta, un nombre de registro o un fragmento de entorno. El texto crudo de una
   excepción **no** se escribe en una columna que la API devuelve al navegador.
2. **No se mete un idioma en la base de datos.** El panel se traduce (`R1` y su corolario
   i18n). Un texto en español guardado en la base saldría en inglés en la interfaz
   inglesa, y la traducción no se puede corregir sin una migración de datos.
3. **Es estable.** El código es un contrato entre el worker y el panel: se puede añadir un
   motivo nuevo sin reescribir lo que ya se guardó.

El texto accionable lo pone el panel, en el idioma del lector, a partir del código. Este
módulo solo decide **qué** pasó.

## Por qué se mira la cadena de causas y no la excepción de superficie

Porque el runner envuelve por diseño (`AGENTS.md` §5.4: el `finally` tiene que poder
limpiar y el error original tiene que quedar encadenado). La excepción que sale de
`run()` es `SandboxExecutionError`; el motivo real —`EgressFenceMissingError`,
`LlmKeyExposureNotAcknowledgedError`, `ImageNotFound`— está en `__cause__`. Mirar solo la
excepción de superficie daría el mismo código genérico para todos los casos, que es
exactamente el defecto que este módulo arregla.

Y se recorre **de la causa más profunda hacia arriba**, no al revés, porque en esa dirección el
primer acierto es el más específico y el envoltorio genérico llega el último. El ejemplo que
conviene recordar es `ImageNotFound` —«la imagen no está en el host», que sí está en la
lista— por encima del `SandboxExecutionError` que lo envuelve.

## Lo que este módulo NO afirma

Que el fallo fuera de un despliegue y no del código. Clasifica **qué excepción** se produjo y
produce un código estable, y nada más. Que el panel traduzca ese código como «revisa el host»
es una afirmación sobre la **acción**, que es del lector; este módulo no puede saber si el
host que hay que mirar es el que ejecuta el panel. Por eso el texto del panel no dice «esto es
lo que hay que arreglar en el servidor»: dice lo que este proceso no tiene.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from docker.errors import DockerException, ImageNotFound

from backend.workers.runner.egress_fence import EgressFenceMissingError
from backend.workers.runner.exceptions import (
    SandboxOutputError,
    SandboxTimeoutError,
    SandboxWorkspaceError,
)
from backend.workers.runner.llm_key_exposure import LlmKeyExposureNotAcknowledgedError

#: El código que se guardaba siempre. Se conserva **solo** para lo que no se sabe
#: clasificar, y por eso sigue siendo el último de la lista y no el primero: que aparezca
#: en un run significa que hay que abrir el registro del worker, no que el panel sabe
#: explicarlo.
#:
#: ## Por qué está también en `_FALLBACK_ELIGIBLE_ERRORS` aunque hoy no pueda ganar nada
#:
#: Porque **no puede ganar nada**: `tasks.py` construye el conjunto de fallos que mejora al
#: cambiar de modelo con los valores que `_AttemptOutcome.error_code` puede tomar, y ese campo
#: solo vale `STRIX_NONZERO_EXIT` o `None`. El código desconocido se escribe al final del
#: camino, en `_mark_failed`, cuando ya no queda otro modelo que probar. La entrada es, por
#: tanto, código muerto hoy.
#:
#: ## Por qué se deja puesta en lugar de quitarla
#:
#: Porque es la **protección del caso que puede aparecer**: si mañana un fallo no clasificado
#: deja de agotar la cadena de modelos —porque el fallo se detecta antes de consumirla—, el
#: conjunto se queda corto y ese fallo no se reintenta, sin ningún aviso de por qué.
#: `_FALLBACK_ELIGIBLE_ERRORS` está en `tasks.py`, que es de otro subagente en este turno, así
#: que aquí solo queda escrito el porqué y **dónde** tocarlo. No es un olvido: es una
#: dependencia cruzada que este fichero no puede resolver solo.
CODIGO_DESCONOCIDO = "STRIX_EXECUTION_FAILED"


@dataclass(frozen=True, slots=True)
class DiagnosticoDeFallo:
    """Qué pasó y con qué código se lo cuenta al panel."""

    codigo: str
    #: Qué comprobación lo produjo. Va al registro del worker, **no** a la base de datos:
    #: es texto de operador y está en español, y la columna que sale por la API es la del
    #: código. Existe para que quien lee el registro sepa por dónde empezar a mirar sin
    #: tener que el traceback entero delante.
    comprobacion: str


#: Motivos conocidos, del más reciente al más antiguo en la cadena de causas. Cada entrada
#: es `(tipo de excepción, código, comprobación)`.
#:
#: La clave es el **tipo**, no la instancia, y la comparación es por `isinstance`: las
#: excepciones del runner heredan unas de otras (`SandboxWorkspaceError` de
#: `SandboxError`), así que el orden de esta tupla decide cuál gana. Se prueba primero la
#: más derivada.
MOTIVOS_CONOCIDOS: tuple[tuple[type[BaseException], str, str], ...] = (
    (
        EgressFenceMissingError,
        "STRIX_EGRESS_FENCE_MISSING",
        "egress_fence.exigir_cerco_de_salida",
    ),
    (
        LlmKeyExposureNotAcknowledgedError,
        "STRIX_LLM_KEY_ACK_MISSING",
        "llm_key_exposure.exigir_reconocimiento_de_exposicion",
    ),
    (
        ImageNotFound,
        "STRIX_IMAGE_UNAVAILABLE",
        "docker: la imagen del sandbox no esta en el host",
    ),
    (
        SandboxWorkspaceError,
        "STRIX_WORKSPACE_UNAVAILABLE",
        "sandbox.setup_workspace: el host no deja preparar el workspace",
    ),
    (
        SandboxOutputError,
        "STRIX_OUTPUT_UNUSABLE",
        "sandbox: Strix no dejo un results.json legible",
    ),
    (
        SandboxTimeoutError,
        "STRIX_TIMEOUT",
        "sandbox: el motor supero el timeout configurado",
    ),
    (
        DockerException,
        "STRIX_DOCKER_UNAVAILABLE",
        "docker: el daemon no acepto la peticion",
    ),
)


def _cadena_de_causas(error: BaseException) -> Iterator[BaseException]:
    """Recorre `error` y sus `__cause__`, del envoltorio a la causa mas profunda.

    Sin repetir y sin girar en un ciclo: el tope de 16 es lo que garantiza que la funcion
    termina **aun con** una cadena de causas construida a mano que se repita, porque la
    comparacion de identidad solo cortaria el ciclo si el objeto se repitiera exactamente.
    Las cadenas reales de Python no tienen tope: `raise ... from ...` garantiza que la causa
    es anterior en el tiempo, así que no puede haber ciclo. El tope protege el caso que solo se
    puede construir a mano, que es también el caso que se prueba.
    """

    vistos: set[int] = set()
    actual: BaseException | None = error
    for _ in range(16):
        if actual is None or id(actual) in vistos:
            return
        vistos.add(id(actual))
        yield actual
        siguiente = actual.__cause__
        actual = siguiente if isinstance(siguiente, BaseException) else None


def diagnosticar_fallo(error: BaseException) -> DiagnosticoDeFallo:
    """Devuelve el código y la comprobación que explican `error`.

    ## Por qué se recorre al revés

    De la causa más profunda hacia la superficie. En `raise ... from error` el `error` es el
    motivo y el que se lanza es el envoltorio, así que el envoltorio está **al principio** de
    la cadena que devuelve `_cadena_de_causas` y el motivo al final; recorrerla al revés es lo
    que hace que el motivo **más específico** gane al envoltorio que lo contiene.

    ## Por qué eso importa con la lista que hay

    Porque el envoltorio y el motivo pueden salir los dos clasificados, y entonces gana el
    orden. `sandbox.run` envuelve en `SandboxExecutionError`, que no está en `MOTIVOS_CONOCIDOS`
    por la razón de más abajo, así que hoy el envoltorio no gana nunca y el recorrido al revés
    no cambia el resultado de ningún fallo real. Es **protección deliberada para el futuro**:
    el día que alguien meta un tipo base en la lista, el recorrido al revés es lo que impide
    que ese tipo absorba los motivos concretos que contiene.

    ## Por qué `ContainerExecutionError` no aparece como rival de `ImageNotFound`

    Porque **no está** en `MOTIVOS_CONOCIDOS`, y no por descuido: es el envoltorio de un fallo de
    arranque, y la lista lo trata como desconocido a propósito
    (`test_el_motivo_real_gana_a_traves_del_envoltorio_del_runner` lo afirma). Conviene dejarlo
    escrito porque es tentador usarlo como ejemplo de «envoltorio contra motivo» y usarlo de
    ejemplo invita a meterlo en la lista para que el ejemplo sea cierto, que es exactamente el
    cambio que rompería el diagnóstico.

    ## Por qué `SandboxError` no está en la lista

    Porque no distingue nada: es la base de cinco fallos que tienen causas distintas. Meterla
    haría que `raise SandboxError(...) from EgressFenceMissingError` se clasificara como
    genérico por el envoltorio. Los tipos que sí se distinguen están uno a uno.

    ## Por qué el tope del recorrido y el `reversed` no se pueden quitar por separado

    `_cadena_de_causas` garantiza que termina; `reversed` garantiza que gana el motivo. Quitarlos
    los dos sería volver al defecto original; quitar solo el `reversed` deja el ciclo protegido y
    el orden por defecto. Los dos tienen su prueba propia y las dos pruebas caen al quitar su
    pieza.
    """

    for causa in reversed(list(_cadena_de_causas(error))):
        for tipo, codigo, comprobacion in MOTIVOS_CONOCIDOS:
            if isinstance(causa, tipo):
                return DiagnosticoDeFallo(codigo=codigo, comprobacion=comprobacion)
    return DiagnosticoDeFallo(
        codigo=CODIGO_DESCONOCIDO,
        comprobacion=f"sin motivo clasificado: {type(error).__name__}",
    )


#: Motivos que **no** cambian por usar otro modelo de LLM. Un host sin cerco, sin
#: reconocimiento de exposicion o sin imagen va a fallar igual con los cinco modelos del
#: catalogo, y reintentarlos solo gasta la cola y confunde el registro.
#:
#: Vive aqui y no en `tasks.py` porque la pregunta es del runner —de que depende de la
#: maquina— y `tasks.py` solo necesita importarlo.
MOTIVOS_DE_DESPLIEGUE = frozenset(
    {
        "STRIX_EGRESS_FENCE_MISSING",
        "STRIX_LLM_KEY_ACK_MISSING",
        "STRIX_IMAGE_UNAVAILABLE",
        "STRIX_WORKSPACE_UNAVAILABLE",
        "STRIX_DOCKER_UNAVAILABLE",
    }
)


def es_fallo_de_despliegue(error: BaseException) -> bool:
    """¿El fallo depende de la máquina donde corre el worker y no del modelo?"""

    return diagnosticar_fallo(error).codigo in MOTIVOS_DE_DESPLIEGUE


__all__ = (
    "CODIGO_DESCONOCIDO",
    "MOTIVOS_CONOCIDOS",
    "MOTIVOS_DE_DESPLIEGUE",
    "DiagnosticoDeFallo",
    "diagnosticar_fallo",
    "es_fallo_de_despliegue",
)
