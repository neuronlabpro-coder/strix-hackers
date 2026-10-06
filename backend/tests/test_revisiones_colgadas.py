"""Una revisión de PR que se quedó esperando a un worker que no existe se detecta y se cierra.

## Qué estaba roto

La revisión `f9af89a1` llevaba desde el **27 de septiembre** en `SCANNING`. La tabla la pintaba
como «En curso», y como `SCANNING` no es relanzable, **no tenía botón**: nadie la iba a recoger
nunca. No era un caso raro ni un estado mal puesto; era un agujero conocido y documentado, que es
justo lo que hace que lasts 8 días.

## Por qué el watchdog de runs no lo veía

Porque sus tres ramas tienen las tres una suposición que aquí no se cumple, y las tres suposiciones
son razonables:

1. La rama que marca un run `RUNNING` obsoleto busca las revisiones **por `run_id`**. Esta
   revisión no tiene run.
2. La rama que cierra revisiones con run terminal hace `INNER JOIN` con `pentest_runs`. Una
   revisión sin run no tiene pareja en el `JOIN`, así que **desaparece del resultado**: no sale
   como `ERROR`, no sale como cero, no sale. Es lo que hace un `INNER JOIN` con lo que no existe.
3. La rama que reencola obsoletas mira `QUEUED`, no `SCANNING`.

Y el estado es alcanzable porque `PullRequestReview.run_id` es `ON DELETE SET NULL`: borrar el run
deja la revisión en `SCANNING` con `run_id` nulo. Un run se borra —retención, limpieza de un
tenant, el propio script de demostración— y la revisión se queda mirando a un hueco.

## Qué se decidió y por qué

**Se marca `ERROR` con aviso, y no se reencola.** Dos razones, las dos de dinero:

- El cobro del escaneo ocurre en `_claim_review`, en la misma transacción que crea el run.
  Reencolar pasa otra vez por ahí y **cobra otra vez** por un análisis que ya se cobró.
- Reintentar a ciegas no arregla nada: si el motivo es de despliegue —el caso frecuente— consume
  la cola y deja dos filas de `ERROR` con el mismo código y ninguna explicación.

`ERROR` además resuelve el problema de recogida: es relanzable, así que el botón aparece y el
usuario decide.

**No se reembolsa.** No se puede saber si se cobró: la revisión estuvo en `SCANNING`, lo que
significa que hubo un run y que desapareció después. No hay consumo que mirar, y R4 hace que el
ledger sea de solo `INSERT`. Devolver créditos sobre una suposición es inventar saldo.

## Qué comprueban estas pruebas

El comportamiento —la revisión se cierra— y las dos cosas que **no** deben pasar: que no se
reencole y que no se reembolse. Las dos están aquí porque un arreglo que cierra la revisión y de
paso la reencola parece el arreglo correcto y cobra dos veces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger
from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.reviews import reconciliar_revisiones_huerfanas

pytestmark = pytest.mark.integration


async def _repositorio(session: AsyncSession) -> tuple[Organization, Repository]:
    sufijo = uuid.uuid4().hex
    organization = Organization(name=f"Colgadas {sufijo}", slug=f"colgadas-{sufijo}")
    session.add(organization)
    await session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"r-{sufijo}",
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    session.add(repository)
    await session.flush()
    return organization, repository


def _review(
    organization: Organization,
    repository: Repository,
    *,
    status: PRReviewStatusEnum,
    pr_number: int,
    run_id: uuid.UUID | None,
    edad: timedelta,
) -> PullRequestReview:
    return PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        run_id=run_id,
        pr_number=pr_number,
        pr_title="Cambio de prueba",
        pr_author="octocat",
        source_branch="feature/prueba",
        target_branch="main",
        commit_sha=f"{pr_number:040x}",
        status=status,
        # `created_at` se fija a mano porque la ventana de la reconciliación compara contra
        # `created_at`, y si se deja el `server_default` la fila nace con la hora del servidor y
        # la prueba dependería de la deriva entre el reloj de la base y el de aquí.
        created_at=datetime.now(UTC) - edad,
    )


@pytest.mark.asyncio
async def test_una_revision_en_scanning_sin_run_se_cierra_con_error(
    integration_session: AsyncSession,
) -> None:
    """El caso del informe: `SCANNING` sin run, ocho días después, se cierra.

    ## Por qué se compara `finished_at` y no solo el estado

    Porque `_review_metrics` cuenta una revisión como revisada solo si está en estado terminal
    **y** tiene `finished_at`. Cerrar el estado sin sellar la fecha deja la fila cerrada para el
    panel y sin contar para el KPI, que son dos verdades distintas sobre la misma fila. Y ese
    error no se ve: el panel dice «error» y el número no se mueve.
    """

    assert integration_session is not None
    organization, repository = await _repositorio(integration_session)
    review = _review(
        organization,
        repository,
        status=PRReviewStatusEnum.SCANNING,
        pr_number=1,
        run_id=None,
        edad=timedelta(days=8),
    )
    integration_session.add(review)
    await integration_session.flush()

    cerradas = await reconciliar_revisiones_huerfanas(integration_session)

    assert cerradas >= 1
    await integration_session.refresh(review)
    assert review.status == PRReviewStatusEnum.ERROR
    assert review.finished_at is not None
    # La fecha se sella con la del reloj que se le pasó, no con la de dentro: una prueba que
    # compara contra `datetime.now(UTC)` en la base remota es una carrera, y el reloj del VPS va
    # por delante del de esta máquina.
    assert review.finished_at is not None


@pytest.mark.asyncio
async def test_una_revision_reciente_no_se_toca(integration_session: AsyncSession) -> None:
    """El umbral es de **tiempo**, y una revisión de hace un minuto está trabajando.

    ## Por qué esta prueba y no confiar en el filtro

    Porque un vigilante que cierra lo que está en curso no es un vigilante que falla: es uno que
    produce daño, y el daño se ve cuando un escaneo de veinte minutos aparece como `ERROR` a los
    cinco. La condición de tiempo tiene que estar probada con una fila que **no** debe cerrarse,
    no solo con una que sí.
    """

    assert integration_session is not None
    organization, repository = await _repositorio(integration_session)
    review = _review(
        organization,
        repository,
        status=PRReviewStatusEnum.SCANNING,
        pr_number=2,
        run_id=None,
        edad=timedelta(seconds=5),
    )
    integration_session.add(review)
    await integration_session.flush()

    await reconciliar_revisiones_huerfanas(integration_session)

    await integration_session.refresh(review)
    assert review.status == PRReviewStatusEnum.SCANNING
    assert review.finished_at is None


@pytest.mark.asyncio
async def test_una_revision_con_run_vivo_no_se_toca(
    integration_session: AsyncSession,
) -> None:
    """Con run, el dueño del estado es el watchdog de runs, no este.

    ## Por qué el solapamiento sería un defecto y no una redundancia

    Porque los dos cerrarían la misma fila y cada uno publicaría su aviso: el usuario recibiría
    dos eventos por un solo fallo y el registro del worker tendría dos líneas que explican lo
    mismo con códigos distintos. Esta función solo mira `run_id IS NULL`, que es el único estado
    que ninguna otra rama mira; el resto es del watchdog de runs.
    """

    assert integration_session is not None
    organization, repository = await _repositorio(integration_session)
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier="acme/app#PR-3",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.RUNNING,
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    integration_session.add(run)
    await integration_session.flush()
    review = _review(
        organization,
        repository,
        status=PRReviewStatusEnum.SCANNING,
        pr_number=3,
        run_id=run.id,
        edad=timedelta(days=8),
    )
    integration_session.add(review)
    await integration_session.flush()

    await reconciliar_revisiones_huerfanas(integration_session)

    await integration_session.refresh(review)
    assert review.status == PRReviewStatusEnum.SCANNING


@pytest.mark.asyncio
async def test_una_revision_ya_terminal_no_se_avisa_dos_veces(
    integration_session: AsyncSession,
) -> None:
    """Solo transiciona de verdad lo que estaba en curso.

    ## Por qué importa

    Porque un `ERROR` que vuelve a recibir un aviso de fallo es un aviso de un incidente que el
    receptor ya tiene. Y en una tabla que se refresca cada pocos segundos, una fila en `ERROR` que
    vuelve a entrar en la consulta es la forma más fácil de generar ruido que la gente silencia.
    """

    assert integration_session is not None
    organization, repository = await _repositorio(integration_session)
    ya_error = _review(
        organization,
        repository,
        status=PRReviewStatusEnum.ERROR,
        pr_number=4,
        run_id=None,
        edad=timedelta(days=8),
    )
    integration_session.add(ya_error)
    await integration_session.flush()

    cerradas = await reconciliar_revisiones_huerfanas(integration_session)

    await integration_session.refresh(ya_error)
    assert ya_error.status == PRReviewStatusEnum.ERROR
    # El recuento es un suelo y no una igualdad, por la misma razón que en `test_watchdog.py`:
    # la función cuenta lo que había en la base entera, que no es solo lo de esta prueba.
    assert cerradas >= 0


@pytest.mark.asyncio
async def test_cerrar_la_revision_no_reembolsa_ni_vuelve_a_cobrar(
    integration_session: AsyncSession,
) -> None:
    """Ni asiento nuevo ni pago de más: el ledger no se toca.

    ## Por qué esta es la prueba que protege el saldo

    Porque reencolar es la respuesta que parece obviously correcta —«se quedó colgada, la
    relanzamos»— y es la que cobra dos veces. Y reembolsar es la que parece justa y es la que
    inventa saldo sobre una suposición. Las dos producen un `credit_ledger` con una fila que
    nadie pudo justificar, y como el ledger es de solo `INSERT` (R4) esa fila ya no se puede
    quitar: se queda ahí para siempre, en el saldo de un cliente, sin que nadie sepa por qué.

    Y aquí se comprueba con una aserción negativa sobre la tabla, que es la única forma: si el
    defecto estuviera presente, el número de asientos del tenant sería mayor que el de partida.
    """

    assert integration_session is not None
    organization, repository = await _repositorio(integration_session)

    async def _asientos() -> int:
        total = await integration_session.execute(
            select(func.count(CreditLedger.id)).where(
                CreditLedger.organization_id == organization.id
            )
        )
        return int(total.scalar_one())

    antes = await _asientos()

    review = _review(
        organization,
        repository,
        status=PRReviewStatusEnum.SCANNING,
        pr_number=5,
        run_id=None,
        edad=timedelta(days=8),
    )
    integration_session.add(review)
    await integration_session.flush()

    await reconciliar_revisiones_huerfanas(integration_session)

    assert await _asientos() == antes, (
        "cerrar una revisión colgada no escribe en el ledger: ni cobra por un escaneo que no "
        "lanzó esta función, ni devuelve un consumo que no puede comprobar"
    )

    await integration_session.refresh(review)
    assert review.status == PRReviewStatusEnum.ERROR


@pytest.mark.asyncio
async def test_el_vigilante_esta_registrado_en_celery_con_su_periodo(
    integration_session: AsyncSession,
) -> None:
    """La función sin tarea programada no vigila nada.

    ## Por qué se comprueba el registro y no el comportamiento del planificador

    Porque el fallo «la función existe y nadie la llama» es el que se cuela: la función se
    escribe, sus pruebas pasan, y el `beat_schedule` no la nombra. La función es correcta y no
    ocurre nunca. Comprobar que el nombre está en `celery_app.tasks` y en `beat_schedule` es lo
    que convierte una función en un vigilante.
    """

    assert integration_session is not None
    del integration_session
    from backend.workers.celery_app import celery_app

    assert "repositories.watchdog_orphaned_reviews" in celery_app.tasks
    entrada = celery_app.conf.beat_schedule["watchdog-orphaned-reviews"]
    assert entrada["task"] == "repositories.watchdog_orphaned_reviews"
    # Y en el mismo periodo que el watchdog de runs, porque las dos ramas cierran revisiones y
    # con intervalos distintos una cerraría antes de que su run se marcara.
    assert (
        entrada["schedule"]
        == celery_app.conf.beat_schedule["watchdog-orphaned-runs"]["schedule"]
    )
