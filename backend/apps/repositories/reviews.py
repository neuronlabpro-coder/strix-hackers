"""Lanzar a mano el análisis de seguridad de un pull request.

## Qué resuelve

Hasta ahora la única forma de que una revisión de PR se analizara era que un **webhook** del
proveedor lo trajera. El panel tenía la tabla de revisiones, el KPI y los filtros, y **ninguna
acción**: no había forma de pedir el análisis de un PR que ya estaba en la tabla, o de
reintentar uno que falló. La función que el producto vende —«analiza el código de un pull
request»— solo se disparaba por un evento externo.
 *
 * Y el caso de uso real es el segundo: una revisión en `FAILED` de la que nadie sabe por
 * qué. Con webhook, la única forma de volver a intentarlo era esperar a un commit nuevo,
 * que llega cuando el autor quiere y no cuando el usuario necesita el resultado.

## Por qué no reusa `queue_pentest` de `pentests/service.py`

Porque esa función crea un `PentestRun` nuevo apuntando a un objetivo del tenant, y una revisión
de PR **ya tiene** su `PullRequestReview` con su `pr_number`, sus ramas y sus SHAs. Crear un run
independiente y asociarlo después dejaría dos caminos para el mismo trabajo, y R4 no los tolera:
es exactamente la clase de defecto que la auditoría ya encontró una vez en este mismo módulo,
cuando el pipeline construía el `PentestRun` a mano sin pasar por el cobro.

## Por qué el cobro lo hace el pipeline y no este servicio

Porque el precio lo declara el propio `run` que el pipeline crea —`scan_mode=QUICK`— y ese run
lo crea `_claim_review`. Cobrar aquí significaría conocer el precio antes de que exista el run,
y R1 prohíbe escribir un precio en el código. Lo que sí hace este servicio es **no duplicar** el
cobro: encola y deja. Si el pipeline no llega a reclamar la revisión, no se cobra nada, porque
el cobro está en el mismo bloque de transacción que la creación del run.

Eso también significa que este servicio puede encolar sin saldo y el workflow lo truthfully
marca `ERROR` con `PR_INSUFFICIENT_CREDITS` en vez de devolver un `402`: hay una diferencia real
entre «no se pudo encolar porque no hay cola» y «se encoló y no se pudo pagar», y este es el
segundo caso. Ver `_marcar_revision_sin_saldo`.

## Qué hay además del lanzamiento

Una reconciliación, `reconciliar_revisiones_huerfanas`, que cierra las revisiones que se
quedaron esperando a un worker que no existe. Está en este fichero, y no en `tasks.py`, porque
es política de revisiones —qué estado es terminal, cuál es relanzable y qué se avisa— y esa
política vive aquí. `tasks.py` solo la invoca, igual que solo invoca `lanzar_analisis_de_review`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.models import PRReviewStatusEnum, PullRequestReview, Repository
from backend.apps.webhooks.emission import (
    EventType,
    pr_review_payload,
    publish_event,
)
from backend.core.config import settings

logger = logging.getLogger(__name__)

#: La firma del encolado. Es la misma que `get_dispatch_pentest_run`, y por la misma razón: la
#: anotación de FastAPI vive en el router y el servicio no depende del framework.
DispatchDependency = Callable[[str], str]


class ReviewNotLaunchableError(LookupError):
    """La revisión no existe, no es de esta organización, o no se puede relanzar."""


class ReviewDispatchError(RuntimeError):
    """La revisión se puso en cola pero no llegó a la cola de trabajo."""

    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__("No se pudo encolar el análisis de la revisión")
        self.review_id = review_id


#: Los estados desde los que tiene sentido pedir un análisis.
#:
#: `QUEUED` y `SCANNING` quedan fuera a propósito: relanzar algo que ya está corriendo
#!: produce dos contenedores de escaneo para el mismo commit, y dos escaneos del mismo PR no
#: son "más seguridad", son la misma seguridad cobrada dos veces. `PASSED` y `FAILED` sí están:
#: un PR al que se le han añadido commits cambia de revisión —y entonces tiene fila nueva—, pero
#: un `FAILED` del que nadie sabe por qué es exactamente el caso que el botón del panel resuelve.
ESTADOS_RELANZABLES = frozenset(
    {
        PRReviewStatusEnum.PASSED,
        PRReviewStatusEnum.FAILED,
        PRReviewStatusEnum.ERROR,
    }
)


#: Los estados en los que una revisión **no** está esperando a nadie. `ERROR` está: es terminal y
#: además es relanzable, que es lo que hace que el botón del panel aparezca.
ESTADOS_TERMINALES = frozenset(
    {
        PRReviewStatusEnum.PASSED,
        PRReviewStatusEnum.FAILED,
        PRReviewStatusEnum.ERROR,
    }
)


async def reconciliar_revisiones_huerfanas(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Cierra revisiones en `SCANNING` que se quedaron sin run, y avisa a la organización.

    ## Qué hueco cierra, y por qué el watchdog de runs no lo cubre

    Porque `reconcile_orphaned_runs` tiene tres ramas y **ninguna** ve una revisión en `SCANNING`
    sin `run_id`:

    1. La que marca un `RUNNING` obsoleto busca las revisiones **por `run_id`**. Sin run no hay
       fila que casa.
    2. La que cierra revisiones con run terminal hace `INNER JOIN` con `pentest_runs`. Una
       revisión sin run desaparece del resultado del `JOIN`: es la misma razón por la que un
       `INNER JOIN` no ve lo que no tiene pareja, y por eso el fallo era invisible en lugar de
       dar un cero.
    3. La que reencola revisas obsoletas mira `QUEUED`, no `SCANNING`.

    El estado es alcanzable porque `PullRequestReview.run_id` es `ON DELETE SET NULL`: borrar el
    `PentestRun` deja la revisión en `SCANNING` con `run_id` nulo, sin ningún proceso vivo que la
    vaya a tocar y sin ninguna tarea que la recoja. Es lo que pasó con la revisión `f9af89a1`,
    que llevaba desde el 27 de septiembre en `SCANNING` sin run y sin botón de relanzar —porque
    `SCANNING` no es relanzable—: nadie la iba a recoger nunca.

    ## Por qué a `ERROR` y no a reencolar

    Por dos razones, y las dos son de dinero.

    **Reencolar cobraría otra vez.** El cobro ocurre en `_claim_review`, en la misma
    transacción que crea el run. Volver a encolar esta revisión volvería a pasar por ahí y
    cobraría un escaneo `QUICK` por un análisis que ya se cobró una vez. La reserva solo se
    devuelve en `_devolver_lo_retenido` (`backend/workers/tasks.py`), y ese camino está en el
    worker que ejecuta el escaneo: si el run ya no existe, no hay nadie que lo devuelva.

    **No se puede reintentar a ciegas con el mismo resultado.** La revisión estaba en `SCANNING`
    porque algo la tomó. Reencolar sin saber qué pasó convierte un fallo consumido en otro
    fallo consumido, y si el motivo es de despliegue —que es el caso frecuente— consume la cola
    y deja dos filas de `ERROR` con el mismo código y ninguna explicación. La respuesta honesta
    al usuario es que su análisis se quedó parado y que puede volver a pedirlo, no un reintento
    invisible que le vuelve a cobrar.

    `ERROR` es además el único estado que resuelve el problema de **recogida**: es relanzable,
    así que el botón del panel aparece y el usuario decide.

    ## Por qué no se reembolsa

    Porque no se puede saber si se cobró. La revisión en `SCANNING` con `run_id` nulo significa
    que hubo un run —`_claim_review` pone `run_id` y `SCANNING` en el mismo commit— y que
    desapareció después. No sabemos si llegó a consumir, y R4 hace que el ledger sea de solo
    `INSERT`: devolver créditos sobre una suposición es inventar saldo. Quien tenga que devolver
    algo es el operador, con el registro del worker delante, no este vigilante.

    ## Por qué el umbral es `pr_review_stale_after_seconds`

    Porque es la misma pregunta que ya se le hace al watchdog de runs —«¿cuánto tiempo lleva
    esto esperando a que nadie lo recoja?»— y la misma respuesta. No hay un segundo parámetro
    para el mismo concepto: dos umbrales para lo mismo divergen sin que nada se entere, que es
    lo que R1 prohíbe. Y aquí el margen puede ser corto porque no hay trabajo en curso que pueda
    llegar tarde: o el run sigue vivo y esta función ni lo mira, o ya no está.

    ## Por qué no toca el run

    Porque si el run existe y está `QUEUED` o `RUNNING`, esa revisión no es huérfana: la está
    ejecutando el worker y el watchdog de runs es quien la cierra cuando el run caduque. Aquí
    solo se mira `run_id IS NULL`, que es el único estado que nadie más mira. Dos vigilantes con
    el mismo alcance se pisan y emiten dos avisos por el mismo fallo.
    """

    current_time = now or datetime.now(UTC)
    limite = current_time - timedelta(seconds=settings.pr_review_stale_after_seconds)
    result = await session.execute(
        select(PullRequestReview)
        .where(
            PullRequestReview.status == PRReviewStatusEnum.SCANNING,
            PullRequestReview.run_id.is_(None),
            PullRequestReview.created_at < limite,
        )
        .order_by(PullRequestReview.created_at, PullRequestReview.id)
        .with_for_update()
    )
    huerfanas = list(result.scalars().all())
    if not huerfanas:
        return 0

    # Los identificadores y la organización se copian a una lista **antes** del commit. Después
    # de confirmar, con una sesión que expira al confirmar, leerlos de los objetos pediría una
    # recarga que en SQLAlchemy asíncrono necesita contexto verde, y reventaría con
    # `MissingGreenlet` sobre un vigilante que ya ha hecho su trabajo. Es el mismo motivo, y el
    # mismo patrón, que `_marcar_revision_sin_saldo` y `_mark_pipeline_error`.
    #
    # Y no es teórico: ese `MissingGreenlet` ya tumbó una prueba de este repositorio.
    a_avisar = [
        (
            review.organization_id,
            pr_review_payload(
                review_id=review.id,
                repository_id=review.repository_id,
                pr_number=review.pr_number,
                status=PRReviewStatusEnum.ERROR.value,
                findings_count=0,
                blocking=True,
                # El mismo código que usa el watchdog de runs para el mismo hecho visto por el
                # otro lado: «el análisis se quedó en curso y lo cerró el vigilante». Desde el
                # panel no hay dos hechos distintos, hay uno, y el detalle de **qué** rama lo
                # cerró va al log del worker —que es donde se buscan los registros de esto— y
                # no a un código que el usuario tendría que traducir para nada.
                error_code="WORKER_WATCHDOG_STALE",
            ),
        )
        for review in huerfanas
    ]

    for review in huerfanas:
        review.status = PRReviewStatusEnum.ERROR
        # `finished_at` no es decorativo: `_review_metrics` cuenta una revisión como revisada
        # solo si está en estado terminal **y** tiene `finished_at`. Sin él, la revisión queda
        # cerrada para el panel y sin contar para el KPI, que son dos verdades distintas sobre
        # la misma fila.
        review.finished_at = current_time
    await session.commit()

    # Todo el aviso va después del commit y solo por lo que esta pasada transitó de verdad: un
    # evento que anuncia un estado que todavía no es visible en la API es peor que uno que
    # llega un segundo tarde.
    for organization_id, payload in a_avisar:
        await publish_event(session, EventType.PR_REVIEW_FAILED, organization_id, payload)
    logger.warning(
        "Se cerraron %d revision(es) de PR en SCANNING sin run; ninguna se reencola porque "
        "volver a encolarla volveria a cobrar el escaneo",
        len(huerfanas),
    )
    return len(huerfanas)


async def lanzar_analisis_de_review(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    review_id: uuid.UUID,
    dispatch: DispatchDependency,
) -> PullRequestReview:
    """Pone una revisión en `QUEUED` y la encola para el pipeline.

    ## Por qué el filtro por organización va **aquí** y no solo en el router

    Porque R3 no es un filtro de ruta: es la frontera de confianza. El mismo motivo que
    `_cargar_repositorio` en `sync.py`. Un `review_id` es adivinable, y sin este filtro un
    llamador podría encolar el análisis del PR de otro cliente —y el pipeline cobraría a ese
    cliente por ello, con su credencial y su repositorio—. Con el filtro, la respuesta es que
    no existe.

    ## El filtro de la revisión y el del repositorio se tapan el uno al otro

    Está medido, y conviene que quien lo toque lo sepa antes. Quitando **solo** el filtro de
    organización de la revisión, la prueba de aislamiento **sigue en verde**: la comprobación del
    repositorio, dos líneas más abajo, también filtra por organización y aborta con el mismo
    `404`. Hay que quitar los dos para que la prueba caiga, y cuando caen sale `202` con una
    revisión de otro tenant encolada.

    Los dos filtros son correctos y los dos se quedan —la redundancia es la defensa en
    profundidad de R3—, pero la consecuencia práctica es que **esta función no tiene una prueba
    de comportamiento capaz de distinguir un filtro del otro**. Si alguien quita los dos sin
    querer, la prueba lo detecta; si quita uno, no. Es el mismo criterio que aplica
    `knowledge/retrieval.py`: cuando sospeches de un segundo filtro que tapa al primero, la
    prueba tiene que mirar el SQL emitido, no el resultado.

    ## Por qué `with_for_update` y no un `UPDATE` condicional

    Porque la transición a `QUEUED` es una **decisión** que hay que leer antes de escribir: si
    entre la lectura y el `commit` otra petición —el webhook del proveedor, o dos clics— pone la
    revisión en cola, el segundo encolado sería un segundo contenedor. El bloqueo de fila es lo
    que convierte «comprobar y escribir» en una sola operación, y es la misma razón por la que
    `_claim_run_for_execution` bloquea la fila antes de pasarla a `RUNNING`.

    ## Por qué el fallo de encolado no se traga

    Porque es el fallo de `silent-failure-hunter`: si `dispatch` falla y la función devuelve con
    la revisión en `QUEUED`, queda en un estado que **nadie va a recoger**. Por eso el error se
    propaga y por eso se devuelve a `ERROR`: la revisión vuelve a ser un estado que el panel sabe
    explicar y que el usuario puede volver a pulsar.

    ## Lo que este servicio ya no resuelve solo, y quién lo resuelve

    Antes este docstring daba por hecho que el problema era solo de aquí: «el watchdog no lo
    marca —solo marca runs—». Eso era cierto del watchdog **de runs**, y era una forma
    silenciosa de dejar el agujero abierto: el fallo de encolado lo cubre esta función, y el
    fallo de recogida posterior ya no lo cubre nadie.

    Ahora hay dos caminos y los dos terminan en `ERROR` con aviso, y ninguno reencola:

    - **`dispatch` falla aquí** → esta función lo captura, marca `ERROR` y propaga.
    - **La revisión se queda esperando a un worker que no existe** → la reconciliación de este
      mismo fichero la detecta por tiempo y la cierra, que es un estado relanzable.

    La diferencia entre los dos es el momento: uno es un fallo que se ve en el clic, el otro es
    un fallo que solo se ve por tiempo. Por eso el segundo necesita su propio vigilante, y por
    eso no basta con decir «el watchdog no lo marca».
    """

    result = await session.execute(
        select(PullRequestReview)
        .where(
            PullRequestReview.id == review_id,
            PullRequestReview.organization_id == organization_id,
        )
        .with_for_update()
    )
    review = result.scalar_one_or_none()
    if review is None:
        raise ReviewNotLaunchableError(str(review_id))

    # El repositorio se comprueba **también** aquí, y no se deduce del estado de la revisión.
    #
    # Porque una revisión puede quedar en un estado relanzable con el repositorio ya
    # desconectado —se desconecta después— y encolarla produce un fallo tres segundos después,
    # en el worker, con un motivo que es del despliegue y no del PR. Comprobarlo aquí convierte
    # un fallo diferido y opaco en un `409` inmediato y con nombre.
    repository_result = await session.execute(
        select(Repository).where(
            Repository.id == review.repository_id,
            Repository.organization_id == organization_id,
        )
    )
    repository = repository_result.scalar_one_or_none()
    if repository is None or not repository.is_active:
        raise ReviewNotLaunchableError("El repositorio de la revisión no está conectado")
    if not repository.pr_reviews_enabled:
        raise ReviewNotLaunchableError("Las revisiones automáticas están deshabilitadas")

    if review.status in {PRReviewStatusEnum.QUEUED, PRReviewStatusEnum.SCANNING}:
        raise ReviewNotLaunchableError("La revisión ya está en curso")

    review.status = PRReviewStatusEnum.QUEUED
    review.run_id = None
    review.finished_at = None
    review.comment_id = None
    await session.commit()

    try:
        task_id = dispatch(str(review_id))
    except Exception as error:
        logger.exception("No se pudo encolar la revisión %s", review_id)
        await session.rollback()
        # La revisión vuelve a su estado anterior y no a `ERROR`: `ERROR` con un `run_id` nulo
        # significa "el pipeline la empezó y se rompió", que no es lo que pasó. Lo que pasó es
        # que el botón no pudo encolarla, y eso se dice devolviendo el error al panel, que
        # muestra el aviso. Dejar la fila en `ERROR` sería mentirle sobre la causa.
        recovery = await session.execute(
            select(PullRequestReview)
            .where(
                PullRequestReview.id == review_id,
                PullRequestReview.organization_id == organization_id,
            )
            .with_for_update()
        )
        failed = recovery.scalar_one_or_none()
        if failed is not None:
            failed.status = PRReviewStatusEnum.ERROR
            await session.commit()
            await publish_event(
                session,
                EventType.PR_REVIEW_FAILED,
                failed.organization_id,
                pr_review_payload(
                    review_id=failed.id,
                    repository_id=failed.repository_id,
                    pr_number=failed.pr_number,
                    status=PRReviewStatusEnum.ERROR.value,
                    findings_count=0,
                    blocking=True,
                    error_code="CELERY_DISPATCH_FAILED",
                ),
            )
        raise ReviewDispatchError(review_id) from error

    logger.info("Revisión %s encolada para análisis como tarea %s", review_id, task_id)
    await session.refresh(review)
    return review


__all__ = (
    "ESTADOS_RELANZABLES",
    "ESTADOS_TERMINALES",
    "ReviewDispatchError",
    "ReviewNotLaunchableError",
    "lanzar_analisis_de_review",
    "reconciliar_revisiones_huerfanas",
)
