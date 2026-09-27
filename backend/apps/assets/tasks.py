"""Tareas Celery de la superficie de ataque.

## Por qué la tarea repite la comprobación de verificación

Porque el endpoint y la tarea no comparten el instante. El endpoint comprueba `is_verified`
y encola; entre ambas cosas pasan milisegundos, o un reintento, o un mensaje que se reencola
después de que alguien haya borrado el dominio. La tarea vuelve a leer la fila y vuelve a
comprobar.

No es duplicación gratuita: es que la fila de `VerifiedDomain` es la **fuente de verdad**, y
una tarea que confía en la decisión de otro proceso está confiando en algo que ya no
existe. Si el dominio se borró, el `CASCADE` se llevó sus activos y no hay dónde escribir; si
se desseverificó —que no tiene ruta, pero el esquema lo permite— descubrirlo sería escanear
sin autorización.

## Por qué `try/finally` y no un `except` que limpia

Porque hay dos formas de terminar: bien y mal. Un `except` solo cubre la segunda, y un
`BaseException` —`CancelledError` de Celery, `SystemExit`, una caída de memoria— no pasa por
ninguno de los dos. El `finally` es el único sitio que se ejecuta en los tres casos, que es
justo donde tiene que ir la liberación de recursos.

Es la regla de AGENTS.md §5.4: los workers que lanzan trabajo externo capturan señales y
limpian con `try/finally` garantizado.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from backend.apps.assets import discovery, service
from backend.core.database import AsyncSessionLocal
from backend.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _run_discovery(domain_id: str, organization_id: str) -> dict[str, object]:
    """Ejecuta el descubrimiento de un dominio verificado y persiste el resultado.

    ## Por qué **no** borra los activos que ya no aparecen

    Porque un subdominio que se ha dado de baja sigue siendo un **hallazgo**: es un nombre
    que el cliente publicaba y ya no publica, y eso es configuración abandonada, que es una de
    las cosas que más se busca en una auditoría. Borrarlo al reescanear convertiría la
    plataforma en un registro de lo que existe *ahora*, que es un escáner, y no en un
    inventario, que es lo que se ha pedido.

    Lo que sí se actualiza es `last_scanned_at` de los que siguen existiendo, y eso es lo que
    permite distinguir "está y lo hemos comprobado hace un minuto" de "no lo hemos vuelto a
    mirar". Marcarlo todo como visto sin comprobar sería peor que no marcar nada, y por eso
    el borrado de un activo caducado es una decisión explícita del cliente, no un efecto
    colateral de un reescaneo.
    """

    async with AsyncSessionLocal() as session:
        try:
            dominio = await service.get_domain(
                session, uuid.UUID(organization_id), uuid.UUID(domain_id)
            )
        except service.DomainNotFoundError:
            # El dominio se borró entre el encolado y la ejecución. No es un fallo: el
            # `CASCADE` ya se llevó los activos y no hay nada que hacer.
            logger.info(
                "Descubrimiento omitido: el dominio %s ya no existe", domain_id
            )
            return {"estado": "omitido", "motivo": "dominio_inexistente"}

        if not dominio.is_verified:
            # Ver la nota de la cabecera: la fila es la fuente de verdad, no la decisión del
            # proceso que encoló.
            logger.warning(
                "Descubrimiento rechazado: el dominio %s ya no esta verificado",
                dominio.domain_name,
            )
            return {"estado": "omitido", "motivo": "no_verificado"}

        nombre = dominio.domain_name
        id_dominio = dominio.id
        id_organizacion = dominio.organization_id
        # La conexión se suelta **antes** de las consultas DNS. Las de resolución son lentas
        # —hasta `dns_timeout_seconds` por candidato— y mantener una sesión de la base de
        # datos abierta durante minutos en un worker es ocupar un pool sin necesidad. El
        # `pool_pre_ping` de la siguiente conexión resuelve el caso de que la sesión se haya
        # invalidado mientras tanto.
        await session.rollback()

    informe = await discovery.discover(nombre)
    filas = discovery.report_to_assets(informe)

    async with AsyncSessionLocal() as session:
        nuevos = await service.merge_assets(
            session, id_dominio, id_organizacion, filas
        )

    return {
        "estado": "completado",
        "dominio": nombre,
        "candidatos": informe.candidates_tried,
        "resueltos": informe.resolved,
        "nuevos": nuevos,
        "timeouts": informe.timed_out,
        "errores": informe.errors,
    }


@celery_app.task(
    name="assets.discover_domain_assets",
    autoretry_for=(TimeoutError,),
    retry_backoff=True,
    retry_backoff_max=120,
    max_retries=2,
)
def discover_domain_assets(
    domain_id: str, organization_id: str
) -> dict[str, object]:
    """Encola y ejecuta el descubrimiento de activos de un dominio verificado.

    ## Por qué reintenta solo por `TimeoutError`

    Porque es el único fallo que se resuelve solo. Un `DNSException` que no sea timeout
    significa que el nombre no existe o que el protocolo falló, y reintentar produce el
    mismo resultado mientras consume un reintento del tope global de Celery, que es un
    recurso compartido. Un `TimeoutError` sí es transitorio —un resolver saturado, una
    perdida de paquetes— y el `retry_backoff` le da margen para que se recupere solo.

    Lo que **no** se reintenta nunca es un fallo de base de datos: eso no se arregla
    esperando, y reintentarlo a ciegas sobre una conexión caída multiplica los intentos sin
    cambiar el resultado. Sale como error y lo decide el llamador.

    ## Por qué devuelve un diccionario y no un entero

    Porque el worker registra el resultado y un entero no dice nada. Con el informe, un fallo
    parcial es legible: `"candidatos": 64, "resueltos": 12, "timeouts": 3` dice que la tarea
    funcionó y que hubo red lenta, que es distinto de `"candidatos": 64, "resueltos": 0`,
    que dice que el dominio no tiene subdominios publicados o que el DNS del cliente no
    responde. Sin esa distinción, el log de un fallo parcial y el de un inventario vacío son
    el mismo texto.
    """

    resultado = asyncio.run(_run_discovery(domain_id, organization_id))
    logger.info(
        "Descubrimiento de activos en %s: %s",
        domain_id,
        ", ".join(f"{clave}={valor}" for clave, valor in resultado.items()),
    )
    return resultado


__all__ = ["discover_domain_assets"]
