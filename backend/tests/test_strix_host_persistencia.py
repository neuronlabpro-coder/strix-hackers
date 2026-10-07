"""Los artefactos del motor, persistidos: qué acaba en `pentest_runs` y qué en `vulnerabilities`.

## Qué se demuestra aquí

La parte que el parser no cubre: que el estado real del run y sus hallazgos llegan a la base con
el tenant correcto, y que un escaneo **sin hallazgos** deja el run en `COMPLETED` con su
cobertura guardada en lugar de fallar.

Las dos cosas que un escaneo limpio tiene que dejar son distintas y las dos se comprueban:

- `status = COMPLETED` con `finished_at` del motor: el escaneo ocurrió.
- `coverage` con `surfaces_reviewed: 18` y un hueco: el escaneo tuvo límites.

Sin la segunda, un run con huecos es indistinguible de uno limpio, y el panel no puede decir la
diferencia entre «no había nada» y «no pude mirar esto».

## R3

Todas las lecturas y escrituras filtran por `organization_id`, y hay una prueba que lo comprueba
con dos tenants reales: escribir los artefactos de un run del tenant A no puede crear ni tocar
una fila del tenant B.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.pentests.schemas import PentestRunResponse
from backend.apps.vulnerabilities.models import Vulnerability
from backend.workers.runner.strix_artefactos import leer_ejecucion
from backend.workers.tasks import StrixIngestionError, _persistir_artefactos_del_motor

FIXTURES = Path(__file__).parent / "fixtures" / "strix_run_referencia"


async def crear_tenant(session: AsyncSession) -> Organization:
    sufijo = uuid.uuid4().hex
    organization = Organization(
        name=f"HostMode {sufijo}",
        slug=f"host-mode-{sufijo}",
        credit_balance=Decimal("1000"),
    )
    session.add(organization)
    await session.flush()
    await session.commit()
    return organization


async def crear_run(session: AsyncSession, organization: Organization) -> PentestRun:
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="mindguard.site",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.RUNNING,
    )
    session.add(run)
    await session.flush()
    await session.commit()
    return run


def ejecucion_de_referencia(tmp_path: Path) -> Any:
    destino = tmp_path / "strix_runs" / "mindguard-site_23ee"
    destino.mkdir(parents=True, exist_ok=True)
    for fichero in ("run.json", "findings.sarif", "coverage.json"):
        shutil.copyfile(FIXTURES / fichero, destino / fichero)
    return leer_ejecucion(tmp_path)


def _sarif_con_un_hallazgo(cvss: float, titulo: str = "SQL injection en el login") -> str:
    """Un SARIF mínimo con **un** hallazgo real y ninguna fila de cobertura.

    Existe porque el run de referencia no encontró nada, y porque lo que hay que comprobar aquí es
    el camino de escritura —tenant, R4, idempotencia—, no el criterio de descubrimiento, que ya
    está probado con los artefactos reales en `test_strix_artefactos.py`.
    """

    return json.dumps(
        {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "Strix",
                            "rules": [
                                {"id": "CWE-89", "shortDescription": {"text": titulo}}
                            ],
                        }
                    },
                    "results": [
                        {
                            "ruleId": "CWE-89",
                            "kind": "fail",
                            "level": "error",
                            "message": {"text": f"{titulo}\n\nEl id va concatenado."},
                            "locations": [
                                {
                                    "logicalLocations": [
                                        {"fullyQualifiedName": "POST /login", "kind": "endpoint"}
                                    ]
                                }
                            ],
                            "properties": {
                                "security-severity": str(cvss),
                                "strix": {
                                    "id": "vuln-0001",
                                    "severity": "high",
                                    "cvss": cvss,
                                    "target": "https://mindguard.site/login",
                                    "impact": "Se puede leer la tabla de credenciales.",
                                    "remediation_steps": "Parametrizar la consulta.",
                                },
                            },
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_un_escaneo_sin_hallazgos_completa_el_run_y_guarda_su_cobertura(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Cero hallazgos es un resultado: `COMPLETED`, con cobertura y sin filas de hallazgo."""

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)
    ejecucion = ejecucion_de_referencia(tmp_path)

    assert ejecucion.hallazgos == ()

    count = await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia="host-pid:4242",
    )
    await integration_session.refresh(run)

    assert count == 0
    assert run.status == ScanStatusEnum.COMPLETED
    assert run.source_scan_id == "mindguard-site_23ee"
    assert run.exit_code == "0"
    assert run.container_id == "host-pid:4242"
    assert run.started_at == ejecucion.start_time
    assert run.finished_at == ejecucion.end_time
    assert run.coverage is not None
    assert run.coverage["surfaces_reviewed"] == 18
    assert run.coverage["findings_filed"] == 0
    assert run.coverage["complete"] is True
    assert len(run.coverage["gaps"]) == 1
    assert run.coverage["gaps"][0]["surface"] == "https://mindguardredteam.com/contact.php"

    guardados = await integration_session.execute(
        select(Vulnerability).where(Vulnerability.run_id == run.id)
    )
    assert list(guardados.scalars().all()) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_los_hallazgos_reales_llegan_a_vulnerabilities_con_su_tenant(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Un hallazgo del SARIF se persiste con `organization_id` y `run_id` de su run.

    Se parte de un SARIF con **un** `kind:"fail"` porque el run de referencia no encontró nada, y
    porque lo que hay que comprobar aquí es el camino de escritura, no el criterio de
    descubrimiento —que ya está en `test_strix_artefactos.py` con los artefactos reales.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    destino = tmp_path / "strix_runs" / "mindguard-site_23ee"
    destino.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "run.json", destino / "run.json")
    shutil.copyfile(FIXTURES / "coverage.json", destino / "coverage.json")
    (destino / "findings.sarif").write_text(_sarif_con_un_hallazgo(8.1), encoding="utf-8")
    ejecucion = leer_ejecucion(tmp_path)
    assert len(ejecucion.hallazgos) == 1

    count = await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia="host-pid:4242",
    )
    await integration_session.refresh(run)

    assert count == 1
    assert run.status == ScanStatusEnum.COMPLETED

    resultado = await integration_session.execute(
        select(Vulnerability).where(
            Vulnerability.run_id == run.id,
            Vulnerability.organization_id == organization.id,
        )
    )
    hallazgo = resultado.scalar_one()
    assert hallazgo.source_finding_id == "vuln-0001"
    assert hallazgo.title == "SQL injection en el login"
    assert hallazgo.affected_target == "https://mindguard.site/login"
    assert hallazgo.cvss_score == pytest.approx(8.1)
    assert hallazgo.organization_id == organization.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reinger_los_mismos_artefactos_no_reescribe_ni_falla(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """La reingesta con el mismo contenido es idempotente, y con distinto contenido es un error.

    Es el caso de un reintento del worker y el de una doble entrega de Celery: en los dos,
    reescribir `vulnerabilities` violaría R4 sin aportar nada. Y si el contenido **cambia**, hay
    alguien mirando: se lanza, porque dos escaneos distintos sobre el mismo run no se pueden
    fusionar en silencio.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)
    ejecucion = ejecucion_de_referencia(tmp_path)

    await _persistir_artefactos_del_motor(
        integration_session, run, ejecucion, exit_code="0", referencia="host-pid:1"
    )
    otra_vez = await _persistir_artefactos_del_motor(
        integration_session, run, ejecucion, exit_code="0", referencia="host-pid:1"
    )
    assert otra_vez == 0

    # Un contenido distinto se construye volviendo a leer el directorio con otro `run.json`: el
    # mismo `run_id` no vale, porque el motor genera el nombre y el identificador es lo que ata el
    # artefacto a un run. Si el identificador cambia, el run ya ingerido y este no son el mismo
    # escaneo, y quien lo llama tiene que enterarse.
    destino = tmp_path / "strix_runs" / "mindguard-site_23ee"
    (destino / "run.json").write_text(
        '{"status":"completed","run_id":"otro-run"}', encoding="utf-8"
    )
    con_otro_id = leer_ejecucion(tmp_path)

    with pytest.raises(StrixIngestionError, match="no coincide"):
        await _persistir_artefactos_del_motor(
            integration_session, run, con_otro_id, exit_code="0", referencia="host-pid:1"
        )


@pytest.mark.integration
@pytest.mark.integration
@pytest.mark.asyncio
async def test_los_agentes_del_motor_llegan_hasta_la_respuesta_de_la_api(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Lo que `coverage.json` publica como `agents[]` sale en la respuesta de la API.

    ## Por qué esta prueba existe: el campo se perdía en el borde

    `CoberturaStrix.a_json()` escribía `agents` en el JSONB y el esquema `ScanCoverage` **no lo
    declaraba**. Pydantic descarta lo que el modelo no declara, así que la información llegaba a la
    base, se guardaba y se perdía al serializar: un campo que parece funcionar y nunca se ve. El
    run de referencia tiene tres agentes `completed` y ninguno llegaba al panel.

    Es la clase de fallo más difícil de ver que hay: no da error, no da `500` y el endpoint
    responde `200`. Solo falta un dato, y nada avisa.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)
    ejecucion = ejecucion_de_referencia(tmp_path)

    await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia="replay:prueba",
    )
    await integration_session.refresh(run)

    assert ejecucion.cobertura is not None
    # El run real trae **cuatro** agentes en `machine_observed.agents`: el `Root Agent` y sus tres
    # subagentes. El encargo hablaba de tres, que son los subagentes; la cifra real del artefacto
    # son cuatro y es la que se afirma, porque un número escrito a mano es un número que se
    # desactualiza sin que nadie lo note.
    assert len(ejecucion.cobertura.agentes) == 4, "el run real trae cuatro agentes en coverage.json"
    assert run.coverage is not None and len(run.coverage["agents"]) == 4

    respuesta = PentestRunResponse.desde_run(run)
    assert respuesta.coverage is not None
    assert len(respuesta.coverage.agents) == 4
    assert {agente.status for agente in respuesta.coverage.agents} == {"completed"}
    # El nombre del agente es un valor del motor: se devuelve tal cual, sin traducir ni normalizar.
    assert all(agente.agent_name for agente in respuesta.coverage.agents)
    assert any(agente.agent_name == "Root Agent" for agente in respuesta.coverage.agents)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_los_artefactos_de_un_run_no_pueden_tocar_el_de_otro_tenant(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """R3: la escritura va por `organization_id` y el identificador del run no la salta.

    Se leen los hallazgos del run del tenant B con el `organization_id` del tenant A. El `WHERE`
    tiene los dos, así que la fila de B no aparece: la función no puede tocar lo que no lee.
    """

    assert integration_session is not None
    organization_a = await crear_tenant(integration_session)
    organization_b = await crear_tenant(integration_session)
    run_b = await crear_run(integration_session, organization_b)
    run_a = await crear_run(integration_session, organization_a)
    ejecucion = ejecucion_de_referencia(tmp_path)

    resultado = await integration_session.execute(
        select(PentestRun).where(
            PentestRun.id == run_a.id,
            PentestRun.organization_id == organization_a.id,
        )
    )
    fila_a = resultado.scalar_one()
    assert fila_a.id != run_b.id

    await _persistir_artefactos_del_motor(
        integration_session, fila_a, ejecucion, exit_code="0", referencia="host-pid:7"
    )

    resultado_b = await integration_session.execute(
        select(PentestRun).where(
            PentestRun.id == run_b.id,
            PentestRun.organization_id == organization_b.id,
        )
    )
    fila_b = resultado_b.scalar_one()
    assert fila_b.status == ScanStatusEnum.RUNNING
    assert fila_b.source_scan_id is None
    assert fila_b.coverage is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_una_reingesta_con_evidencia_distinta_se_rechaza(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Mismos identificadores y **distinto** contenido es un error, no una actualización.

    R4 dice que la evidencia del escaneo es inmutable, y un `UPDATE` silencioso la reescribiría.
    La comparación es `_same_immutable_evidence`, la misma que usa el camino de ingesta del modo
    contenedor: un criterio, dos caminos.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    destino = tmp_path / "strix_runs" / "mindguard-site_23ee"
    destino.mkdir(parents=True)
    for fichero in ("run.json", "coverage.json"):
        shutil.copyfile(FIXTURES / fichero, destino / fichero)
    (destino / "findings.sarif").write_text(_sarif_con_un_hallazgo(8.1), encoding="utf-8")
    primero = leer_ejecucion(tmp_path)
    await _persistir_artefactos_del_motor(
        integration_session, run, primero, exit_code="0", referencia="host-pid:1"
    )

    (destino / "findings.sarif").write_text(
        _sarif_con_un_hallazgo(9.9, titulo="otro titulo"), encoding="utf-8"
    )
    segundo = leer_ejecucion(tmp_path)

    with pytest.raises(StrixIngestionError, match="no coinciden"):
        await _persistir_artefactos_del_motor(
            integration_session, run, segundo, exit_code="0", referencia="host-pid:1"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_el_run_no_se_puede_ingerir_tras_un_estado_terminal_distinto(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Un run en `FAILED` no se completa porque llegaran artefactos: eso sería tapar un fallo."""

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)
    run.status = ScanStatusEnum.FAILED
    run.finished_at = datetime.now(UTC)
    await integration_session.commit()
    await integration_session.refresh(run)

    ejecucion = ejecucion_de_referencia(tmp_path)

    with pytest.raises(StrixIngestionError, match="estado susceptible"):
        await _persistir_artefactos_del_motor(
            integration_session, run, ejecucion, exit_code="0", referencia="host-pid:1"
        )
