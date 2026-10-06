"""Un reintento no cobra dos veces, y un comentario que falla no tira el escaneo.

## Los dos defectos que este fichero existe para no dejar volver

**El primero es dinero multiplicado por el fallo de infraestructura más probable del producto.**
`run_pr_security_pipeline` reintenta ante `GitRateLimitError` con `max_retries=3`, y el
reintento llegaba a `_claim_review` con `retry_failed=True`, que ponía la revisión en `QUEUED` con
`run_id = None` y construía un `PentestRun` **nuevo**. Cada intento se cobraba su propio
`apply_credit_delta`. Un `429` de GitHub —«demasiadas peticiones», que es más probable cuando la
plataforma acaba de lanzar varios escaneos a la vez— costaba cuatro escaneos cobrados por uno solo.

**El segundo es la misma clase exacta que el badge, en el paso siguiente.** En `_finalize_review`,
`post_pr_comment` y `update_pr_comment` lanzaban `GitClientError`, el error salía por el
`except Exception` del pipeline, `_mark_pipeline_error` ponía la revisión en `ERROR`… con el run ya
en `COMPLETED`, con sus hallazgos escritos y con sus créditos cobrados. El cliente veía «Error»
encima de un análisis que sí se había hecho, y el reintento de Celery relanzaba el escaneo entero.

## Por qué el andamiaje está duplicado y no importado

Porque hay más de un agente trabajando en este árbol y los ficheros de pruebas se tocan entre
ellos: un `from backend.tests.test_pr_pipeline import _crear_pipeline_review` convierte el fichero
ajeno en una dependencia de este, y el día que alguien lo reformatee, esto falla por un motivo que
no tiene nada que ver con lo que mide. Los datos de prueba se duplican a propósito.

## Por qué las afirmaciones van contra el ledger y no contra una constante

Porque lo que hay que demostrar es «cuántos asientos hay», no «cuánto cuesta un escaneo». Una
prueba que comparara el saldo contra `scan_credit_cost` pasaría con un cobro duplicado si el precio
cambia, y no mide nada. El ledger es *append-only* (R4): la fila que se escribió ahí no se puede
quitar, así que el número de filas **es** el defecto.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.pricing import scan_credit_cost
from backend.apps.billing.service import apply_credit_delta, credit_balance_of
from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum
from backend.apps.repositories.clients.base import GitClientError, GitRateLimitError
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.pipeline import (
    _run_pr_security_pipeline,
    reintentar_comentario_de_revision,
)
from backend.apps.repositories.reviews import lanzar_analisis_de_review
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.core.crypto import encrypt_secret

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Andamiaje
# --------------------------------------------------------------------------- #


async def _revision_para_el_pipeline(
    session: AsyncSession,
    *,
    saldo: str = "100",
    status: PRReviewStatusEnum = PRReviewStatusEnum.QUEUED,
) -> tuple[Organization, Repository, PullRequestReview]:
    """Un tenant con saldo **por el ledger**, un repositorio, su credencial y una revisión.

    ## Por qué el saldo entra por `apply_credit_delta` y no por la columna

    Porque `credit_balance_of` suma el ledger y `organizations.credit_balance` es una caché. Si la
    prueba escribiera la columna directamente, las dos verdades no coincidirían y una aserción
    sobre el saldo estaría midiendo un número que en producción no existe. En producción el saldo
    de partida es siempre un bono, nunca un `UPDATE`, y la prueba tiene que medir esa realidad.
    """

    sufijo = uuid.uuid4().hex
    organization = Organization(name=f"Cobro PR {sufijo}", slug=f"cobro-pr-{sufijo}")
    session.add(organization)
    await session.flush()
    await apply_credit_delta(
        session=session,
        organization_id=organization.id,
        amount=Decimal(saldo),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
    )
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"repo-{uuid.uuid4().hex[:12]}",
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    session.add(repository)
    await session.flush()
    session.add(
        GitCredential(
            organization_id=organization.id,
            provider=GitProviderEnum.GITHUB,
            encrypted_access_token=encrypt_secret(
                "github-token",
                organization_id=str(organization.id),
                provider=GitProviderEnum.GITHUB.value,
            ),
        )
    )
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=77,
        pr_title="Cambio",
        pr_author="alice",
        source_branch="feature/cambio",
        target_branch="main",
        commit_sha="b" * 40,
        base_sha="f" * 40,
        head_clone_url="https://github.com/acme/app.git",
        status=status,
    )
    session.add(review)
    await session.commit()
    return organization, repository, review


class _ClienteFalso:
    """Cliente Git que registra lo que se le pidió y puede negarse solo a comentar.

    El defecto que se está probando es «el comentario falla», así que el rechazo se produce en
    `post_pr_comment` y `update_pr_comment` y **en ningún otro sitio**: una prueba que fallara
    por el badge o por la materialización no mediría lo que dice medir, que es la clase de error
    que este proyecto ya ha pagado una vez.
    """

    def __init__(self, *, comentario_falla: BaseException | None = None) -> None:
        self.comentarios: list[str] = []
        self.actualizaciones: list[str] = []
        self.estados: list[str] = []
        self.cerrado = False
        self.comentario_falla = comentario_falla

    def set_commit_status(
        self,
        _repo_full_name: str,
        _sha: str,
        state: str,
        _description: str,
        _target_url: str,
    ) -> None:
        self.estados.append(state)

    def post_pr_comment(self, _repo_full_name: str, _pr_number: int, body: str) -> str:
        if self.comentario_falla is not None:
            raise self.comentario_falla
        self.comentarios.append(body)
        return "comment-1"

    def update_pr_comment(
        self, _repo_full_name: str, _comment_id: str, body: str
    ) -> None:
        if self.comentario_falla is not None:
            raise self.comentario_falla
        self.actualizaciones.append(body)

    def close(self) -> None:
        self.cerrado = True


class _ManagerFalso:
    """Un sandbox de mentira que encuentra un `CRITICAL`.

    Lleva un contador de ejecuciones porque la aserción que importa es **cuántas veces corrió**:
    un reintento del comentario que volviera a escanear dejaría el contador en dos, y esa es
    exactamente la pregunta que este fichero hace.
    """

    def __init__(self, run_id: str, target: str, workspace_root: Path) -> None:
        self.run_id = run_id
        self.target = target
        self.workspace_root = workspace_root
        self.temp_dir: Path | None = None
        self.cleanup_pending = False
        self.ejecuciones = 0

    def setup_workspace(self) -> Path:
        run_dir = self.workspace_root / self.run_id
        (run_dir / "workspace").mkdir(parents=True)
        (run_dir / "output").mkdir()
        self.temp_dir = run_dir
        return run_dir

    def run(self, **_kwargs: Any) -> SimpleNamespace:
        self.ejecuciones += 1
        return SimpleNamespace(
            exit_code=0, output_json=_informe_con_un_critical(), container_id="container-1"
        )

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None


def _informe_con_un_critical() -> str:
    return json.dumps(
        {
            "status": "completed",
            "scan_id": "scan-cobro-1",
            "findings": [
                {
                    "id": "finding-cobro-1",
                    "title": "Inyección en el controlador",
                    "description": "Un finding reproducible",
                    "severity": SeverityEnum.CRITICAL.value,
                    "cvss_score": 9.8,
                    "affected_target": "changed.py:1",
                    "poc_reproduction_raw": "pytest -q test_security.py",
                    "autofix_patch_diff": "--- a/changed.py\n+++ b/changed.py",
                }
            ],
        }
    )


def _materializador_que_prepara_el_workspace() -> Any:
    async def materializer(
        _repository: Repository,
        review: PullRequestReview,
        target_dir: Path,
        *,
        credential: GitCredential | None,
    ) -> list[str]:
        assert review.id is not None
        assert credential is not None, "el pipeline de escaneo siempre lleva credencial"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "changed.py").write_text("print('changed')\n", encoding="utf-8")
        return ["changed.py"]

    return materializer


@asynccontextmanager
async def _sesion_de_la_prueba(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


async def _asientos(session: AsyncSession, organization_id: uuid.UUID) -> list[CreditLedger]:
    """Los asientos del tenant, **sin garantía de orden**.

    No hay `ORDER BY` porque no hay columna por la que ordenar: `created_at` es
    `server_default=func.now()` y en PostgreSQL `now()` es la marca de **transacción**, así que
    dos asientos de la misma transacción comparten reloj y el `id` es un UUIDv4 que no ordena por
    inserción. Cada prueba localiza los suyos por `reason`, `reference_id` y `amount_delta`, que es
    lo que significa, en vez de por la posición que ocupan.
    """

    resultado = await session.execute(
        select(CreditLedger)
        .where(CreditLedger.organization_id == organization_id)
        .order_by(CreditLedger.reason, CreditLedger.reference_id)
    )
    return list(resultado.scalars().all())


def _costos(saldo_inicial: str = "100") -> Decimal:
    """El precio que el pipeline cobra por una revisión, leído de la misma tabla que él lee."""

    return scan_credit_cost(ScanModeEnum.QUICK)


# --------------------------------------------------------------------------- #
# 1. El reintento no vuelve a cobrar
# --------------------------------------------------------------------------- #


async def test_un_reintento_por_limite_de_tasa_no_cobra_un_segundo_escaneo(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Un `429` de GitHub reintenta el pipeline y el tenant paga **una** revisión.

    ## La secuencia es la de producción, no una simplificación

    El primer intento falla **antes** del sandbox, en `build_client_for_repository`: es donde un
    `429` ocurre de verdad, porque el camino que lo produce es renovar o validar la credencial
    contra el proveedor. `_claim_review` ya había confirmado el cobro, así que cuando la tarea
    reintenta con `retry_failed=True` la revisión está en `ERROR`, ya se pagó, y el reintento crea
    un `PentestRun` nuevo. Ese es el escenario exacto que costaba cuatro cobros.

    ## Lo que se mira, y por qué en este orden

    1. **Que el fallo se propaga**, o sea que el primer intento cobra y no escanea: sin esto la
       prueba pasaría sin haber probado nada.
    2. **El número de asientos después del reintento**, que es el defecto.
    3. **Que el escaneo se entrega**, porque un arreglo que no cobró el reintento tampoco lo
       escaneó — y eso sería un arreglo que regala análisis, no uno que arregla el cobro.
    4. **Que no hay asiento compensatorio**: la política elegida es *no volver a cobrar*, no
       *cobrar y devolver*, y un reembolso que no existe es parte del contrato.
    """

    assert integration_session is not None
    organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    organization_id = organization.id
    review_id = review.id
    managers: list[_ManagerFalso] = []
    coste = _costos()

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        assert scan_mode == "quick"
        assert target_type == "REPOSITORY"
        manager = _ManagerFalso(run_id, target, workspace_root)
        managers.append(manager)
        return manager

    async def cliente_que_exige_cuota(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        # El proveedor agota su cuota justo al validar la credencial. Lo que sale de aquí es un
        # `GitRateLimitError`, que es lo que `autoretry_for` de la tarea sabe reintentar.
        raise GitRateLimitError(
            "El proveedor Git agotó su cuota temporal", status_code=429, retry_after=60
        )

    async def cliente_que_no_falla(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso()

    # --- Intento 1: GitHub devuelve 429 antes de que exista un sandbox ------------------- #
    with pytest.raises(GitRateLimitError):
        await _run_pr_security_pipeline(
            str(review_id),
            session_provider=lambda: _sesion_de_la_prueba(integration_session),
            client_builder=cliente_que_exige_cuota,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=_materializador_que_prepara_el_workspace(),
            workspace_root=tmp_path,
        )

    assert managers == [], "el sandbox no llegó a construirse: el fallo fue de cliente Git"
    tras_el_primer_intento = await _asientos(integration_session, organization_id)
    consumos_primeros = [
        a
        for a in tras_el_primer_intento
        if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(consumos_primeros) == 1, tras_el_primer_intento
    assert consumos_primeros[0].amount_delta == -coste

    # --- Intento 2: el mismo encargo, con `retry_failed=True` ------------------------------- #
    resultado = await _run_pr_security_pipeline(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=cliente_que_no_falla,  # pyright: ignore[reportArgumentType]
        manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
        materializer=_materializador_que_prepara_el_workspace(),
        workspace_root=tmp_path,
        retry_failed=True,
    )

    assert resultado == PRReviewStatusEnum.FAILED.value
    # Y el escaneo ocurrió una vez, no dos: la máquina que iba a construir el primer intento nunca
    # llegó a instanciarse.
    assert len(managers) == 1
    assert managers[0].ejecuciones == 1

    # El defecto, medido donde se mide: **un** asiento de consumo para dos ejecuciones.
    asientos = await _asientos(integration_session, organization_id)
    consumos = [a for a in asientos if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION]
    assert len(consumos) == 1, [str(a.reference_id) for a in asientos]
    assert consumos[0].amount_delta == -coste
    assert await credit_balance_of(integration_session, organization_id) == Decimal("100") - coste

    guardada = await integration_session.get(PullRequestReview, review_id)
    assert guardada is not None
    assert guardada.status == PRReviewStatusEnum.FAILED
    assert guardada.merge_blocked is True
    run = await integration_session.get(PentestRun, guardada.run_id)
    assert run is not None
    assert run.status == ScanStatusEnum.COMPLETED


async def test_un_reintento_no_devuelve_nada_tampoco(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """La otra mitad de la política: el reintento **no reembolsa**, porque no cobra.

    ## Por qué hace falta una prueba aparte y no una aserción más en la anterior

    Porque son dos políticas distintas y se pueden equivocar por separado. Una podría cobrar dos
    veces y devolver dos veces —el saldo del cliente sería el correcto y el defecto seguiría
    debajo, repartido en cuatro asientos que R4 no deja quitar—, y esa es exactamente la
    situación en la que una prueba que solo mira el saldo dice que todo está bien.

    Aquí la aserción es **sobre las filas**, y por eso la forma en que está escrita no depende de
    la aritmética: si el arreglo hubiera sido «cobrar y devolver», el saldo volvería a `100` y esta
    prueba lo vería con dos filas de consumo y dos de devolución.
    """

    assert integration_session is not None
    organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    organization_id = organization.id
    review_id = review.id
    coste = _costos()

    async def cliente_que_exige_cuota(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        raise GitRateLimitError("cuota agotada", status_code=429)

    async def cliente_que_no_falla(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso()

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        del scan_mode, target_type
        return _ManagerFalso(run_id, target, workspace_root)

    with pytest.raises(GitRateLimitError):
        await _run_pr_security_pipeline(
            str(review_id),
            session_provider=lambda: _sesion_de_la_prueba(integration_session),
            client_builder=cliente_que_exige_cuota,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=_materializador_que_prepara_el_workspace(),
            workspace_root=tmp_path,
        )

    await _run_pr_security_pipeline(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=cliente_que_no_falla,  # pyright: ignore[reportArgumentType]
        manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
        materializer=_materializador_que_prepara_el_workspace(),
        workspace_root=tmp_path,
        retry_failed=True,
    )

    asientos = await _asientos(integration_session, organization_id)
    # Un bono de alta y **un** consumo. Ni un segundo consumo, ni una devolución, ni un asiento de
    # ningún otro motivo: el reintento no mueve el dinero en ninguna dirección. El orden es el de
    # la consulta —`reason` y `reference_id`—, y no por posición de inserción, que en PostgreSQL
    # con `now()` como marca de transacción no dice nada.
    assert len(asientos) == 2, [(a.reason.value, a.reference_id) for a in asientos]
    assert [a.reason for a in asientos] == [
        LedgerReasonEnum.SCAN_CONSUMPTION,
        LedgerReasonEnum.SIGNUP_BONUS,
    ]
    assert all("refund" not in (a.reference_id or "") for a in asientos)
    assert await credit_balance_of(integration_session, organization_id) == Decimal("100") - coste


async def test_un_relanzamiento_pedido_por_el_usuario_si_cobra(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Reanalizar desde el panel es un encargo nuevo, y se cobra aunque ya estuviera pagado.

    ## Por qué esta prueba es la que impide arreglar el defecto al revés

    La regla del reintento se puede escribir de dos formas: «un reintento no cobra» —que es lo
    correcto— o «esta revisión no vuelve a cobrarse nunca», que también impediría el cobro doble y
    además dejaría **gratis** el botón de reanalizar. La segunda es un cambio de precio que nadie
    ha pedido: el usuario pidió volver a analizar el código, y el análisis cuesta lo que cuesta.

    Y no se prueba con un `status = QUEUED` escrito a mano sino llamando a
    `lanzar_analisis_de_review`, que es el servicio real del endpoint, para que la prueba mida el
    camino que el panel recorre y no una reconstrucción suya.
    """

    assert integration_session is not None
    organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    organization_id = organization.id
    review_id = review.id
    coste = _costos()
    clientes: list[_ClienteFalso] = []

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        cliente = _ClienteFalso()
        clientes.append(cliente)
        return cliente

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        del scan_mode, target_type
        return _ManagerFalso(run_id, target, workspace_root)

    async def _una_pasada() -> str:
        return await _run_pr_security_pipeline(
            str(review_id),
            session_provider=lambda: _sesion_de_la_prueba(integration_session),
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=_materializador_que_prepara_el_workspace(),
            workspace_root=tmp_path,
        )

    assert await _una_pasada() == PRReviewStatusEnum.FAILED.value
    assert (
        len(
            [
                a
                for a in await _asientos(integration_session, organization_id)
                if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION
            ]
        )
        == 1
    ), "la primera pasada cobra un escaneo"

    # El relanzamiento real del panel: pone la revisión en `QUEUED` y suelta el run anterior.
    await lanzar_analisis_de_review(
        integration_session,
        organization_id=organization_id,
        review_id=review_id,
        dispatch=lambda _review_id: "task-relaunch",
    )

    # Y el relanzamiento no es un reintento: llega con `retries == 0`, así que `retry_failed` es
    # `False` y el pipeline cobra. Ese es el criterio, y se comprueba aquí y no en un comentario.
    assert await _una_pasada() == PRReviewStatusEnum.FAILED.value

    consumos = [
        a
        for a in await _asientos(integration_session, organization_id)
        if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(consumos) == 2, "el relanzamiento del panel es un análisis nuevo y se cobra"
    assert all(a.amount_delta == -coste for a in consumos)
    assert await credit_balance_of(integration_session, organization_id) == (
        Decimal("100") - 2 * coste
    )
    assert len(clientes) == 2


async def test_un_reintento_sin_cobro_previo_si_cobra(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Si la revisión no debe nada, el reintento cobra: la regla mira el ledger, no el flag.

    ## Por qué este caso importa aunque hoy no se produzca

    Porque la regla del reintento se apoya en una invariante que no está escrita en ninguna parte
    del tipo de una línea: «todo fallo reintentable de este pipeline ocurre **después** del
    cobro». Es cierta hoy —el único camino reintentable antes del sandbox es abrir el cliente, y
    `_claim_review` ya confirmó— pero depende de un orden de llamadas que nadie escribió para
    sostenerla.

    Si alguien añadiera mañana un `autoretry_for` nuevo, o moviera el cobro más tarde, un reintento
    sin cobro previo daría un análisis **gratis**. Con esta comprobación al ledger, ese caso se
    cobra, porque la pregunta es «¿esta revisión debe algo?» y la respuesta es que no.

    El estado de partida —una revisión en `ERROR` que nunca llegó a cobrarse— es alcanzable: es lo
    que deja una revisión cerrada por `reconciliar_revisiones_huerfanas` y a la que luego le llega
    un reintento.
    """

    assert integration_session is not None
    organization, _repository, review = await _revision_para_el_pipeline(
        integration_session, status=PRReviewStatusEnum.ERROR
    )
    organization_id = organization.id
    review_id = review.id
    coste = _costos()

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso()

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        del scan_mode, target_type
        return _ManagerFalso(run_id, target, workspace_root)

    resultado = await _run_pr_security_pipeline(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
        manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
        materializer=_materializador_que_prepara_el_workspace(),
        workspace_root=tmp_path,
        retry_failed=True,
    )

    assert resultado == PRReviewStatusEnum.FAILED.value
    consumos = [
        a
        for a in await _asientos(integration_session, organization_id)
        if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(consumos) == 1, "sin cobro previo, el reintento es el primer análisis y se cobra"
    assert consumos[0].amount_delta == -coste


# --------------------------------------------------------------------------- #
# 2. El comentario que falla no tumba el escaneo
# --------------------------------------------------------------------------- #


async def test_un_comentario_que_github_rechaza_no_tumba_el_escaneo(
    integration_session: AsyncSession,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un `403` al comentar deja la revisión con su veredicto real y el run completado.

    ## Por qué `403` y no otro código

    Porque es el caso de verdad: una credencial sin permiso de escritura sobre el repositorio
    devuelve `403` en `POST /issues/{n}/comments`, y `BaseGitClient._raise_http_error` lo levanta
    como `GitClientError` —que **no** está en el `autoretry_for` de la tarea, así que ni siquiera
    reintentaba. Con eso, la revisión acababa en `ERROR` y el análisis se perdía entero.

    ## Lo que se comprueba, y por qué el estado de la fila va primero

    Porque «no lanzó» no es nada: un pipeline que no escaneara tampoco habría lanzado. Lo primero
    que se mira es que la revisión esté en su veredicto **real** (`FAILED`, con `merge_blocked`) y
    no en `ERROR`, que es la traducción directa de «el cliente ve Error sobre un análisis que sí se
    hizo». Después, que el run esté `COMPLETED`, que el hallazgo esté en la base, y que el
    comentario pendiente se haya encolado **por su cuenta**.
    """

    assert integration_session is not None
    organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    review_id = review.id
    coste = _costos()
    rechazados = GitClientError("El proveedor Git rechazó la operación", status_code=403)
    encolados: list[str] = []

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso(comentario_falla=rechazados)

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        del scan_mode, target_type
        return _ManagerFalso(run_id, target, workspace_root)

    with caplog.at_level("WARNING"):
        resultado = await _run_pr_security_pipeline(
            str(review_id),
            session_provider=lambda: _sesion_de_la_prueba(integration_session),
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=_materializador_que_prepara_el_workspace(),
            workspace_root=tmp_path,
            comment_dispatch=lambda _review_id: encolados.append(_review_id) or "tarea-1",
        )

    assert resultado == PRReviewStatusEnum.FAILED.value, "un 403 al comentar no es un ERROR"

    guardada = await integration_session.get(PullRequestReview, review_id)
    assert guardada is not None
    assert guardada.status == PRReviewStatusEnum.FAILED
    assert guardada.status != PRReviewStatusEnum.ERROR
    assert guardada.merge_blocked is True
    assert guardada.finished_at is not None
    # `comment_id` nulo **con** la revisión bloqueando el merge es el estado durable de
    # «el escaneo se entregó y su comentario no». Es lo que hace el reintento idempotente.
    assert guardada.comment_id is None

    run = await integration_session.get(PentestRun, guardada.run_id)
    assert run is not None
    assert run.status == ScanStatusEnum.COMPLETED

    hallazgos = list(
        (
            await integration_session.execute(
                select(Vulnerability).where(Vulnerability.run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(hallazgos) == 1
    assert hallazgos[0].severity == SeverityEnum.CRITICAL

    # Y el reintento se encoló solo, con el identificador de la revisión y nada más.
    assert encolados == [str(review_id)]

    # El cobro tampoco se toca: el análisis se cobró una vez y se sigue cobrando una vez.
    consumos = [
        a
        for a in await _asientos(integration_session, organization.id)
        if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(consumos) == 1
    assert consumos[0].amount_delta == -coste

    # Y el motivo queda escrito con lo que hace falta para diagnosticarlo sin abrir el pipeline.
    mensajes = [registro.getMessage() for registro in caplog.records]
    aviso = next(
        mensaje
        for mensaje in mensajes
        if "No se pudo publicar el comentario" in mensaje
    )
    assert str(review_id) in aviso
    assert "acme/app" in aviso
    assert "77" in aviso
    assert "GitClientError" in aviso
    assert "http=403" in aviso


async def test_el_reintento_del_comentario_no_vuelve_a_escanear_ni_a_cobrar(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Republicar el comentario es una operación aparte: ni sandbox nuevo, ni run nuevo, ni cobro.

    ## Por qué esta es la prueba que cierra el encargo

    Porque «el comentario no tumba el escaneo» y «el comentario se puede reintentar solo» son dos
    mitades, y la segunda es la que se comprueba mal con más facilidad: si el reintento pasara por
    `_claim_review`, volvería a crear un run y —antes del arreglo del punto 1— a cobrar otra vez.
    Aquí se comprueba, sobre el mismo tenant y la misma revisión:

    - que el run es **el mismo** y sigue `COMPLETED`,
    - que `finished_at` —la fecha de finalización del análisis— **no se mueve**, porque un
      reintento de comentario que reescribe esa fecha es un análisis que parece rehecho,
    - que no aparece un segundo `PentestRun`,
    - y que el ledger sigue teniendo **un** asiento de consumo.
    """

    assert integration_session is not None
    organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    review_id = review.id
    organization_id = organization.id
    coste = _costos()
    rechazados = GitClientError("El proveedor Git rechazó la operación", status_code=403)

    async def cliente_que_rechaza(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso(comentario_falla=rechazados)

    async def cliente_que_escribe(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso()

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        del scan_mode, target_type
        return _ManagerFalso(run_id, target, workspace_root)

    managers: list[_ManagerFalso] = []

    def factory_que_cuenta(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        manager = manager_factory(run_id, target, scan_mode, target_type, workspace_root)
        managers.append(manager)
        return manager

    await _run_pr_security_pipeline(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=cliente_que_rechaza,  # pyright: ignore[reportArgumentType]
        manager_factory=factory_que_cuenta,  # pyright: ignore[reportArgumentType]
        materializer=_materializador_que_prepara_el_workspace(),
        workspace_root=tmp_path,
        comment_dispatch=lambda _review_id: "tarea-1",
    )
    assert len(managers) == 1

    antes = await integration_session.get(PullRequestReview, review_id)
    assert antes is not None
    run_id_original = antes.run_id
    finished_at_original = antes.finished_at

    # --- Ahora el reintento, con un proveedor que ya acepta el comentario ------------------- #
    cliente_del_reintento = _ClienteFalso()

    async def cliente_que_escribe_con_doble(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return cliente_del_reintento

    resultado = await reintentar_comentario_de_revision(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=cliente_que_escribe_con_doble,  # pyright: ignore[reportArgumentType]
    )
    assert resultado == "POSTED"
    assert cliente_del_reintento.cerrado is True, "el cliente del reintento también se cierra"
    # Y el comentario lleva el informe que el cliente no tenía: el hallazgo y su PoC.
    assert len(cliente_del_reintento.comentarios) == 1
    assert "CRITICAL" in cliente_del_reintento.comentarios[0]
    assert "pytest -q test_security.py" in cliente_del_reintento.comentarios[0]

    despues = await integration_session.get(PullRequestReview, review_id)
    assert despues is not None
    assert despues.comment_id == "comment-1"
    assert despues.run_id == run_id_original
    assert despues.finished_at == finished_at_original
    assert despues.status == PRReviewStatusEnum.FAILED
    assert despues.merge_blocked is True

    assert len(managers) == 1, "el reintento del comentario no lanza un sandbox"
    runs = list(
        (
            await integration_session.execute(
                select(PentestRun).where(PentestRun.organization_id == organization_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1, "el reintento del comentario no crea un run"
    assert runs[0].status == ScanStatusEnum.COMPLETED

    consumos = [
        a
        for a in await _asientos(integration_session, organization_id)
        if a.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(consumos) == 1, "el reintento del comentario no cobra"
    assert consumos[0].amount_delta == -coste


async def test_el_reintento_del_comentario_propaga_el_error_del_proveedor_sin_envolver(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Si el proveedor sigue rechazando, sale su excepción **pelada**, no una de la plataforma.

    ## Por qué esto decide si el reintento existe o no

    Porque `autoretry_for` decide con el **tipo** de la excepción. Un `GitRateLimitError` —que es
    un «espera y vuelve a intentarlo»— envuelto en un `PRPipelineError`, que es cualquier otra
    cosa, se trataría como permanente: la tarea saldría roja a la primera y no habría segunda
    oportunidad. La envoltura sería, sin querer, la manera de desactivar el reintento.

    Se comprueba con `pytest.raises(GitRateLimitError)` a secas, no con un `match` sobre un
    `PRPipelineError`, porque lo que se afirma es el **tipo**, y eso es justo lo que lee Celery.
    """

    assert integration_session is not None
    _organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    review_id = review.id

    async def cliente_que_rechaza(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso(
            comentario_falla=GitRateLimitError(
                "El proveedor Git agotó su cuota temporal", status_code=429
            )
        )

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerFalso:
        del scan_mode, target_type
        return _ManagerFalso(run_id, target, workspace_root)

    async def cliente_que_escribe(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso()

    await _run_pr_security_pipeline(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=cliente_que_escribe,  # pyright: ignore[reportArgumentType]
        manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
        materializer=_materializador_que_prepara_el_workspace(),
        workspace_root=tmp_path,
        comment_dispatch=lambda _review_id: "tarea-1",
    )
    # A partir de aquí el comentario ya existe, así que el reintento lo **actualiza**, que es el
    # mismo camino que usa el pipeline cuando reanaliza una revisión que ya tenía comentario. El
    # rechazo se pone en el cliente del reintento, que es donde importa.

    with pytest.raises(GitRateLimitError):
        await reintentar_comentario_de_revision(
            str(review_id),
            session_provider=lambda: _sesion_de_la_prueba(integration_session),
            client_builder=cliente_que_rechaza,  # pyright: ignore[reportArgumentType]
        )


async def test_una_revision_sin_nada_que_publicar_no_es_un_error(
    integration_session: AsyncSession,
) -> None:
    """Una revisión en `QUEUED` no tiene veredicto que comentar, y eso devuelve `SKIPPED`.

    ## Por qué hace falta y por qué `SKIPPED` y no una excepción

    Porque la tarea es idempotente y a veces se la llama cuando no hay nada que hacer: una revisión
    que no llegó a escanear, una que no bloquea el merge, una que ya tiene su comentario. Convertir
    eso en una tarea roja obliga a alguien a mirar el registro del worker para descubrir que no
    pasó nada, y lo que se busca con esta prueba es justo lo contrario: que un «no había nada» sea
    distinguible de un fallo a simple vista.
    """

    assert integration_session is not None
    _organization, _repository, review = await _revision_para_el_pipeline(integration_session)
    review_id = review.id

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteFalso:
        return _ClienteFalso()

    resultado = await reintentar_comentario_de_revision(
        str(review_id),
        session_provider=lambda: _sesion_de_la_prueba(integration_session),
        client_builder=client_builder,  # pyright: ignore[reportArgumentType]
    )
    assert resultado == "SKIPPED"


def test_la_tarea_del_comentario_esta_registrada_y_reintenta() -> None:
    """La función sin tarea no reintenta nada: hace falta el nombre y su política.

    ## Por qué se comprueba el registro y no el comportamiento del planificador

    Porque el fallo «la función existe y nadie la llama» es el que se cuela: la función se escribe,
    sus pruebas pasan en verde y el reintento no ocurre nunca. Aquí se mira que el nombre existe en
    `celery_app.tasks` —que es lo que hace que un worker la pueda recibir por nombre— y que su
    `autoretry_for` incluye los fallos transitorios del proveedor. Es lo que convierte un
    `logger.warning` en un reintento de verdad.
    """

    from backend.workers.celery_app import celery_app

    assert "repositories.publish_pr_review_comment" in celery_app.tasks
    tarea = celery_app.tasks["repositories.publish_pr_review_comment"]
    assert GitRateLimitError in tarea.autoretry_for
    assert tarea.max_retries >= 1
    assert tarea.retry_backoff is True
