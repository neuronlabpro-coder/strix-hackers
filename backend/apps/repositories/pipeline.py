"""Orquestación de una revisión de Pull Request aislada y efímera."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.pricing import scan_credit_cost
from backend.apps.billing.service import ZERO, InsufficientCreditsError, apply_credit_delta
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.clients.base import (
    BaseGitClient,
    GitClientError,
    GitRateLimitError,
    GitServerError,
)
from backend.apps.repositories.feedback import build_pr_comment_markdown
from backend.apps.repositories.models import (
    GitCredential,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.services import build_client_for_repository
from backend.apps.repositories.token_refresh import GitCredencialError
from backend.apps.repositories.workspace import materialize_pr_workspace
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.apps.webhooks.emission import (
    EventType,
    pentest_payload,
    pr_review_payload,
    publish_event,
    vulnerability_created_payload,
)
from backend.core.config import settings
from backend.core.database import create_database_engine
from backend.workers.parser.strix_parser import extract_strix_scan_id, parse_strix_output
from backend.workers.runner.diagnostico import diagnosticar_fallo, es_fallo_de_despliegue
from backend.workers.runner.sandbox import SandboxRunResult, StrixSandboxManager

logger = logging.getLogger(__name__)


class PRPipelineError(RuntimeError):
    """Error de dominio durante el pipeline de revisión de PR."""


class SessionProvider(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


ClientBuilder = Callable[[AsyncSession, Repository], Awaitable[BaseGitClient]]
ManagerFactory = Callable[..., StrixSandboxManager]
Materializer = Callable[..., Awaitable[list[str]]]

#: La firma del encolado del **reintento del comentario**. Es la misma forma que
#: `DispatchDependency` en `reviews.py` y por el mismo motivo: el módulo de dominio no
#: depende del de Celery, y una prueba puede encolar contra un doble sin levantar un broker.
CommentDispatch = Callable[[str], str]

#: Prefijo de la referencia de asiento que identifica a qué revisión de PR pertenece un cobro.
#:
#: ## Por qué el reference_id lleva el `review_id` y no solo el `run_id`
#:
#: Porque el **reintento** de una revisión crea un `PentestRun` nuevo y se lleva por delante un
#: `apply_credit_delta` nuevo, y lo que hay que demostrar es que ese segundo cobro no ocurre.
#: Con la referencia desnuda —el identificador del run— la pregunta «¿esta revisión ya se
#: cobró?» no tiene respuesta: el run del intento anterior desapareció de la fila en el momento
#: en que `run_id = None` lo suelta, y el ledger no guarda el historial de esa unión.
#:
#: ## Por qué el `run_id` va **delante** y no detrás
#:
#: Por compatibilidad con el resto del sistema, que ya busca por `run_id` con
#: `reference_id LIKE '{run_id}%'`: la reserva (`{run_id}`), el ajuste contra el consumo real
#: (`{run_id}:usage`), el reembolso (`{run_id}:refund`) y la devolución por aborto
#: (`{run_id}:abort_refund:*`) **todos** empiezan por el identificador del run. Ponerlo delante
#: mantiene esa búsqueda funcionando —`tasks.py:_saldo_retenido` la usa para decidir cuánto
#: devolver— y deja la parte de la revisión al final, donde un `LIKE '%:pr:{review_id}'` la
#: encuentra sin ambigüedad porque no lleva comodín al final.
#:
#: ## Por qué el patrón del `LIKE` es literal salvo el `LIKE` inicial
#:
#: Porque un UUID es hexadecimal con guiones: no puede contener ni `%` ni `_`. Es la misma
#: razón por la que `_saldo_retenido` puede construir su patrón sin `ESCAPE`, y deja de ser
#: cierta en el mismo sitio: el día que esta función recibiera un identificador que no fuera un
#: UUID, el comodín dejaría de ser un wildcard y empezaría a ser un personaje.
PREFIJO_DE_COBRO_DE_REVISION = ":pr:"


class PublicacionDeComentario(NamedTuple):
    """Qué pasó con el comentario del pull request, en tres respuestas y no en una excepción.

    ## Por qué no un `bool`

    Porque hacen falta **dos** preguntas distintas y se responden con datos distintos: ¿se
    intentó?, ¿funcionó?. La segunda sin la primera es ruido —«no se publicó nada» no es un
    fallo cuando no había nada que publicar—, y la primera sin la segunda es el defecto que
    este módulo evita: un comentario que se intentó y falló tiene que quedar marcado como
    pendiente para poder reintentarlo **sin** repetir el escaneo, y eso lo decide quien llama
    con `intentado and not publicado`.

    ## Por qué se lleva la excepción

    Porque hay dos llamadores con necesidades opuestas. El pipeline la **registra y se la
    queda**: un comentario que falla no puede tocar un veredicto ya confirmado. La tarea de
    reintento, en cambio, la necesita **sin envolver**: `autoretry_for` decide con el tipo de la
    excepción, y un `GitRateLimitError` metido dentro de un `PRPipelineError` se trataría como
    permanente y no se reintentaría nunca. Devolverla es lo que deja que cada llamador haga lo
    correcto sin que esta función tenga que saber de qué lado está.
    """

    #: Que se hizo una llamada al proveedor: actualizar el comentario anterior, o publicarlo.
    intentado: bool
    #: Que el proveedor aceptó y hay `comment_id` con el que volver a actualizarlo.
    publicado: bool
    #: La excepción original, **sin envolver**, cuando `intentado` y no `publicado`.
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class PipelineClaim:
    """Lo que el pipeline necesita para trabajar, y lo que necesita para **anunciar**.

    ## Por qué los identificadores están duplicados

    Los objetos de ORM (`review`, `repository`, `run`) son ya convenientes mientras la
    sesión está viva, pero **no sobreviven a un `commit` o un `rollback` con
    `expire_on_commit=True`**: sus atributos pasan a pedir una recarga, y en SQLAlchemy
    asíncrono esa recarga necesita un contexto verde. Leerlos fuera de una operación de base
    de datos —que es exactamente lo que se hace al construir un payload de evento— falla con
    `MissingGreenlet`.

    Los UUID van aparte por eso. Son valores planos, ya resueltos, y funcionan en cualquier
    punto del flujo: después de un commit, después de un rollback, dentro de un `except`.

    La alternativa —leer `claim.repository.id` y confiar en que nadie expire la sesión— es
    lo que produjo el fallo: la sesión de los tests usa el valor por defecto de SQLAlchemy y
    el pipeline de producción usa `expire_on_commit=False`, así que el mismo código pasaba
    en un sitio y reventaba en otro.
    """

    review: PullRequestReview
    repository: Repository
    #: La credencial **con la que se materializó el workspace**, y por eso es opcional: el
    #: reintento del comentario no materializa nada, abre su cliente por su cuenta con
    #: `build_client_for_repository`, y meterle aquí la credencial del claim obligaría a leerla
    #: solo para no usarla. El pipeline de escaneo, que sí la usa, la sigue teniendo.
    credential: GitCredential | None
    run: PentestRun
    review_id: UUID
    organization_id: UUID
    run_id: UUID
    #: Plano, a propósito. Ver la nota de la clase.
    repository_id: UUID
    repository_full_name: str
    commit_sha: str
    pr_number: int


@asynccontextmanager
async def _default_session_provider() -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(settings)
    session_factory = async_sessionmaker[AsyncSession](
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _default_comment_dispatch(review_id: str) -> str:
    """Encola, de verdad, el reintento del comentario. Devuelve el identificador de la tarea.

    El `import` va **dentro** de la función, y no por gusto: `backend.apps.repositories.tasks`
    importa este módulo para registrar la tarea del pipeline, así que importarlo arriba aquí
    cerraría un ciclo de módulos. Es el mismo motivo por el que `pentests/service.py` mete a
    `backend.workers.tasks` dentro de `dispatch_pentest_run` y por el que `emission.py` mete a
    `celery_app` dentro de `publish_event`.
    """

    from backend.apps.repositories.tasks import publicar_comentario_de_review

    return str(publicar_comentario_de_review.delay(review_id).id)  # pyright: ignore[reportFunctionMemberAccess]


def _parse_review_id(review_id: str) -> UUID:
    try:
        return UUID(review_id)
    except (TypeError, ValueError) as error:
        raise PRPipelineError("review_id no contiene un UUID válido") from error


async def _marcar_revision_sin_saldo(
    session: AsyncSession,
    review: PullRequestReview,
    review_id: UUID,
    repository_id: UUID,
    pr_number: int,
    organization_id: UUID,
) -> None:
    """Deja la revisión en `ERROR` y publica el evento, con los identificadores ya copiados.

    ## Por qué los identificadores vienen como argumentos y no se leen aquí

    Porque se llama **después** de un `rollback`, y después de un rollback los objetos de ORM
    están expirados: leer `review.id` es una recarga que necesita contexto verde, y en una
    corrutina eso es un `MissingGreenlet`. No es un detalle teórico — una prueba lo falló antes de
    que este comentario existiera.

    Por eso se copian en el llamador, **antes** del rollback, que es el mismo patrón y la misma
    explicación que usa el bloque de «revisiones deshabilitadas» de este fichero y que ya está
    escrito ahí. Se copia en vez de inventar otro, y la firma lo hace explícito: si mañana alguien
    llama a esta función sin pasar los ids, no compila.

    ## Por qué el motivo va en el evento y no en la fila

    Porque `PullRequestReview` **no tiene columna de error**, y no se añade una aquí: el motivo
    de un fallo de revisión ya viaja en el evento `PR_REVIEW_FAILED` y en su `error_code`, que es
    donde el panel lo lee. Meterlo también en la fila sería la misma información en dos sitios, y
    la que no se actualiza es la que miente.

    ## Por qué un `error_code` estable y no el texto

    Porque el `error_code` es lo que el panel puede **filtrar** y agrupar. Un mensaje en español
    es un texto que hay que leer uno a uno; un código estable es el que permite ver «esta
    organización lleva veinte revisiones rechazadas por saldo» de un vistazo, que es la pregunta
    que se hace alguien con un panel lleno de errores.
    """

    await session.refresh(review)
    payload = pr_review_payload(
        review_id=review_id,
        repository_id=repository_id,
        pr_number=pr_number,
        status=PRReviewStatusEnum.ERROR.value,
        findings_count=0,
        blocking=True,
        error_code="INSUFFICIENT_CREDITS",
    )
    review.status = PRReviewStatusEnum.ERROR
    review.finished_at = datetime.now(UTC)
    await session.commit()
    await publish_event(session, EventType.PR_REVIEW_FAILED, organization_id, payload)


def _target_identifier(repository: Repository, pr_number: int) -> str:
    return f"{repository.full_name}#PR-{pr_number}"[:512]


def _referencia_de_cobro(run_id: UUID, review_id: UUID) -> str:
    """La referencia del asiento de consumo de un intento de revisión de pull request.

    Lleva las dos mitades porque las dos hacen falta. El `run_id` va delante para que las
    búsquedas por run que ya existen sigan funcionando —ver `PREFIJO_DE_COBRO_DE_REVISION`— y el
    `review_id` va detrás porque responde a la pregunta que decide si un reintento vuelve a
    pagar: «¿esta revisión ya le costó algo al tenant?».
    """

    return f"{run_id}{PREFIJO_DE_COBRO_DE_REVISION}{review_id}"


async def _creditos_cobrados_para_la_revision(
    session: AsyncSession,
    organization_id: UUID,
    review_id: UUID,
) -> Decimal:
    """Lo que esta revisión de pull request le ha costado ya al tenant. **Con su signo de gasto.**

    Es decir, un número **negativo** cuando se ha cobrado algo y cero cuando no. Quien llama solo
    necesita comparar con cero, y no tiene que saber en qué dirección es la deuda.

    ## Por qué se pregunta al ledger y no a un campo de la revisión

    Porque `credit_ledger` es el único sitio donde el importe cobrado está sellado, y porque es
    **append-only** (R4): no se puede reescribir un cobro, así que tampoco se puede «desmarcar»
    uno. Consultarlo es la única forma honesta de saber si ya se pagó.

    ## Por qué la suma y no un `EXISTS`

    Porque la pregunta de verdad no es «¿existe un asiento?» sino «¿debe algo?». Una devolución
    compensatoria —un reembolso— deja el asiento puesto y deja esta revisión en cero, y con un
    `EXISTS` eso se leería como «ya se cobró» cuando lo cierto es que ya no debe nada. Sumar da
    el número, y con el número se decide.

    ## Por qué el filtro de organización va en el `WHERE`

    Es R3 resuelto en el sitio correcto: traer el asiento de otro tenant a memoria para
    descartarlo con un `if` es el patrón que `pentests/service.py` documenta como incorrecto. Y
    aquí no es teórico: sin ese filtro el `SUM` sería la suma de los asientos de **todos** los
    clientes, y la decisión de si este reintento vuelve a pagar saldría de un número que
    incluye el trabajo de otros tenants.

    ## Por qué el `LIKE` no lleva comodín al final

    Porque el `review_id` es un UUID y no puede contener ni `%` ni `_`, así que la cola de la
    referencia tiene que coincidir **exacta**. La misma razón por la que `_saldo_retenido` puede
    construir su patrón sin `ESCAPE`, y deja de ser cierta en el mismo sitio.
    """

    total = await session.execute(
        select(func.coalesce(func.sum(CreditLedger.amount_delta), 0)).where(
            CreditLedger.organization_id == organization_id,
            CreditLedger.reason == LedgerReasonEnum.SCAN_CONSUMPTION,
            # Sin comodín al final a propósito: el `review_id` es un UUID y no puede contener ni
            # `%` ni `_`, así que la cola de la referencia tiene que coincidir **exacta**. El
            # motivo entero está en el docstring de arriba, y el patrón viaja como parámetro
            # ligado de SQLAlchemy, nunca concatenado en la sentencia.
            CreditLedger.reference_id.like(f"%{PREFIJO_DE_COBRO_DE_REVISION}{review_id}"),
        )
    )
    return Decimal(str(total.scalar_one()))


async def _claim_review(
    session: AsyncSession,
    review_id: UUID,
    *,
    retry_failed: bool = False,
    celery_task_id: str | None = None,
) -> PipelineClaim | None:
    review_result = await session.execute(
        select(PullRequestReview)
        .where(PullRequestReview.id == review_id)
        .with_for_update()
    )
    review = review_result.scalar_one_or_none()
    if review is None:
        raise PRPipelineError("La revisión de PR no existe")
    #: Un reintento de Celery es **el mismo encargo**, no uno nuevo: la tarea es la misma, el
    #: argumento es el mismo y lo que cambió entre medias es el humor del proveedor. La marca
    #: se toma **antes** de tocar nada, porque después de estas líneas el estado ya es `QUEUED`.
    #:
    #: ## Por qué también acepta `QUEUED` y no solo `ERROR`
    #:
    #: Porque `ERROR` es el estado que deja `_mark_pipeline_error`, y ese camino es el normal pero
    #: no el único: si el marcado no llegara a ocurrir, o si un reintento llegara mientras otra
    #: llamada a la cola había puesto la revisión en `QUEUED`, un `retry_failed=True` se
    #: encontraría con una revisión reclamable que **ya estaba pagada**. Sin esta línea, ese
    #: reintento cobraría un segundo escaneo por el mismo análisis, que es exactamente el defecto.
    #:
    #: El coste de aceptarlo es cero, y se ve mirando la condición del cobro: el reintento solo se
    #: ahorra el pago cuando **el ledger dice que debe algo**, así que un `QUEUED` que nunca se
    #: cobró —el caso de un reintento imposible— se cobra igual. La marca es más ancha y la
    #: pregunta que decide sigue siendo la misma.
    es_reintento = retry_failed and review.status in {
        PRReviewStatusEnum.ERROR,
        PRReviewStatusEnum.QUEUED,
    }
    if review.status == PRReviewStatusEnum.ERROR and retry_failed:
        review.status = PRReviewStatusEnum.QUEUED
        review.run_id = None
        review.finished_at = None
    if review.status != PRReviewStatusEnum.QUEUED:
        return None
    repository_result = await session.execute(
        select(Repository)
        .where(
            Repository.id == review.repository_id,
            Repository.organization_id == review.organization_id,
        )
        .with_for_update()
    )
    repository = repository_result.scalar_one_or_none()
    if repository is None or not repository.is_active:
        raise PRPipelineError("El repositorio de la revisión no está activo")
    if not repository.pr_reviews_enabled:
        review.status = PRReviewStatusEnum.ERROR
        review.finished_at = datetime.now(UTC)
        # El payload se construye **antes** del commit. Con una sesión que expira al
        # confirmar, leer `review.organization_id` después es una recarga que necesita
        # contexto verde: `MissingGreenlet` sobre una revisión que ya está guardada como
        # fallida, que es una operación correcta reportada como error.
        payload = pr_review_payload(
            review_id=review.id,
            repository_id=repository.id,
            pr_number=review.pr_number,
            status=PRReviewStatusEnum.ERROR.value,
            findings_count=0,
            blocking=True,
            error_code="PR_REVIEWS_DISABLED",
        )
        tenant_id = review.organization_id
        await session.commit()
        # Aquí todavía no existe `claim`: esta función lo construye. Se usan las filas que
        # acaba de leer, que son la misma revisión y el mismo repositorio.
        await publish_event(session, EventType.PR_REVIEW_FAILED, tenant_id, payload)
        raise PRPipelineError("Las revisiones automáticas están deshabilitadas")

    run: PentestRun | None = None
    if review.run_id is not None:
        existing_run = await session.get(PentestRun, review.run_id)
        if existing_run is not None and existing_run.status in {
            ScanStatusEnum.QUEUED,
            ScanStatusEnum.RUNNING,
        }:
            run = existing_run
    if run is None:
        run = PentestRun(
            organization_id=review.organization_id,
            target_type=TargetTypeEnum.REPOSITORY,
            target_identifier=_target_identifier(repository, review.pr_number),
            scan_mode=ScanModeEnum.QUICK,
            status=ScanStatusEnum.RUNNING,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        # Y aquí se cobra, que es lo que no pasaba.
        #
        # ## Por qué este camino también paga
        #
        # Porque es un **segundo punto de entrada a un escaneo**, y `pentests/service.py`
        # declara que R4 «no tolera dos caminos para lo mismo». Era exactamente lo que la
        # auditoría encontró: este bloque construía el `PentestRun` directamente, sin pasar por
        # `queue_pentest`, y por tanto sin `apply_credit_delta`, sin comprobación de saldo y sin
        # asiento en el ledger. El resultado es que una revisión de PR disparada por un **webhook
        # de Git** ejecutaba un escaneo real que consumía tokens de la plataforma y no se
        # contabilizaba en ninguna parte.
        #
        # ## Por qué se cobra a precio de `QUICK` y no uno propio
        #
        # Porque el `run` de arriba **ya declara** `scan_mode=QUICK`. Cobrar otra cosa sería
        # inventar un precio que R1 prohíbe escribir en el código, y cobrar más de lo que cuesta
        # la operación que de verdad se ejecuta sería cobrar de más. `scan_credit_cost(QUICK)` es
        # el precio configurado de un escaneo `QUICK`, y esto es uno.
        #
        # Lo que **no** se implementa aquí es el cupo de revisiones por asiento que mencionan
        # `ARCHITECTURE.md` y la fase 5: no existe en ninguna parte del backend, y decidir su
        # valor y su quién lo consume es una decisión de producto, no un arreglo de seguridad.
        #
        # ## Por qué un reintento **no** vuelve a cobrar, y por qué se pregunta al ledger
        #
        # Porque un reintento de Celery no es un encargo nuevo. Es la **misma** tarea, con el
        # **mismo** argumento, que vuelve a ejecutarse porque el proveedor devolvió un `429` o un
        # `5xx`. Lo que cambió entre el intento anterior y este es el humor de GitHub, no lo que
        # pidió el cliente. Y un `429` es, literalmente, «demasiadas peticiones»: es **más**
        # probable cuando la plataforma acaba de lanzar varios escaneos a la vez, que es
        # exactamente cuando el reintento se encadena. Cobrar cada intento convertía el fallo de
        # infraestructura más frecuente en la fuente de ingresos menos discreta del producto.
        #
        # Lo que se hace, entonces, es **cobrar una vez por revisión** y que el reintento sea
        # gratis. Y no es «cobrar una vez y ya»: es *no volver a cobrar mientras esta revisión
        # deba algo*, que es una afirmación sobre el ledger, no sobre un flag.
        #
        # ## Por qué la pregunta es al ledger y no al `retry_failed` a secas
        #
        # Porque el flag dice «esto viene de un reintento» y el ledger dice «esto ya se pagó», y
        # lo segundo es lo que decide. Si mañana alguien añadiera un camino reintentable **antes**
        # del cobro —que es el caso que temería un flag solo—, el reintento no cobraría y el
        # escaneo saldría gratis. Con el ledger, ese caso se cobra porque no debe nada, y el
        # defecto no puede depender de un orden de llamadas que nadie escribió para eso.
        #
        # Y la otra mitad: un relanzamiento desde el panel es un encargo **nuevo** y sí cobra,
        # aunque la revisión ya estuviera pagada. Por eso la condición es `es_reintento and debe`,
        # y no `if debe`. Con `if debe` el arreglo sería «no cobrar nunca dos veces» y el precio
        # del botón de reanalizar pasaría a ser cero sin que nadie lo hubiera decidido.
        organization_id = review.organization_id
        debe = await _creditos_cobrados_para_la_revision(session, organization_id, review.id)
        if es_reintento and debe < ZERO:
            logger.info(
                "El reintento de la revisión %s no cobra: el intento anterior ya se cobró "
                "(saldo de esta revisión %s, intento previo run=%s). Un reintento del proveedor "
                "es el mismo encargo, no uno nuevo",
                review.id,
                debe,
                run.id,
            )
        else:
            if es_reintento:
                # No debería ocurrir: todo fallo reintentable de este pipeline ocurre **después**
                # del cobro. Se deja dicho en voz alta porque si algún día aparece, lo que
                # significa es que se está dando un análisis gratis y eso es un aviso, no un
                # detalle. La política sigue siendo la correcta para el caso real, así que no
                # se bloquea el escaneo por ello.
                logger.warning(
                    "El reintento de la revisión %s no encuentra ningún cobro anterior y se cobra "
                    "como uno nuevo; revisa si algún camino reintentable se ha movido antes del "
                    "cobro",
                    review.id,
                )
            try:
                await apply_credit_delta(
                    session=session,
                    organization_id=organization_id,
                    amount=-scan_credit_cost(ScanModeEnum.QUICK),
                    reason=LedgerReasonEnum.SCAN_CONSUMPTION,
                    reference_id=_referencia_de_cobro(run.id, review.id),
                    actor_user_id=None,
                )
            except InsufficientCreditsError:
                # No hay cliente HTTP al que responderle un `402`: esto lo dispara un
                # webhook. Lo que se puede es **dejar constancia**, y una revisión de PR
                # marcada como fallida con el motivo es constancia que el cliente ve en su
                # panel y que un operador puede auditar.
                #
                # Y es mejor que ejecutar: el trabajo no llega a lanzarse, así que no se
                # consume nada de la plataforma. Un escaneo de revisión sin pagar sería un
                # escaneo gratis automatizado por un webhook, que es el peor caso posible.
                #
                # Los identificadores se copian **antes** del rollback, por el motivo que está
                # escrito en `_marcar_revision_sin_saldo`. `organization_id` ya lo está desde
                # arriba, que es justo por eso se copió antes de hacer nada.
                review_id = review.id
                repository_id = review.repository_id
                pr_number = review.pr_number
                await session.rollback()
                await _marcar_revision_sin_saldo(
                    session,
                    review,
                    review_id,
                    repository_id,
                    pr_number,
                    organization_id,
                )
                raise PRPipelineError(
                    "La organización no tiene saldo para la revisión automática"
                ) from None
    run.status = ScanStatusEnum.RUNNING
    run.started_at = run.started_at or datetime.now(UTC)
    if celery_task_id is not None:
        run.celery_task_id = celery_task_id[:64]
    review.run_id = run.id
    review.status = PRReviewStatusEnum.SCANNING
    review.issues_caught_critical = 0
    review.issues_caught_high = 0
    review.merge_blocked = False
    review.finished_at = None
    await session.commit()

    credential_result = await session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == repository.organization_id,
            GitCredential.provider == repository.provider,
        )
    )
    credential = credential_result.scalar_one_or_none()
    if credential is None:
        raise PRPipelineError("No existe una credencial Git para la organización")
    return PipelineClaim(
        review,
        repository,
        credential,
        run,
        review.id,
        review.organization_id,
        run.id,
        repository.id,
        repository.full_name,
        review.commit_sha,
        review.pr_number,
    )


async def _mark_pipeline_error(
    session: AsyncSession,
    claim: PipelineClaim,
    error_code: str,
) -> None:
    await session.rollback()
    review_result = await session.execute(
        select(PullRequestReview)
        .where(PullRequestReview.id == claim.review_id)
        .with_for_update()
    )
    review = review_result.scalar_one_or_none()
    run_result = await session.execute(
        select(PentestRun)
        .where(
            PentestRun.id == claim.run_id,
            PentestRun.organization_id == claim.organization_id,
        )
        .with_for_update()
    )
    run = run_result.scalar_one_or_none()
    # Las banderas dicen si la transición **ocurrió ahora**, no si el objeto tiene el
    # campo puesto. La función se llama también con un run que ya estaba en estado
    # terminal, y preguntar por `finished_at is not None` daría un falso positivo en
    # cuanto una segunda ejecución pasara por aquí.
    review_transiciono = review is not None and review.status == PRReviewStatusEnum.SCANNING
    run_transiciono = run is not None and run.status in {
        ScanStatusEnum.QUEUED,
        ScanStatusEnum.RUNNING,
    }
    if review is not None and review_transiciono:
        review.status = PRReviewStatusEnum.ERROR
        review.finished_at = datetime.now(UTC)
    if run is not None and run_transiciono:
        run.status = ScanStatusEnum.FAILED
        run.finished_at = datetime.now(UTC)
        run.error_message = error_code
    # Los payloads se construyen **antes** del commit. Después, con una sesión que expire
    # al confirmar, leer `review.id` o `run.target_identifier` dispara una recarga que en
    # SQLAlchemy asíncrono necesita un contexto verde y revienta con `MissingGreenlet` —
    # sobre un pipeline que ya está guardado como fallido. Es el mismo modo de fallo que
    # `_persist_findings`: una operación de dominio correcta reportada como error.
    #
    # Solo se construyen los payloads de las transiciones que **ocurrieron**: repetir un
    # fallo que el receptor ya recibió con su código original lo haría parecer que hay dos
    # incidentes.
    review_payload: dict[str, object] | None = None
    if review is not None and review_transiciono:
        review_payload = pr_review_payload(
            review_id=review.id,
            repository_id=claim.repository_id,
            pr_number=claim.pr_number,
            status=PRReviewStatusEnum.ERROR.value,
            findings_count=0,
            blocking=True,
            error_code=error_code,
        )
    run_payload: dict[str, object] | None = None
    if run is not None and run_transiciono:
        run_payload = pentest_payload(
            run_id=run.id,
            status=ScanStatusEnum.FAILED.value,
            target_type=run.target_type,
            target_value=run.target_identifier,
            scan_mode=run.scan_mode,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_code=error_code,
        )

    await session.commit()
    # El tenant se toma de `claim`, que es un `dataclass` de valores planos leídos antes
    # del commit. Leerlo de `run` o de `review` después de confirmar pediría sus atributos
    # a la sesión, y con una que expira eso es una recarga que necesita contexto verde.
    if review_payload is not None:
        await publish_event(
            session, EventType.PR_REVIEW_FAILED, claim.organization_id, review_payload
        )
    if run_payload is not None:
        await publish_event(
            session, EventType.PENTEST_FAILED, claim.organization_id, run_payload
        )


async def _set_cleanup_pending(session: AsyncSession, claim: PipelineClaim) -> None:
    await session.rollback()
    result = await session.execute(
        select(PentestRun)
        .where(
            PentestRun.id == claim.run_id,
            PentestRun.organization_id == claim.organization_id,
        )
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is not None:
        run.cleanup_pending = True
        await session.commit()


def _review_url(review_id: UUID) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}/pr-reviews/{review_id}"


async def _publica_estado_del_commit(
    client: BaseGitClient,
    claim: PipelineClaim,
    state: str,
    description: str,
) -> bool:
    """Publica el badge de estado del commit. Devuelve si se publicó; **nunca lanza**.

    ## Por qué un badge no puede tumbar un escaneo

    Porque es lo más cosmético que hace el pipeline y lo primero que hace. El estado del
    commit se publica en `pipeline.py` **antes** de `manager_factory`, de `setup_workspace` y de
    `manager.run`: es literalmente el primer paso. Con la función anterior, un `404` del
    proveedor —un repositorio renombrado, un commit que no está en el repositorio
    configurado, una credencial sin permiso sobre ese repositorio— propagaba como
    `GitClientError`, salía por el `except Exception` del pipeline y **el escaneo no se
    intentaba nunca**. Medido: dos revisiones de pull request que Pagaron 3 créditos cada una
    y no llegaron a levantar el sandbox.

    La asimetría es el fondo del asunto, y no es un detalle de orden: el paso que puede fallar
    es el que menos importa, y el que importa puede no llegar a intentarse nunca. Con un
    repositorio real el `404` no aparece y el escaneo pasa, así que el defecto es
    **invisible hasta que ocurre**: cuando aparece, no falla el badge, falla el análisis.

    ## Por qué no se reintenta y por qué no se propaga

    Porque `run_pr_security_pipeline` reintenta ante `GitRateLimitError` y `GitServerError`, y
    un reintento **no** es repetir esta llamada: es repetir el pipeline entero, que vuelve a
    clonar el repositorio y a levantar un sandbox. Un `429` del proveedor en la llamada más
    cosmética del proceso no puede costar una segunda ejecución de la revisión.

    ## Por qué esto ya no es un problema de dinero, y por qué el motivo sigue siendo bueno

    Porque el cobro de un reintento está resuelto más abajo, en `_claim_review`: el reintento no
    vuelve a cobrar lo que ya se cobró. Ese arreglo llegó **después** de que este texto se
    escribiera, y la conclusión de entonces —«tres reintentos serían tres escaneos cobrados»—
    era correcta y ya no lo es. Lo que queda en pie es peor que el dinero: el sandbox se vuelve
    a lanzar, y el cliente espera el doble por un badge que no se publicó. Por eso el
    comentario que sigue habla del **coste de ejecución** y no del cobro, y por eso el arreglo
    no se ha tocado: sigue siendo el primer paso del pipeline y sigue siendo el menos
    importante.

    ## Por qué se registra igual, y con qué

    Porque «no se publicó» y «no se intentó» tienen que ser distinguibles. Un fallo silencioso
    aquí es el que hace que un día nadie sepa por qué un pull request no tiene badge. El
    registro lleva revisión, repositorio, estado y **el tipo** de la excepción, no su mensaje:
    el mensaje de un `GitClientError` está saneado, pero el de una excepción cualquiera puede no
    estarlo, y un registro de worker no es el sitio donde se sueltan secretos. Del `GitClientError`
    sí se copia el **código HTTP**, que es lo que dice si fue un 404 de repositorio, un 403 de
    permiso o un 429 de cuota.

    Y no tapa el motivo real: si el escaneo falla después por otra cosa, el `except Exception`
    del pipeline sigue registrando ese otro motivo, y el del badge queda como una línea aparte
    con su propio contexto.
    """

    try:
        await asyncio.to_thread(
            client.set_commit_status,
            claim.repository_full_name,
            claim.commit_sha,
            state,
            description[:130],
            _review_url(claim.review_id),
        )
    except Exception as error:
        codigo_http = error.status_code if isinstance(error, GitClientError) else None
        logger.warning(
            "No se pudo publicar el estado %s del commit %s del repositorio %s (revision=%s "
            "motivo=%s%s); el escaneo continua",
            state,
            claim.commit_sha,
            claim.repository_full_name,
            claim.review_id,
            type(error).__name__,
            f" http={codigo_http}" if codigo_http is not None else "",
        )
        return False
    return True


async def _publica_comentario_de_revision(
    client: BaseGitClient,
    claim: PipelineClaim,
    comment: str,
) -> PublicacionDeComentario:
    """Publica o actualiza el comentario del pull request. **Nunca lanza.**

    ## Por qué un comentario que falla no puede tumbar el escaneo

    Porque en este punto el escaneo **ya está hecho y confirmado**: `_persist_findings` escribió
    los hallazgos, los selló con el `run` en `COMPLETED` y emitió su evento. Antes, un `403` del
    proveedor en esta línea —una credencial sin permiso de escritura, que es un `GitClientError`
    de lo más normal del mundo— salía por el `except Exception` del pipeline, `_mark_pipeline_error`
    ponía la revisión en `ERROR` y el cliente se encontraba con «Error» **encima de un análisis que
    se había hecho**, con sus hallazgos en la base y sus créditos cobrados. Peor: el reintento de
    Celery volvía a reclamar la revisión, y eso significaba otro escaneo entero.

    Es la misma clase exacta que el badge, y por eso vive al lado de `_publica_estado_del_commit`
    con la misma forma y el mismo contrato: se intenta, se registra si falla, y la revisión no se
    entera.

    ## Por qué aquí sí hay que reintentarlo, y en el badge no

    Porque no son lo mismo, y la diferencia es exactamente lo que el encargo pedía no perder:

    - **El badge** es cosmético. Perderlo cuesta una línea de texto que nadie necesita para
      decidir, y reintentar el pipeline entero por él sale más caro que el badge.
    - **El comentario** es el informe del análisis, con los pasos de reproducción. Perderlo es
      perder información que el cliente pidió y que **no tiene forma de recuperar por otro
      camino**: los datos están en el panel, pero el comentario del pull request es lo que ven
      los demás del equipo.

    Así que el comentario que falla **se encola otra vez, solo**. Y solo: la tarea de reintento no
    lanza el sandbox, no vuelve a cobrar y no toca el veredicto.

    ## Qué queda registrado cuando falla, y por qué el estado de la fila ya lo dice

    El registro del worker lleva revisión, repositorio, número de pull request, si iba a
    actualizar un comentario existente y **el tipo** de la excepción con su código HTTP. No lleva
    el mensaje: el de un `GitClientError` está saneado pero el de una excepción cualquiera puede
    no estarlo, y un registro de worker no es donde se sueltan secretos.

    Y en la base no hace falta una columna nueva: una revisión que bloquea el merge y tiene
    `comment_id` nulo **es** el estado «el escaneo se entregó y su comentario no». Es un estado
    que se puede leer, no un dato que seDuplique. Lo que **no** hay —y es lo único que falta— es
    una marca de «pendiente desde cuándo», que es lo que haría falta para reintentar en bucle con
    retroceso sin pisar el mismo comentario cada `watchdog`; eso es una columna y una migración, y
    no se toman aquí. Mientras tanto el reintento lo acota el propio `autoretry_for` de la tarea.
    """

    if claim.review.comment_id:
        action = "actualizar"
    elif claim.review.merge_blocked:
        action = "publicar"
    else:
        # Una revisión que no bloquea no lleva comentario: es lo que el pipeline decide y lo que
        # el proveedor recibiría con un texto de «todo limpio» que nadie pidió. No es un fallo, y
        # por eso no se registra ni se reintenta.
        return PublicacionDeComentario(intentado=False, publicado=False)
    try:
        if claim.review.comment_id:
            await asyncio.to_thread(
                client.update_pr_comment,
                claim.repository_full_name,
                claim.review.comment_id,
                comment,
            )
        else:
            comment_id = await asyncio.to_thread(
                client.post_pr_comment,
                claim.repository_full_name,
                claim.pr_number,
                comment,
            )
            claim.review.comment_id = comment_id
    except Exception as error:
        codigo_http = error.status_code if isinstance(error, GitClientError) else None
        logger.warning(
            "No se pudo %s el comentario del PR %s del repositorio %s (revision=%s "
            "comment_id=%s motivo=%s%s); el escaneo ya esta confirmado y no se toca. El "
            "comentario se reintentara solo",
            action,
            claim.pr_number,
            claim.repository_full_name,
            claim.review_id,
            claim.review.comment_id,
            type(error).__name__,
            f" http={codigo_http}" if codigo_http is not None else "",
        )
        return PublicacionDeComentario(intentado=True, publicado=False, error=error)
    return PublicacionDeComentario(intentado=True, publicado=True)


async def _encola_reintento_del_comentario(
    review_id: UUID,
    dispatch: CommentDispatch,
) -> str | None:
    """Encola el reintento del comentario. Devuelve el identificador de tarea, o `None`.

    ## Por qué el fallo del encolado se registra y no se propaga

    Porque el veredicto ya está confirmado y ya se ha emitido su evento cuando se llega aquí. Si
    el encolado lanzara, el `except Exception` del pipeline pondría la revisión en `ERROR` —con el
    run en `COMPLETED` y los hallazgos escritos— y volveríamos exactamente al defecto que este
    módulo arregla: perder un análisis por no poder dejar una nota. El coste de que el reintento no
    llegue a encolarse es, en cambio, un comentario que se puede volver a pedir a mano.

    ## Por qué es un parámetro y no un `send_task` escrito aquí

    Porque el módulo de dominio no importa Celery —la misma razón por la que `DispatchDependency`
    es un `Callable` en `reviews.py`—, y porque una prueba necesita poder ver **qué** se encoló sin
    levantar un broker.
    """

    try:
        task_id = dispatch(str(review_id))
    except Exception:
        logger.exception(
            "No se pudo encolar el reintento del comentario de la revisión %s; el análisis "
            "sigue entregado y el comentario se puede volver a publicar desde el panel",
            review_id,
        )
        return None
    logger.info(
        "Comentario de la revisión %s pendiente: se reintentará como tarea %s, sin repetir el "
        "escaneo",
        review_id,
        task_id,
    )
    return task_id


async def _persist_findings(
    session: AsyncSession,
    claim: PipelineClaim,
    result: SandboxRunResult,
) -> list[Vulnerability]:
    if result.exit_code != 0:
        await _mark_pipeline_error(session, claim, "STRIX_NONZERO_EXIT")
        raise PRPipelineError("Strix terminó con un código de salida no cero")
    expected_scan_id = extract_strix_scan_id(result.output_json)
    run_result = await session.execute(
        select(PentestRun)
        .where(
            PentestRun.id == claim.run_id,
            PentestRun.organization_id == claim.organization_id,
        )
        .with_for_update()
    )
    run = run_result.scalar_one_or_none()
    if run is None:
        raise PRPipelineError("El run de la revisión no existe")
    if run.status == ScanStatusEnum.COMPLETED:
        existing_result = await session.execute(
            select(Vulnerability).where(
                Vulnerability.run_id == claim.run_id,
                Vulnerability.organization_id == claim.organization_id,
            )
        )
        return list(existing_result.scalars().all())
    if run.status not in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:
        raise PRPipelineError("El run ya no admite ingesta de resultados")
    if run.source_scan_id is not None and run.source_scan_id != expected_scan_id:
        raise PRPipelineError("El scan_id del reporte no coincide con el run")
    findings = parse_strix_output(
        result.output_json,
        organization_id=claim.organization_id,
        run_id=claim.run_id,
        db=None,
        expected_scan_id=expected_scan_id,
    )
    session.add_all(findings)
    await session.flush()
    run.source_scan_id = expected_scan_id
    run.status = ScanStatusEnum.COMPLETED
    run.finished_at = datetime.now(UTC)
    run.exit_code = str(result.exit_code)
    # El payload se construye **antes** del commit. Después, los objetos de ORM quedan
    # expirados y leer `finding.id` dispara una recarga que en SQLAlchemy asíncrono necesita
    # un contexto verde: sin él, la lectura del payload revienta con `MissingGreenlet` y el
    # escaneo —que ya está confirmado— se reporta como fallido.
    #
    # Además es lo correcto por otro motivo: el cuerpo del evento describe lo que se
    # acaba de guardar, y leerlo antes de confirmar lo lee de los objetos que se van a
    # persistir, no de una recarga que podría devolver otra cosa.
    payload = vulnerability_created_payload(
        [
            {
                "id": str(finding.id),
                "severity": finding.severity,
                "title": finding.title,
                "run_id": str(claim.run_id),
            }
            for finding in findings
        ]
    )
    await session.commit()
    await publish_event(session, EventType.VULNERABILITY_CREATED, claim.organization_id, payload)
    return findings


async def _finalize_review(
    session: AsyncSession,
    claim: PipelineClaim,
    findings: list[Vulnerability],
    client: BaseGitClient,
    comment_dispatch: CommentDispatch,
) -> str:
    critical_count = sum(
        finding.severity == SeverityEnum.CRITICAL for finding in findings
    )
    high_count = sum(finding.severity == SeverityEnum.HIGH for finding in findings)
    blocked = critical_count > 0 or high_count > 0
    claim.review.issues_caught_critical = critical_count
    claim.review.issues_caught_high = high_count
    claim.review.merge_blocked = blocked
    claim.review.status = PRReviewStatusEnum.FAILED if blocked else PRReviewStatusEnum.PASSED
    claim.review.finished_at = datetime.now(UTC)
    await session.flush()
    state = "failure" if blocked else "success"
    description = (
        f"Fenix review: {critical_count} critical, {high_count} high findings."
        if blocked
        else "Fenix review: no critical or high findings."
    )
    comment = build_pr_comment_markdown(
        findings,
        f"{settings.frontend_base_url.rstrip('/')}/issues",
    )
    comentario = await _publica_comentario_de_revision(client, claim, comment)
    # Va después de publicar el comentario y **antes** del `commit`, y no puede tumbar nada:
    # los hallazgos ya están guardados y confirmados, así que un badge perdido no puede
    # convertir un análisis que terminó en una revisión que falló. Antes sí lo hacía, porque
    # `_publish_status` propagaba el `GitClientError` y el `except Exception` del pipeline
    # marcaba la revisión en `ERROR` con el run ya en `COMPLETED`.
    await _publica_estado_del_commit(client, claim, state, description)
    # El payload y el valor de retorno se resuelven **antes** del commit. Después, con una
    # sesión que expire, `claim.review.id` o `claim.review.status` piden una recarga que
    # necesita contexto verde: la revisión quedaría guardada como completada y el pipeline
    # se reportaría como fallido al leer su propio resultado.
    payload = pr_review_payload(
        review_id=claim.review_id,
        repository_id=claim.repository_id,
        pr_number=claim.pr_number,
        status=claim.review.status.value,
        findings_count=len(findings),
        blocking=blocked,
    )
    status_final = claim.review.status.value

    await session.commit()
    await publish_event(
        session, EventType.PR_REVIEW_COMPLETED, claim.organization_id, payload
    )
    # Y lo último, **después** del commit y del evento: el reintento del comentario se encola
    # sabiendo que el veredicto ya es visible. Si se encolara antes y el encolado fallara, se
    # perdería el reintento de un comentario que ya no queda pendiente de nada.
    if comentario.intentado and not comentario.publicado:
        await _encola_reintento_del_comentario(claim.review_id, comment_dispatch)
    return status_final


async def _run_pr_security_pipeline(
    review_id: str,
    *,
    session_provider: SessionProvider = _default_session_provider,
    client_builder: ClientBuilder = build_client_for_repository,
    manager_factory: ManagerFactory = StrixSandboxManager,
    materializer: Materializer = materialize_pr_workspace,
    workspace_root: Path | None = None,
    retry_failed: bool = False,
    celery_task_id: str | None = None,
    comment_dispatch: CommentDispatch = _default_comment_dispatch,
) -> str:
    """Ejecuta el pipeline completo y siempre purga el workspace en ``finally``."""

    parsed_review_id = _parse_review_id(review_id)
    claim: PipelineClaim | None = None
    manager: StrixSandboxManager | None = None
    client: BaseGitClient | None = None
    async with session_provider() as session:
        try:
            claim = await _claim_review(
                session,
                parsed_review_id,
                retry_failed=retry_failed,
                celery_task_id=celery_task_id,
            )
            if claim is None:
                return "SKIPPED"
            client = await client_builder(session, claim.repository)
            # El primer paso del pipeline es el más cosmético, y por eso no puede tumbarlo. Con
            # un repositorio real el `404` no aparece y nada se nota; cuando aparece, lo que
            # falla es el escaneo entero, sin haber intentado escanear nada.
            await _publica_estado_del_commit(
                client,
                claim,
                "pending",
                "Fenix security review in progress",
            )
            manager = manager_factory(
                run_id=str(claim.run.id),
                target=claim.run.target_identifier,
                scan_mode="quick",
                target_type="REPOSITORY",
                workspace_root=workspace_root or settings.strix_workspace_root,
            )
            workspace_dir = manager.setup_workspace()
            # El directorio `workspace` del host se monta como `/workspace/target`
            # dentro del sandbox; por eso el repositorio se materializa aquí.
            target_dir = workspace_dir / "workspace"
            modified_files = await materializer(
                claim.repository,
                claim.review,
                target_dir,
                credential=claim.credential,
            )
            manager.included_files = modified_files
            result = await asyncio.to_thread(
                manager.run,
                timeout_seconds=settings.pr_scan_hard_timeout_seconds,
                soft_timeout_seconds=settings.pr_scan_soft_timeout_seconds,
                workspace_prepared=True,
            )
            findings = await _persist_findings(session, claim, result)
            return await _finalize_review(session, claim, findings, client, comment_dispatch)
        except (GitRateLimitError, GitServerError):
            if claim is not None:
                await _mark_pipeline_error(session, claim, "GIT_TRANSIENT_ERROR")
            logger.exception("Fallo Git transitorio en el pipeline PR %s", parsed_review_id)
            raise
        except GitCredencialError as error:
            # La credencial del tenant no estaba en vigor y no se ha podido dejar en vigor. Es el
            # único fallo del pipeline que **no** es del escaneo, y por eso necesita su propio
            # `error_code`: «PR_PIPELINE_FAILED» le diría a quien mira el panel que la revisión se
            # rompió, y la acción que hay que hacer —reconectar la credencial— es otra distinta.
            #
            # Y necesita marcarla aunque después se reintente. `_mark_pipeline_error` solo
            # transiciona lo que estaba en curso, así que un reintento posterior vuelve a
            # `_claim_review` con la revisión en `SCANNING` —porque el propio marcado la devuelve a
            # `ERROR` y el reintento la pone en `QUEUED`— y no se pierde nada por marcar antes.
            #
            # La excepción sale **sin envolver**, y a propósito: `autoretry_for` decide con el tipo,
            # y un `GitCredencialNoDisponibleError` dentro de un `PRPipelineError` se trataría como
            # permanente y no se reintentaría nunca.
            if claim is not None:
                await _mark_pipeline_error(session, claim, error.codigo_revision)
            # `logger.exception` aquí volcaría el `repr` de la excepción, que es inocuo porque
            # el mensaje no lleva nada del proveedor, pero `logger.error` con el estado ya lo dice
            # entero y sin depender de que nadie mire un traceback.
            logger.error(
                "El pipeline PR %s no pudo dejar en vigor la credencial Git: organization=%s "
                "repository=%s estado=%s motivo=%s",
                parsed_review_id,
                claim.organization_id if claim is not None else None,
                claim.repository_id if claim is not None else None,
                error.estado.value,
                type(error).__name__,
            )
            raise
        except Exception as error:
            # Aquí es donde se perdía el motivo. `PR_PIPELINE_FAILED` era un literal: la misma
            # línea para un host sin el cerco de salida que se niega a lanzar el sandbox, para
            # un `DockerException` y para un bug de verdad. Y quien lo lee pierde **exactamente**
            # la información que `diagnostico.py` existe para dar: si lo que falla es la máquina
            # donde corre el worker, se arregla en el host, no en el repositorio.
            #
            # Se usa el mismo clasificador que `tasks.py` y no una lista propia, por la misma razón
            # que allí: dos verdades divergen, y la que se queda vieja es la del clasificador.
            diagnostico = diagnosticar_fallo(error)
            if claim is not None:
                await _mark_pipeline_error(session, claim, diagnostico.codigo)
                if client is not None:
                    await _publica_estado_del_commit(
                        client,
                        claim,
                        "error",
                        "Fenix security review could not be completed",
                    )
            if es_fallo_de_despliegue(error):
                logger.error(
                    "El pipeline PR %s falló por configuración del despliegue, no del código: %s "
                    "(%s). Arreglarlo es en el host donde corre el worker.",
                    parsed_review_id,
                    diagnostico.codigo,
                    diagnostico.comprobacion,
                )
            else:
                logger.exception("Falló el pipeline de revisión PR %s", parsed_review_id)
            raise PRPipelineError("Falló el pipeline de revisión PR") from error
        finally:
            if manager is not None:
                if manager.temp_dir is not None:
                    try:
                        await asyncio.to_thread(manager.cleanup)
                    except Exception:
                        manager.cleanup_pending = True
                        logger.exception("No se pudo purgar el workspace del PR")
                if manager.cleanup_pending and claim is not None:
                    await _set_cleanup_pending(session, claim)
            if client is not None:
                try:
                    close = getattr(client, "close", None)
                    if callable(close):
                        await asyncio.to_thread(close)
                except Exception:
                    logger.exception("No se pudo cerrar el cliente Git del PR")


async def reintentar_comentario_de_revision(
    review_id: str,
    *,
    session_provider: SessionProvider = _default_session_provider,
    client_builder: ClientBuilder = build_client_for_repository,
) -> str:
    """Vuelve a publicar el comentario de un pull request **sin repetir el escaneo**.

    Es la respuesta a «el escaneo funcionó y se perdió igual por no poder dejar un comentario».
    Y es exactamente por eso una función aparte: no toca `pentest_runs`, no lanza el sandbox, no
    pasa por `_claim_review` —y por eso **no vuelve a cobrar nada**—, no cambia el veredicto de
    la revisión y no vuelve a emitir su evento de finalización.

    ## Por qué reconstruye el texto en vez de guardarlo

    Porque R4 deja los hallazgos **inmutables** y porque el texto es una función determinista de
    ellos: `build_pr_comment_markdown` ordena por severidad y por título, así que el comentario
    que se republica es byte a byte el que se quiso publicar. Guardarlo habría sido duplicar en
    una columna mutable lo que ya está sellado en otra parte, y la copia es la que se queda vieja.

    ## Por qué reconstruye un `PipelineClaim` y no llama a `_finalize_review`

    Porque `_finalize_review` **reescribe el veredicto**: recalcula `issues_caught_*`,
    `merge_blocked`, el estado y `finished_at`, y vuelve a emitir `pr_review.completed`. Con los
    mismos datos produciría el mismo resultado —salvo por el `finished_at`—, así que la
    diferencia real no es el dato sino el **efecto**: un reintento de comentario que reescribe la
    fecha de finalización de un análisis ya entregado es un análisis que parece rehecho. Aquí solo
    se toca `comment_id`.

    ## Por qué sale `SKIPPED` y no un error cuando no hay nada que publicar

    Porque «no hay nada que publicar» no es un fallo: es una revisión que no bloquea el merge y
    nunca tuvo comentario, una revisión que no llegó a escanear, o una que ya lo tiene. La tarea
    se encola solo después de un fallo real, así que llegar aquí es o una segunda ejecución —que
    Celery no hace con `autoretry_for`— o una llamada a mano. En los tres casos registrar y
    devolver «no había nada» es la respuesta correcta, y un error artificial convertiría un
    reintento idempotente en una tarea roja que alguien tiene que investigar.

    ## Lo que esta función **no** garantiza, y por qué se acepta

    Que no haya un comentario duplicado. Si el proveedor acepta el comentario y la respuesta se
    pierde por el camino —una conexión que se corta después de que el proveedor ya lo guardó—,
    el reintento no tiene `comment_id` y publica otro. La entrega es **al menos una vez**, no
    exactamente una.

    Se ha descartado la otra mitad de la solución, que sería bloquear la fila de la revisión con
    `FOR UPDATE` durante la llamada: `database-reviewer` es explícito en que una transacción no
    puede mantener bloqueos mientras habla con una API externa, y esa llamada dura lo que dure la
    cola del proveedor. Un comentario duplicado es ruido; un worker bloqueando la fila de la
    revisión mientras GitHub piensa es un escaneo que no arranca.
    """

    parsed_review_id = _parse_review_id(review_id)
    client: BaseGitClient | None = None
    async with session_provider() as session:
        try:
            review_result = await session.execute(
                select(PullRequestReview).where(PullRequestReview.id == parsed_review_id)
            )
            review = review_result.scalar_one_or_none()
            if review is None:
                raise PRPipelineError("La revisión de PR no existe")
            if review.status not in {PRReviewStatusEnum.PASSED, PRReviewStatusEnum.FAILED}:
                logger.info(
                    "No se republica el comentario de la revisión %s: está en %s, y un "
                    "comentario solo tiene sentido sobre un veredicto ya entregado",
                    parsed_review_id,
                    review.status.value,
                )
                return "SKIPPED"
            if review.run_id is None:
                logger.warning(
                    "No se republica el comentario de la revisión %s: no tiene run, así que no "
                    "tiene hallazgos que contar",
                    parsed_review_id,
                )
                return "SKIPPED"
            organization_id = review.organization_id
            repository_id = review.repository_id
            comment_id = review.comment_id
            run_id = review.run_id
            blocked = review.merge_blocked
            run_result = await session.execute(
                select(PentestRun).where(
                    PentestRun.id == run_id,
                    PentestRun.organization_id == organization_id,
                )
            )
            run = run_result.scalar_one_or_none()
            if run is None or run.status != ScanStatusEnum.COMPLETED:
                logger.warning(
                    "No se republica el comentario de la revisión %s: su run no está "
                    "COMPLETED, así que el análisis no se entregó y no hay nada que publicar",
                    parsed_review_id,
                )
                return "SKIPPED"
            if comment_id is None and not blocked:
                return "SKIPPED"
            findings_result = await session.execute(
                select(Vulnerability).where(
                    Vulnerability.run_id == run_id,
                    Vulnerability.organization_id == organization_id,
                )
            )
            findings = list(findings_result.scalars().all())
            repository_result = await session.execute(
                select(Repository).where(
                    Repository.id == repository_id,
                    Repository.organization_id == organization_id,
                )
            )
            repository = repository_result.scalar_one_or_none()
            if repository is None:
                raise PRPipelineError("El repositorio de la revisión no existe")
            claim = PipelineClaim(
                review=review,
                repository=repository,
                credential=None,
                run=run,
                review_id=parsed_review_id,
                organization_id=organization_id,
                run_id=run_id,
                repository_id=repository_id,
                repository_full_name=repository.full_name,
                commit_sha=review.commit_sha,
                pr_number=review.pr_number,
            )
            comment = build_pr_comment_markdown(
                findings,
                f"{settings.frontend_base_url.rstrip('/')}/issues",
            )
            client = await client_builder(session, repository)
            publicacion = await _publica_comentario_de_revision(client, claim, comment)
            if not publicacion.publicado:
                # La excepción sale del cliente **sin envolver** y a propósito: `autoretry_for`
                # decide con el tipo, y un `GitRateLimitError` dentro de un `PRPipelineError` se
                # trataría como permanente y esta tarea no volvería a intentarlo nunca. Es el
                # mismo motivo por el que el pipeline deja salir su `GitCredencialError` pelado.
                if publicacion.error is not None:
                    raise publicacion.error
                raise PRPipelineError(
                    "No se pudo publicar el comentario de la revisión; el análisis no se repite"
                )
            # El identificador se copia a un valor plano **antes** del commit: leerlo del objeto
            # de ORM después de confirmar pide una recarga que, con una sesión que expire,
            # necesita contexto verde. Es el mismo modo de fallo que `_persist_findings`.
            comment_id_publicado = claim.review.comment_id
            await session.commit()
            logger.info(
                "Comentario de la revisión %s republicado sin repetir el escaneo (run=%s, "
                "comment_id=%s)",
                parsed_review_id,
                run_id,
                comment_id_publicado,
            )
            return "POSTED"
        finally:
            if client is not None:
                try:
                    close = getattr(client, "close", None)
                    if callable(close):
                        await asyncio.to_thread(close)
                except Exception:
                    logger.exception("No se pudo cerrar el cliente Git al republicar el comentario")
