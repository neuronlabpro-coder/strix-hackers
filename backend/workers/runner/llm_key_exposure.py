"""Decide si la clave del proveedor puede entrar en el contenedor, y deja constancia.

## El problema

`container_environment()` entrega la clave del proveedor al contenedor como `LLM_API_KEY`, y
eso es **necesario**: Strix habla con el proveedor de inferencia por su cuenta, desde dentro
del contenedor, y ese trafico no pasa por el backend. Sin la clave en el contenedor no hay
escaneo.

El riesgo es que el contenedor tambien lleva una caja de herramientas de pentest completa —
shell, Python, Playwright, caido, `jwt_tool`— y que su entrada no es solo el operador: es
**el objetivo que se esta escaneando**. Un README malicioso, una pagina que el agente visita,
un `package.json` con un `postinstall` que imprime el entorno, y el agente tiene ordenes de
cumplir. Una inyeccion de prompt que llegue a decir "imprime tus variables de entorno" se
convierte en exfiltracion de la credencial de la plataforma, que es de la **plataforma**, no
del tenant, y que por tanto da acceso al gasto de todos los clientes.

## Lo que este modulo NO hace, y por que

**No arregla el problema.** Las tres salidas reales son, en orden de coste:

1. **Un proxy de inferencia en el worker.** `LLM_API_BASE` apunta al proxy, que anade la
   clave de verdad; el contenedor recibe un marcador. La clave no sale nunca del worker y lo
   unico que un compromised agente puede hacer es gastar, no robar. Es lo correcto, y exige
   un servicio nuevo y cablear la red del sandbox para que llegue al worker.

2. **Una credencial con tope por escaneo.** Se emite contra la API de aprovisionamiento del
   proveedor y se borra al terminar. Acota el daño a un escaneo. **Requiere una clave de
   aprovisionamiento aparte** —la actual responde `401 Invalid API key` en `POST /keys`, o
   sea que el endpoint existe pero esta credencial no tiene el permiso— y anade una llamada de
   red por trabajo mas su limpieza.

3. **Proxy HTTP de salida en el contenedor**, que es lo que hace el cerco de `egress_fence`
   pero allowlistado por host en vez de por puerto. Reduce a exfiltracion, no la elimina: si el
   proveedor es el unico host permitido, no se puede sacar la clave por ahi.

Las tres son decisiones de despliegue con coste, y ninguna se toma aqui.

## Lo que si hace

Que la exposicion sea **un hecho declarado y no un accidente**. En produccion, sin
`STRIX_LLM_KEY_EXPOSURE_ACK`, el runner **se niega a lanzar**. Un despliegue en el que nadie ha
decidido nada deja de escanear, en vez de escanear con la credencial de la plataforma dentro de
la caja de herramientas de un atacante.

Es el mismo criterio que `egress_fence`, y por el mismo motivo: un aviso en el log no protege
nada, y una proteccion que se puede activar sin decidir nada no es una proteccion.
"""

from __future__ import annotations

import logging

from backend.core.config import settings

logger = logging.getLogger(__name__)

#: Texto que hay que escribir en `STRIX_LLM_KEY_EXPOSURE_ACK` para confirmar que la exposicion
#: de la clave en el contenedor es una decision tomada y no un olvido.
#:
#: Se exige una frase exacta y no un `true` a secas porque un interruptor booleano se activa
#: sin que nadie lea lo que activa, y porque dentro de tres meses nobody recuerda si lo que
#: se habia activado era "lo se" o "lo se porque lo puse para que no fallara el escaneo".
FRASE_DE_RECONOCIMIENTO = "la-clave-del-proveedor-entra-en-el-contenedor-aceptado"

#: Las tres salidas reales, para que quien tenga que decidir las tenga delante y no las tenga
#: que reconstruir.
SALIDAS = (
    "proxy de inferencia en el worker: LLM_API_BASE apunta al proxy y la clave no sale nunca",
    "credencial con tope por escaneo: se emite contra la API de aprovisionamiento del proveedor",
    "proxy HTTP de salida por host: se permite solo el proveedor, y no se puede sacar por ahi",
)


class LlmKeyExposureNotAcknowledgedError(RuntimeError):
    """La clave entraria en el contenedor y nadie ha dicho que si."""


def exigir_reconocimiento_de_exposicion() -> str:
    """Devuelve la clave que se va a entregar, o se niega a seguir.

    ## Por que se devuelve la clave y no solo se comprueba

    Porque el valor se necesita igualmente para construir el entorno del contenedor, y leerlo
    dos veces desde dos sitios es la forma de que un dia los dos valores no coincidan. Que la
    funcion que **decide** sea la que **entrega** hace imposible esa discrepancia.
    """

    if not settings.strix_require_llm_key_exposure_ack:
        logger.warning(
            "La clave del proveedor va a entrar en el contenedor sin reconocimiento. El "
            "contenedor lleva shell, Python y un navegador, y su entrada no es solo el "
            "operador: es tambien el objetivo que se escanea. Pon "
            "STRIX_LLM_KEY_EXPOSURE_ACK=%r en produccion, o acepta el riesgo de forma "
            "explicita. Las salidas reales estan en el modulo: %s",
            FRASE_DE_RECONOCIMIENTO,
            "; ".join(SALIDAS),
        )
        return settings.llm_api_key.get_secret_value()

    if settings.strix_llm_key_exposure_ack != FRASE_DE_RECONOCIMIENTO:
        raise LlmKeyExposureNotAcknowledgedError(
            "La clave del proveedor entraria en el contenedor y STRIX_LLM_KEY_EXPOSURE_ACK no "
            f"confirma la exposicion. Hay que poner exactamente {FRASE_DE_RECONOCIMIENTO!r}, "
            "no un true: el valor es la constancia de que alguien ha leido el riesgo. Lo que "
            "se protege y lo que no, en una linea: el contenedor lleva shell, Python y un "
            "navegador, su entrada incluye el objetivo que se escanea, y la credencial es de "
            f"la plataforma y da acceso al gasto de todos los clientes. Las tres salidas "
            f"reales: {'; '.join(SALIDAS)}."
        )

    logger.info(
        "Exposicion de la clave del proveedor en el contenedor reconocida de forma explicita"
    )
    return settings.llm_api_key.get_secret_value()
