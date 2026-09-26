"""Tarea de Celery para el despacho de webhooks.

Está en un módulo propio, y no dentro de `dispatcher.py`, por el mismo motivo que
`repositories/tasks.py`: Celery descubre las tareas importando el módulo que las declara,
y el módulo de lógica no debería depender de que alguien importe el decorador para que la
cola funcione. Separarlos hace que la lógica sea importable desde una prueba sin que la
prueba levante Celery.
"""

from __future__ import annotations

import logging

from backend.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

#: Límite de tiempo blando de una entrega concreta, en segundos. El peor caso son tres
#: intentos con 5 s de timeout cada uno más las esperas de retroceso, y se deja margen para
#: que el worker no lo mate antes de poder registrar por qué falló. Si el dispatcher no
#: puede ni guardar el historial porque lo mataron, el diagnóstico se pierde entero.
TASK_SOFT_TIME_LIMIT_SECONDS = 60


@celery_app.task(
    bind=True,
    name="webhooks.deliver_event",
    soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
    max_retries=0,
)
def deliver_event(self: object, endpoint_id: str, event_type: str, payload_json: str) -> str:
    """Envía un evento a un endpoint y registra el resultado.

    ## Por qué `max_retries=0`

    El dispatcher **ya** reintenta tres veces con retroceso exponencial dentro de la tarea.
    Si además Celery reintentara, cada evento llegaría al cliente hasta nueve veces, y un
    receptor que responde `500` por un payload que no entiende acabaría con el mismo
    evento nueve veces. Un reintento tiene que vivir en un solo sitio, y ese es el que sabe
    distinguir un `500` transitorio de un `400` que no lo es.

    ## Por qué devuelve el motivo y no relanza

    La operación de dominio que originó el evento ya terminó. Relanzar haría que Celery
    marcara la tarea como fallida y, con `task_acks_late`, la devolvería a la cola: el
    resultado sería un reintento invisible para el usuario y una cola que crece sola. El
    motivo se devuelve como cadena y se registra en el historial de entregas, que es donde
    el usuario puede verlo.
    """

    from backend.apps.webhooks.dispatcher import deliver_event_task

    del self
    try:
        resultado = deliver_event_task(endpoint_id, event_type, payload_json)
    except Exception as error:
        # Se traga la excepción y la registra con su tipo, nunca con su contenido: un
        # `ValueError` de descifrado puede llevar material del secreto en el mensaje.
        logger.error(
            "La entrega del webhook %s para %s fallo: %s",
            endpoint_id,
            event_type,
            type(error).__name__,
        )
        return "error"
    logger.info(
        "Entrega del webhook %s para %s terminada: %s", endpoint_id, event_type, resultado
    )
    return resultado
