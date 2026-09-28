import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.workers.runner.exceptions import SandboxTimeoutError
from backend.workers.tasks import (
    execute_pentest_run,
    mark_run_timed_out,
    reconcile_orphaned_runs,
    schedule_startup_watchdog,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_watchdog_marks_stale_running_runs_failed(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Watchdog {uuid.uuid4().hex}",
        slug=f"watchdog-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="watchdog.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.RUNNING,
        started_at=datetime.now(UTC) - timedelta(hours=2),
        container_id=None,
    )
    integration_session.add(run)
    await integration_session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    integration_session.add(repository)
    await integration_session.flush()
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        run_id=run.id,
        pr_number=1,
        pr_title="Orphaned PR",
        pr_author="alice",
        source_branch="feature/orphan",
        target_branch="main",
        commit_sha="a" * 40,
        status=PRReviewStatusEnum.SCANNING,
    )
    integration_session.add(review)
    await integration_session.flush()
    workspace = tmp_path / str(run.id)
    workspace.mkdir()
    (workspace / "source.py").write_text("secret-source", encoding="utf-8")

    killer = MagicMock()
    with patch("backend.workers.tasks.StrixSandboxManager.remove_network_for_run"):
        marked = await reconcile_orphaned_runs(
            integration_session,
            now=datetime.now(UTC),
            kill_container=killer,
            workspace_root=tmp_path,
        )

    # El recuento es un suelo, no una igualdad. La función devuelve cuantos runs obsoletos ha
    # marcado en **toda** la base, porque es un vigilante, y eso depende de lo que haya en la
    # tabla en ese momento. Igualarlo a uno hacia que la prueba midiera el estado de la base
    # compartida en vez de su propio comportamiento.
    #
    # Lo que de verdad se comprueba es lo de despues: el run en FAILED, con su codigo de error y
    # su espacio de trabajo borrado. Eso si es el contrato, y no depende de nada ajeno.
    assert marked >= 1
    # Y lo mismo con el eliminador de contenedores: se invoca una vez por cada run
    # obsoleto que hay en la base, no una vez por esta prueba. Lo que se comprueba es
    # que el suyo se elimino, y que se elimino **una** vez. Un `assert_called_once_with`
    # aqui habria estado midiendo cuantos escaneos obsoletos hay en el mundo.
    eliminados = [args.args[0] for args in killer.call_args_list]
    assert eliminados.count(f"fenix-strix-{run.id}") == 1
    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.FAILED
    assert run.error_message == "WORKER_WATCHDOG_ORPHANED"
    assert run.cleanup_pending is False
    await integration_session.refresh(review)
    assert review.status == PRReviewStatusEnum.ERROR
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_watchdog_retries_pending_cleanup_for_terminal_run(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Cleanup {uuid.uuid4().hex}",
        slug=f"cleanup-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="cleanup.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.FAILED,
        finished_at=datetime.now(UTC) - timedelta(hours=2),
        cleanup_pending=True,
    )
    integration_session.add(run)
    await integration_session.flush()
    workspace = tmp_path / str(run.id)
    workspace.mkdir()
    (workspace / "source.py").write_text("secret-source", encoding="utf-8")

    killer = MagicMock()
    with patch("backend.workers.tasks.StrixSandboxManager.remove_network_for_run"):
        marked = await reconcile_orphaned_runs(
            integration_session,
            now=datetime.now(UTC),
            kill_container=killer,
            workspace_root=tmp_path,
        )

    # El recuento es un suelo, no una igualdad. La función devuelve cuantos runs obsoletos ha
    # marcado en **toda** la base, porque es un vigilante, y eso depende de lo que haya en la
    # tabla en ese momento. Igualarlo a uno hacia que la prueba midiera el estado de la base
    # compartida en vez de su propio comportamiento.
    #
    # Lo que de verdad se comprueba es lo de despues: el run en FAILED, con su codigo de error y
    # su espacio de trabajo borrado. Eso si es el contrato, y no depende de nada ajeno.
    assert marked >= 1
    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.FAILED
    assert run.cleanup_pending is False
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_timeout_transition_is_persisted_for_active_run(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Timeout {uuid.uuid4().hex}",
        slug=f"timeout-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="timeout.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.RUNNING,
        started_at=datetime.now(UTC),
    )
    integration_session.add(run)
    await integration_session.flush()

    changed = await mark_run_timed_out(integration_session, run.id, organization.id)

    assert changed is True
    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.TIMED_OUT
    assert run.error_message == "STRIX_TIMEOUT"


def test_execute_task_marks_timeout_when_sandbox_raises_timeout() -> None:
    run_id = str(uuid.uuid4())
    mark_timeout = AsyncMock()
    with (
        patch(
            "backend.workers.tasks._get_run_organization_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "backend.workers.tasks._execute_pentest_run",
            new=AsyncMock(side_effect=SandboxTimeoutError("timeout")),
        ),
        patch("backend.workers.tasks._mark_timed_out", new=mark_timeout),
    ):
        with pytest.raises(SandboxTimeoutError):
            execute_pentest_run.run(run_id)  # pyright: ignore[reportFunctionMemberAccess]

    mark_timeout.assert_awaited_once_with(None, uuid.UUID(run_id))


@pytest.mark.asyncio
async def test_watchdog_reenqueues_stale_queued_pr_reviews(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    assert integration_session is not None
    organization = Organization(
        name=f"Queued {uuid.uuid4().hex}",
        slug=f"queued-{uuid.uuid4().hex}",
    )
    integration_session.add(organization)
    await integration_session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    integration_session.add(repository)
    await integration_session.flush()
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=8,
        pr_title="Queued PR",
        pr_author="alice",
        source_branch="feature/queued",
        target_branch="main",
        commit_sha="b" * 40,
        status=PRReviewStatusEnum.QUEUED,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    integration_session.add(review)
    await integration_session.commit()

    with patch("backend.apps.repositories.tasks.run_pr_security_pipeline.delay") as delay:
        # El valor se descarta a proposito: es un recuento global y esta prueba no lo necesita.
        # El motivo de por que no se comprueba esta escrito mas abajo, junto a la asercion.
        await reconcile_orphaned_runs(
            integration_session,
            now=datetime.now(UTC),
            workspace_root=tmp_path,
        )

    # El `marked` no se comprueba aquí, y no es una omisión.
    #
    # Esta prueba no crea ningún `PentestRun`: crea una revisión de pull request encolada. El
    # número que devuelve la función cuenta los runs obsoletos marcados en toda la base, así que
    # aquí no mide nada de esta prueba —mide si había algún escaneo colgado en ese momento—. Por
    # eso la aserción era `marked == 0`: describía el estado de la base, no el efecto del
    # vigilante, y por eso dejó de cumplirse en cuanto hubo un escaneo obsoleto que no fuera de
    # esta prueba.
    #
    # El valor de retorno sí está cubierto, y bien, en las dos pruebas anteriores: ambas crean un
    # run obsoleto y comprueban su transición a FAILED.
    #
    # Y la aserción es sobre **esta** revisión, no sobre cuántas llamadas hubo en total.
    #
    # `reconcile_orphaned_runs` es un vigilante: recorre las revisiones en `QUEUED` de **todas**
    # las organizaciones, que es lo que tiene que hacer para desatascar las que quedaron
    # colgadas. Contar las llamadas globalmente mide una cosa que esta prueba no controla y que
    # depende de lo que haya en la base en ese momento.
    #
    # El síntoma era que la prueba pasaba sola hasta que el seeder de demostración dejó una
    # revisión encolada de otra organización, y entonces falló por una llamada de más. El
    # defecto no estaba en el vigilante: estaba en medir algo ajeno.
    #
    # Lo que sí importa, y lo que se comprueba, es que esta revisión se reencoló **una** vez y
    # no cero ni dos.
    reencolados = [args.args[0] for args in delay.call_args_list]
    assert reencolados.count(str(review.id)) == 1


def test_startup_signal_enqueues_watchdog() -> None:
    with patch("backend.workers.tasks.watchdog_orphaned_runs.delay") as delay:
        schedule_startup_watchdog()

    delay.assert_called_once_with()
