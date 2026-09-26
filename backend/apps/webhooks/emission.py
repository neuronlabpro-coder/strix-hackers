"""Emisión de eventos de dominio hacia los webhooks salientes.

Este módulo es el **único** puente entre el dominio y el motor de webhooks. Ningún servicio
de pentests, vulnerabilidades o facturación llama a `celery_app.send_task` ni consulta
`webhook_endpoints`: llaman a las funciones de aquí, y este módulo decide cómo.

## Por qué una función asíncrona y no la `enqueue_event` síncrona

`dispatcher.enqueue_event` es síncrona y abre su propio motor con `asyncio.run()`. Desde un
servicio de dominio, que ya está dentro de un bucle de eventos, eso lanza
`RuntimeError: asyncio.run() cannot be called from a running event loop`. Es exactamente el
motivo por el que el motor llevaba meses sin un solo emisor: la función se probó en
aislamiento, desde un script sin bucle, y no hay forma de que un test así detecte que no
funciona en su único contexto de uso real.

`publish_event` es asíncrona y **reutiliza la sesión del llamante**: no abre un motor
nuevo, no crea un pool de conexiones para cada evento y no compite por la conexión con la
transacción que la llamó. El coste de emitir es una consulta.

## Por qué no se emite nunca antes del commit

Todos los puntos de emisión están **después** de su `commit`. Un webhook es una llamada
saliente que un tercero puede atender de inmediato: si se encolara antes de confirmar, el
cliente que lo recibe podría llamar a la API y no ver todavía el escaneo completado que el
evento anuncia. Un evento que describe un estado que aún no es visible es peor que un
evento que llega un segundo tarde.

## Por qué no propaga excepciones

Emitir es un efecto secundario de una operación que ya tuvo éxito. Un broker caído, un
endpoint mal configurado o una tabla de suscripciones inaccesible no pueden hacer que un
escaneo que terminó bien se reporte como fallido. Todas las funciones de aquí devuelven un
contador y registran el fallo; ninguna lanza.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.webhooks.dispatcher import logger, subscribed_endpoint_ids
from backend.apps.webhooks.events import EventType

#: Tarea de Celery que entrega el evento. Se referencia por su nombre de cadena, y no por
#: el objeto de la tarea, porque el registro de las tareas ocurre en `celery_app` y
#: importarlo desde aquí crea un ciclo: `celery_app` importa los workers, los workers
#: importan este módulo, y este importa `celery_app`.
DELIVER_TASK_NAME = "webhooks.deliver_event"


async def publish_event(
    session: AsyncSession,
    event_type: EventType | str,
    organization_id: uuid.UUID,
    payload: dict[str, Any],
) -> int:
    """Encola el evento para los endpoints suscritos. Devuelve cuántos son.

    ## Por qué el payload se serializa aquí y no en la tarea

    La tarea recibe un `str`. Si recibiera el `dict`, Celery lo serializaría con su
    codificador propio, que ordena claves de forma distinta entre versiones y convierte los
    tipos que encuentra a algo que la entrega tiene que volver a interpretar. Fijar el
    formato en el punto de emisión hace que la firma HMAC que calcula el receptor dependa
    solo de lo que se decidió aquí, y que dos eventos con los mismos datos producen
    siempre el mismo cuerpo y por tanto la misma firma.

    ## Por qué se cuentan y se enolan con la misma lista

    `subscribed_endpoint_ids` se llama **una** vez. Consultar dos veces —una para contar y
    otra para encolar— es como aparece un "despachado a 2 endpoints" en el log y solo llega
    a uno, si la suscripción cambia entre las dos consultas.
    """

    nombre = event_type.value if isinstance(event_type, EventType) else str(event_type)
    try:
        destinos = await subscribed_endpoint_ids(session, organization_id, nombre)
    except Exception as error:
        # Una consulta fallida significa que no se puede saber a quién enviar. No se lanza:
        # el cambio de dominio ya está confirmado y se queda confirmado.
        logger.error(
            "No se pudieron resolver los endpoints suscritos a %s para el tenant %s: %s",
            nombre,
            organization_id,
            type(error).__name__,
        )
        return 0

    if not destinos:
        return 0

    try:
        cuerpo = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as error:
        # Un payload no serializable es un error de programación. Se registra el tipo del
        # error y se devuelve 0: emitirlo a los cinco endpoints a la vez produciría cinco
        # entregas que fallarían en el receptor con el mismo error de serialización, y el
        # diagnóstico llega igual por el log.
        logger.error(
            "El payload de %s para el tenant %s no es serializable: %s",
            nombre,
            organization_id,
            type(error).__name__,
        )
        return 0

    from backend.workers.celery_app import celery_app

    for endpoint_id in destinos:
        try:
            celery_app.send_task(DELIVER_TASK_NAME, args=[endpoint_id, nombre, cuerpo])
        except Exception as error:
            # Un endpoint que no se pudo encolar no impide encolar los demás: son
            # independientes, y perder todos por el fallo de uno sería lo contrario.
            logger.error(
                "No se pudo encolar la entrega del endpoint %s para %s: %s",
                endpoint_id,
                nombre,
                type(error).__name__,
            )
    return len(destinos)


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #
#
# Se construyen aquí y no en el punto de emisión para que el **contrato del payload** esté
# en un solo sitio. Un payload escrito en cada rama de un `if` diverge en cuanto dos ramas
# se escriben en días distintos, y un consumidor de webhooks no puede arreglarlo: solo puede
#UMN-observar que dejó de recibir un campo.
#
# Todos los importes viajan como **cadena decimal**, nunca como `float`. La firma HMAC se
# calcula sobre este mismo cuerpo, así que un `250.1` en coma flotante produciría una
# firma distinta de la de `"250.1"` según por dónde se serialice, y el receptor la
# rechazaría sin motivo aparente.


def _decimal(valor: Decimal | int | float) -> str:
    """Un importe como cadena decimal exacta.

    `float` entra por `str()` y no por `Decimal(...)` a propósito: `str(0.1)` es `"0.1"`,
    que es lo que el emisor quiso escribir, mientras que `Decimal(0.1)` es
    `0.1000000000000000055511151231257827`, que es lo que el binario contiene y que nadie
    pidió.
    """

    if isinstance(valor, Decimal):
        return str(valor)
    return str(valor)


def pentest_payload(
    *,
    run_id: uuid.UUID,
    status: str,
    target_type: str,
    target_value: str,
    scan_mode: str,
    started_at: datetime | None,
    finished_at: datetime | None,
    error_code: str | None = None,
    findings_count: int = 0,
) -> dict[str, Any]:
    """Evento de ciclo de vida de un escaneo.

    `target_value` se incluye porque es lo que el receptor necesita para correlacionar con su
    propia inventario, y no se puede derivar de otra parte del cuerpo.
    """

    return {
        "run_id": str(run_id),
        "status": status,
        "target_type": target_type,
        "target": target_value,
        "scan_mode": scan_mode,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "error_code": error_code,
        "findings_count": findings_count,
    }


def vulnerability_created_payload(
    vulnerabilities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hallazgos nuevos de una ejecución.

    ## Por qué **un** evento con la lista y no uno por hallazgo

    La unidad de la transacción es la ingesta: se confirman todos los hallazgos del
    informe o ninguno. Emitir un evento por hallazgo convertiría un informe con cincuenta
    hallazgos en cincuenta entregas HTTP con sus cinco reintentos, y la mayoría de esos
    cincuenta seríannotifications de un consumidor que solo quería "este escaneo ya está
    ingerido".

    El receptor que necesite reaccionar a cada hallazgo itera la lista, que viaja completa
    dentro del mismo evento. El catálogo de eventos ya separa `vulnerability.created` de
    `vulnerability.status_changed`, así que quien quiera uno y otro los sigue distinguiendo.
    """

    return {
        "count": len(vulnerabilities),
        "vulnerabilities": vulnerabilities,
    }


def vulnerability_status_payload(
    *,
    vulnerability_id: uuid.UUID,
    previous_status: str,
    new_status: str,
    severity: str,
    title: str,
) -> dict[str, Any]:
    """Cambio de estado en triaje.

    `previous_status` viaja porque sin él el consumidor solo ve el destino y no puede
    distinguir una regresión —de `FIXED` a `OPEN`— de un avance. Y una regresión es
    justamente el evento que más importa recibir.
    """

    return {
        "vulnerability_id": str(vulnerability_id),
        "previous_status": previous_status,
        "status": new_status,
        "severity": severity,
        "title": title,
    }


def pr_review_payload(
    *,
    review_id: uuid.UUID,
    repository_id: uuid.UUID,
    pr_number: int,
    status: str,
    findings_count: int,
    blocking: bool,
    review_url: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Desenlace de una revisión de pull request."""

    return {
        "review_id": str(review_id),
        "repository_id": str(repository_id),
        "pr_number": pr_number,
        "status": status,
        "findings_count": findings_count,
        "blocking": blocking,
        "review_url": review_url,
        "error_code": error_code,
    }


def credits_purchased_payload(
    *,
    amount: Decimal | int,
    balance_after: Decimal | int,
    session_id: str | None,
) -> dict[str, Any]:
    """Compra acreditada.

    `amount` va como cadena decimal por la razón de la firma: el receptor calcula el HMAC
    sobre este mismo texto, y si el importe viajara como número JSON su
    representación dependería de quién lo serializara.
    """

    return {
        "credits": _decimal(amount),
        "balance_after": _decimal(balance_after),
        "payment_session_id": session_id,
    }


def credits_low_payload(
    *,
    balance: Decimal | int,
    threshold: Decimal | int,
) -> dict[str, Any]:
    """Saldo por debajo del umbral configurado.

    `threshold` viaja con el evento para que el receptor pueda decidir si su cuenta necesita
    actuar: un saldo de 40 con umbral 50 y un saldo de 40 con umbral 10 no son el mismo
    aviso, y sin la referencia el consumidor tendría que conocer la configuración de la
    plataforma.
    """

    return {
        "balance": _decimal(balance),
        "threshold": _decimal(threshold),
    }


def should_warn_low_credits(
    balance: Decimal, threshold: Decimal, previous_balance: Decimal | None
) -> bool:
    """Si el saldo cruzó el umbral hacia abajo, y no lleva ya tiempo debajo.

    ## Por qué se avisa del **cruce** y no de estar por debajo

    Si se emitiera en cada deducción mientras el saldo siga bajo el umbral, un cliente que
    no recarga recibiría el mismo aviso en cada escaneo: el receptor empezaría a ignorar el
    evento, que es justo lo contrario de lo que sirve un aviso de saldo bajo.

    Se emite una vez por cruce: de estar por encima a estar por debajo. Volver a avisar exige
    que el saldo vuelva a subir y vuelva a bajar, que es una recarga —o una corrección
    administrativa— y por tanto información nueva.

    Con `previous_balance` desconocido —una primera observación, o un saldo que ya estaba
    bajo cuando se empezó a medir— se avisa, porque no saber si cruzó no es motivo para
    tragarse el aviso: el receptor puede deduplicar por su cuenta y un aviso perdido no se
    recupera.
    """

    if threshold <= 0:
        return False
    if balance > threshold:
        return False
    if previous_balance is None:
        return True
    return previous_balance > threshold >= balance
