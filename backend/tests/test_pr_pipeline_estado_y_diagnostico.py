"""El pipeline de revisión de pull request: el badge que tumbaba el escaneo y el motivo perdido.

## Los dos defectos que este fichero existe para no dejar volver

**El primero es una asimetría de orden.** La publicación del estado del commit del pull request
era la **primera** instrucción del pipeline, antes de `manager_factory`, de `setup_workspace` y de
`manager.run`. Su error se traducía en `PRPipelineError` y salía por el `except Exception` del
pipeline. Con un repositorio real el `404` no aparece y no se nota nada; cuando aparece, lo que se
rompe es **el escaneo entero**, sin haber intentado escanear:

    pipeline.py:636  await _publish_status(...)
    github.py:206    set_commit_status
    httpx: 404 Not Found for url
      'https://api.github.com/repos/acme/auth-service/statuses/democommit0001'
    → GitClientError: El proveedor Git rechazó la operación

El paso que puede fallar es el que menos importa, y el que importa puede no llegar a intentarse
nunca. Eso está al revés, y es lo que estas pruebas comprueban.

**El segundo es la pérdida del motivo.** `tasks.py` clasifica el fallo con `diagnosticar_fallo` y
`pipeline.py` escribía el literal `PR_PIPELINE_FAILED`. La misma línea para un host sin el cerco de
salida que se niega a lanzar el sandbox, para un `DockerException` y para un bug de verdad, que es
exactamente la razón por la que existe `diagnostico.py`.

## Por qué este fichero tiene su propio andamiaje y no importa el de `test_pr_pipeline.py`

Porque hay más de un agente trabajando en este árbol y los ficheros de pruebas se tocan entre
ellos. Un `from backend.tests.test_pr_pipeline import _create_pipeline_review` convierte el fichero
ajeno en una dependencia de este, y el día que alguien lo reformatee, esto falla por un motivo que
no tiene nada que ver con lo que mide. Los datos de prueba se duplican a propósito.
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

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanStatusEnum
from backend.apps.repositories.clients.base import GitClientError
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.pipeline import PRPipelineError, _run_pr_security_pipeline
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.core.crypto import encrypt_secret
from backend.workers.runner.diagnostico import diagnosticar_fallo
from backend.workers.runner.egress_fence import EgressFenceMissingError
from backend.workers.runner.exceptions import SandboxExecutionError

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Andamiaje
# --------------------------------------------------------------------------- #


async def _revision_para_pipeline(
    session: AsyncSession,
) -> tuple[Repository, PullRequestReview, GitCredential]:
    """Organización con saldo, repositorio, credencial y una revisión en `QUEUED`.

    El saldo importa: `_claim_review` cobra un escaneo `QUICK` desde el webhook, que es el segundo
    punto de entrada a un escaneo. Una organización de prueba sin saldo ni siquiera llega a
    lanzarse, y eso es el comportamiento correcto, no un accidente de la prueba.
    """

    sufijo = uuid.uuid4().hex
    organization = Organization(
        name=f"Pipeline {sufijo}",
        slug=f"pipeline-{sufijo}",
        credit_balance=Decimal("1000"),
    )
    session.add(organization)
    await session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    session.add(repository)
    await session.flush()
    credential = GitCredential(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        encrypted_access_token=encrypt_secret(
            "github-token",
            organization_id=str(organization.id),
            provider=GitProviderEnum.GITHUB.value,
        ),
    )
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=17,
        pr_title="Cambio",
        pr_author="alice",
        source_branch="feature/cambio",
        target_branch="main",
        commit_sha="b" * 40,
        base_sha="f" * 40,
        head_clone_url="https://github.com/acme/app.git",
    )
    session.add_all([credential, review])
    await session.commit()
    return repository, review, credential


class _ClienteQueRechazaElBadge:
    """Un cliente Git cuyo único defecto es el que se está probando.

    `set_commit_status` lanza exactamente lo que lanzaba en el incidente: un `GitClientError` con
    `status_code=404`, que es lo que produce `BaseGitClient._raise_http_error` cuando el
    proveedor responde «no existe», y con el mensaje ya saneado. No se finge ningún otro defecto,
    porque una prueba que falla por otra causa no mide lo que dice medir.
    """

    def __init__(self) -> None:
        self.intentos_de_badge: list[str] = []
        self.comentarios: list[str] = []

    def set_commit_status(
        self,
        _repo_full_name: str,
        _sha: str,
        state: str,
        _description: str,
        _target_url: str,
    ) -> None:
        self.intentos_de_badge.append(state)
        raise GitClientError("El proveedor Git rechazó la operación", status_code=404)

    def post_pr_comment(self, _repo_full_name: str, _pr_number: int, body: str) -> str:
        self.comentarios.append(body)
        return "comment-404"

    def update_pr_comment(self, _repo_full_name: str, _comment_id: str, _body: str) -> None:
        return None

    def close(self) -> None:
        return None


def _informe_con_un_critical() -> str:
    return json.dumps(
        {
            "status": "completed",
            "scan_id": "scan-pr-404",
            "findings": [
                {
                    "id": "finding-pr-404",
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


class _ManagerQueEncuentraUnCritical:
    """Un sandbox de mentira que devuelve un informe con un hallazgo `CRITICAL`.

    Lleva un contador de ejecuciones a propósito: la aserción que importa es que `run` se llamó,
    y un booleano dentro del propio doble sería menos claro que un número que se puede leer en
    la aserción y ver de un vistazo que fue una vez y no dos.
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

    def run(self, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["workspace_prepared"] is True
        self.ejecuciones += 1
        return SimpleNamespace(
            exit_code=0, output_json=_informe_con_un_critical(), container_id="container-404"
        )

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None


class _ManagerQueNoArranca:
    """Un sandbox de mentira que falla **antes** de devolver nada, como un host sin cerco."""

    def __init__(self, run_id: str, target: str, workspace_root: Path) -> None:
        self.run_id = run_id
        self.target = target
        self.workspace_root = workspace_root
        self.temp_dir: Path | None = None
        self.cleanup_pending = False

    def setup_workspace(self) -> Path:
        run_dir = self.workspace_root / self.run_id
        (run_dir / "workspace").mkdir(parents=True)
        self.temp_dir = run_dir
        return run_dir

    def run(self, **_kwargs: Any) -> SimpleNamespace:
        # El runner envuelve por diseño y el motivo real va en `__cause__`. Es la forma que
        # `diagnostico.py` documenta para explicar por qué se mira la cadena y no la excepción
        # de superficie, así que el doble tiene que montarla igual o no estaría probando nada.
        raise SandboxExecutionError("Falló la ejecución del sandbox Strix") from (
            EgressFenceMissingError("sin cerco de salida")
        )

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None


# --------------------------------------------------------------------------- #
# 1. El badge no tumba el escaneo
# --------------------------------------------------------------------------- #


async def test_un_badge_que_el_proveedor_rechaza_no_impide_escanear(
    integration_session: AsyncSession,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Con un 404 en el estado del commit, el análisis se ejecuta igual y se guarda.

    Se comprueba **que el escaneo corrió**, que es lo que no pasaba, y no solo que la función no
    lanzó. Un test que mirara únicamente «no se levantó `GitClientError`» pasaría con un escaneo
    que no se intentó: lo que hay que ver es el hallazgo persistido y el run `COMPLETED`.

    Y se comprueba la otra mitad del contrato: el fallo del badge **queda escrito**, con revisión,
    repositorio, estado y código HTTP. Un fallo silencioso en un paso cosmético es el que hace que
    un día nadie sepa por qué un pull request no tiene badge, y el riesgo real de tragárselo es
    acabar creyendo que el badge se publicó.
    """

    assert integration_session is not None
    _repository, review, _credential = await _revision_para_pipeline(integration_session)
    review_id = review.id
    cliente = _ClienteQueRechazaElBadge()
    managers: list[_ManagerQueEncuentraUnCritical] = []

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerQueEncuentraUnCritical:
        # El pipeline llama a la factoría **por palabras clave**, así que los nombres importan y
        # no se pueden marcar con un guion bajo. Se comprueban aquí y no en el doble para que un
        # cambio accidental en cómo se lanza el sandbox se vea en el log de esta prueba y no en
        # un `TypeError` medio enterrado en el `except Exception` del pipeline.
        assert scan_mode == "quick"
        assert target_type == "REPOSITORY"
        manager = _ManagerQueEncuentraUnCritical(run_id, target, workspace_root)
        managers.append(manager)
        return manager

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteQueRechazaElBadge:
        return cliente

    async def materializer(
        loaded_repository: Repository,
        loaded_review: PullRequestReview,
        target_dir: Path,
        *,
        credential: GitCredential,
    ) -> list[str]:
        # Igual que la factoría del sandbox, esta se llama por palabras clave y sus nombres
        # importan. El `credential` además se comprueba porque es lo que materializa el
        # workspace: sin credencial no hay clon, y un fallo ahí tampoco es un fallo del escaneo.
        assert loaded_review.id == review_id
        assert credential.organization_id == loaded_repository.organization_id
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "changed.py").write_text("print('changed')\n", encoding="utf-8")
        return ["changed.py"]

    @asynccontextmanager
    async def session_provider() -> AsyncIterator[AsyncSession]:
        yield integration_session

    with caplog.at_level("WARNING"):
        resultado = await _run_pr_security_pipeline(
            str(review_id),
            session_provider=session_provider,
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=materializer,
            workspace_root=tmp_path,
        )

    assert resultado == PRReviewStatusEnum.FAILED.value
    # El sandbox llegó a ejecutarse. Antes de este arreglo no se llamaba ni una vez, y esta línea
    # es la que lo demuestra.
    assert len(managers) == 1
    assert managers[0].ejecuciones == 1, "el sandbox no llegó a ejecutarse"

    guardada = await integration_session.get(PullRequestReview, review_id)
    assert guardada is not None
    assert guardada.status == PRReviewStatusEnum.FAILED
    assert guardada.merge_blocked is True
    assert guardada.finished_at is not None

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

    # El badge se intentó dos veces —el `pending` y el veredicto final— y las dos fallaron.
    # Ninguna hizo caer el escaneo, que es justo lo que se quería.
    assert cliente.intentos_de_badge == ["pending", "failure"]

    mensajes = [registro.getMessage() for registro in caplog.records]
    aviso_badge = next(
        mensaje
        for mensaje in mensajes
        if "No se pudo publicar el estado" in mensaje and "pending" in mensaje
    )
    assert str(review_id) in aviso_badge
    assert "acme/app" in aviso_badge
    assert "GitClientError" in aviso_badge
    # El código HTTP es lo que dice si fue un repositorio renombrado, un permiso o una cuota, y
    # es la razón de que el log lleve el dato y no solo el tipo de la excepción.
    assert "http=404" in aviso_badge


# --------------------------------------------------------------------------- #
# 2. El motivo que se guardaba era un literal
# --------------------------------------------------------------------------- #


async def test_el_pipeline_guarda_el_motivo_clasificado_y_no_un_literal(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Un host sin el cerco de salida se guarda como tal, no como «la revisión se rompió».

    La aserción mira `run.error_message`, que es la columna que la API devuelve al navegador y la
    que el panel traduce con `run.fallos.<código>`. Con `PR_PIPELINE_FAILED` esa traducción no
    existe —el literal no está en ningún `locales/`— y quien mira el panel no tiene nada
    accionable; con `STRIX_EGRESS_FENCE_MISSING` la tiene, porque `pentests.json` dice qué
    instalar y dónde.

    Y se comprueban las dos mitades del contrato, no solo una: el **código guardado** y el `status`
    del run. Un diagnóstico correcto con el estado mal puesto seguiría siendo un escaneo que no
    termina, y el cliente vería una revisión colgada.
    """

    assert integration_session is not None
    _repository, review, _credential = await _revision_para_pipeline(integration_session)
    review_id = review.id

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteQueRechazaElBadge:
        return _ClienteQueRechazaElBadge()

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerQueNoArranca:
        assert scan_mode == "quick"
        assert target_type == "REPOSITORY"
        return _ManagerQueNoArranca(run_id, target, workspace_root)

    async def materializer(
        loaded_repository: Repository,
        _review: PullRequestReview,
        _target_dir: Path,
        *,
        credential: GitCredential,
    ) -> list[str]:
        del loaded_repository, credential
        return []

    @asynccontextmanager
    async def session_provider() -> AsyncIterator[AsyncSession]:
        yield integration_session

    with pytest.raises(PRPipelineError):
        await _run_pr_security_pipeline(
            str(review_id),
            session_provider=session_provider,
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=materializer,
            workspace_root=tmp_path,
        )

    guardada = await integration_session.get(PullRequestReview, review_id)
    assert guardada is not None
    assert guardada.status == PRReviewStatusEnum.ERROR
    assert guardada.run_id is not None
    assert guardada.finished_at is not None

    run = await integration_session.get(PentestRun, guardada.run_id)
    assert run is not None
    assert run.status == ScanStatusEnum.FAILED
    assert run.error_message == "STRIX_EGRESS_FENCE_MISSING"
    # La aserción es sobre el **valor guardado**, no contra una constante: así el test sigue
    # midiendo aunque alguien vuelva a escribir el literal en otro sitio.
    assert run.error_message != "PR_PIPELINE_FAILED"


async def test_el_diagnostico_del_pipeline_es_el_mismo_que_el_del_worker(
    integration_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Las dos rutas de ejecución clasifican igual, y por construcción y no por casualidad.

    Se ejecuta el mismo fallo —un `EgressFenceMissingError` envuelto por el runner— por el
    pipeline y se compara el código guardado con el que produce el clasificador. `tasks.py` ya
    usaba `diagnosticar_fallo`; el pipeline escribía un literal. Dos listas de motivos divergen: el
    día que se añadiese uno al diagnóstico, la mitad de los fallos de despliegue volvería a
    cobrar al cliente sin que nadie lo notara.

    Que se comparen los **dos valores** y no uno contra una constante es lo que hace que la
    prueba detecte la regresión si alguien reintrodujera el literal en `pipeline.py`.
    """

    assert integration_session is not None
    _repository, review, _credential = await _revision_para_pipeline(integration_session)
    review_id = review.id

    async def client_builder(
        _session: AsyncSession, _repository: Repository
    ) -> _ClienteQueRechazaElBadge:
        return _ClienteQueRechazaElBadge()

    def manager_factory(
        run_id: str, target: str, scan_mode: str, target_type: str, workspace_root: Path
    ) -> _ManagerQueNoArranca:
        assert scan_mode == "quick"
        assert target_type == "REPOSITORY"
        return _ManagerQueNoArranca(run_id, target, workspace_root)

    async def materializer(
        loaded_repository: Repository,
        _review: PullRequestReview,
        _target_dir: Path,
        *,
        credential: GitCredential,
    ) -> list[str]:
        del loaded_repository, credential
        return []

    @asynccontextmanager
    async def session_provider() -> AsyncIterator[AsyncSession]:
        yield integration_session

    with pytest.raises(PRPipelineError):
        await _run_pr_security_pipeline(
            str(review_id),
            session_provider=session_provider,
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
            manager_factory=manager_factory,  # pyright: ignore[reportArgumentType]
            materializer=materializer,
            workspace_root=tmp_path,
        )

    guardada = await integration_session.get(PullRequestReview, review_id)
    assert guardada is not None and guardada.run_id is not None
    run = await integration_session.get(PentestRun, guardada.run_id)
    assert run is not None

    # Lo que dice el clasificador, no lo que escribió nadie a mano.
    esperado = diagnosticar_fallo(_como_la_entrega_el_runner()).codigo

    assert run.error_message == esperado
    # Y la segunda mitad de la comparación: si el pipeline volviera a su literal, el `esperado`
    # seguiría siendo el código clasificado y la aserción de arriba caería. Por eso las dos
    # líneas van juntas y no basta con comprobar el valor contra una constante.
    assert esperado == "STRIX_EGRESS_FENCE_MISSING"


def _como_la_entrega_el_runner() -> BaseException:
    """La forma en que el runner entrega el motivo: el envoltorio con la causa encadenada.

    El runner agrupa por diseño —para que su `finally` pueda limpiar— y deja el motivo real en
    `__cause__`. Por eso `diagnostico.py` recorre la cadena al revés. Esta función reproduce esa
    forma exacta en lugar de lanzar el motivo desnudo, que es lo que ocurriría si la prueba
    clasificara mal por construcción del doble y no por el código.
    """

    try:
        raise SandboxExecutionError("Falló la ejecución del sandbox Strix") from (
            EgressFenceMissingError("sin cerco de salida")
        )
    except SandboxExecutionError as envuelto:
        return envuelto
