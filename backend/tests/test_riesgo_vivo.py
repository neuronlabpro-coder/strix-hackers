"""La misma pregunta sobre hallazgos tiene una sola respuesta en todo el panel.

## Qué se está probando y por qué estas pruebas existen

El 4 de octubre, un escaneo con cuatro hallazgos —`CRITICAL` en `REMEDIATION_PROPOSED`,
`HIGH` y `MEDIUM` en `OPEN`, `INFO` en `IGNORED`— daba tres cifras distintas según dónde se
mirase:

| Lectura | Filtro | Cifra |
| :--- | :--- | :--- |
| Ficha del escaneo | `status != IGNORED` | 3, y «Crítico: 1» |
| Anillo del dashboard | `status IN (OPEN, IN_PROGRESS)` | 2, y «Crítico: 0» |
| Tabla de issues | sin filtro | 4 |

Las tres eran **técnicamente correctas**: cada una contestaba una pregunta distinta sin decirlo.
El defecto no era que el número fuera malo, era que no se sabía cuál se estaba mirando, y eso
en un panel de seguridad es peor que un número malo porque no se ve.

## Qué decide este fichero

Que la pregunta de **riesgo actual** tiene un solo criterio, y que ese criterio no está escrito
en tres sitios sino en uno. El criterio ya existía y estaba decidido: `ROADMAP.md` dice que
`REMEDIATION_PROPOSED` «sigue contando como abierto» porque un PR sin fusionar no arregla
nada, y `IssueStatusEnum.is_open_for_closure` lo codifica. Lo que faltaba era que las tres
pantallas lo leyeran.

## Por qué estas pruebas miran los dos endpoints y no una constante

Porque el defecto era de comportamiento, no de definición: las tres constantes eran correctas y
la divergencia estaba en quién las usaba. Una prueba que comprueba `ESTADOS_RIESGO_VIVO ==
(OPEN, IN_PROGRESS, REMEDIATION_PROPOSED)` pasa con el defecto entero puesto, porque el defecto
no era el valor sino que `pentests/router.py` seguía usando `!= IGNORED`. Estas pruebas cambian
el valor,leen las dos respuestas de HTTP y comparan.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.dashboard.service import ESTADOS_RIESGO_VIVO
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

#: Los cuatro estados del enum y si cuentan como riesgo vivo. La columna de la derecha **no** se
#: deduce de `ESTADOS_RIESGO_VIVO`: está escrita a mano, para que un cambio en el criterio se vea
#: como un fallo de la prueba y no como un cambio de definición que nadie nota.
RIESGO_POR_ESTADO: dict[IssueStatusEnum, bool] = {
    IssueStatusEnum.OPEN: True,
    IssueStatusEnum.IN_PROGRESS: True,
    # Un PR abierto no es un hallazgo arreglado: se puede cerrar sin fusionar, y fusionarse sin
    # arreglar nada. Es el estado que separaba las dos cifras.
    IssueStatusEnum.REMEDIATION_PROPOSED: True,
    IssueStatusEnum.FIXED: False,
    IssueStatusEnum.SNOOZED: False,
    IssueStatusEnum.IGNORED: False,
}


async def _tenant_con_escaneo(session: AsyncSession) -> tuple[dict[str, str], PentestRun]:
    """Un tenant con un run completado, listo para colgarle hallazgos."""

    sufijo = uuid.uuid4().hex
    organization = Organization(name=f"Riesgo {sufijo}", slug=f"riesgo-{sufijo}")
    user = User(
        email=f"riesgo-{sufijo}@example.com",
        hashed_password="not-used",
        full_name=" Riesgo Admin",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier="acme/app#PR-1",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.COMPLETED,
    )
    session.add(run)
    await session.flush()
    cabeceras = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    return cabeceras, run


def _hallazgo(
    organization_id: uuid.UUID,
    run_id: uuid.UUID,
    severity: SeverityEnum,
    status: IssueStatusEnum,
    indice: int,
) -> Vulnerability:
    return Vulnerability(
        organization_id=organization_id,
        run_id=run_id,
        source_finding_id=f"rv-{indice}-{severity.value}-{status.value}",
        title=f"Hallazgo {indice}",
        description="Descripcion del hallazgo",
        severity=severity,
        cvss_score=7.5,
        affected_target="acme/app#PR-1",
        poc_reproduction_raw="curl https://example.com",
        status=status,
        discovered_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_el_criterio_de_riesgo_vivo_es_el_del_enum_y_no_una_lista_nueva(
    integration_session: AsyncSession,
) -> None:
    """El conjunto que usan las pantallas sale de `is_open_for_closure`, no de una copia.

    ## Por qué esta prueba y no solo leer el código

    Porque la tentación natural cuando dos sitios discrepan es escribir la lista correcta en cada
    uno. Funciona, pasa el gate, y en seis meses vuelve a haber tres listas. Lo que ata el
    criterio al enum es que **añadir un estado nuevo obliga a decidir**: si se añade un estado,
    esta prueba falla si el conjunto no lo incluye, y ese fallo es la pregunta «¿esto es riesgo
    vivo?». Con listas propias, añadir un estado no rompe nada y aparece un hallazgo que nadie
    cuenta.
    """

    assert integration_session is not None
    esperados = tuple(estado for estado, cuenta in RIESGO_POR_ESTADO.items() if cuenta)

    assert ESTADOS_RIESGO_VIVO == esperados

    # Y el enum no se ha movido por debajo: el conjunto no es una constante decorativa.
    for estado, cuenta in RIESGO_POR_ESTADO.items():
        assert estado.is_open_for_closure is cuenta, estado.value


@pytest.mark.asyncio
async def test_la_ficha_y_el_anillo_dicen_la_misma_cosa_sobre_los_mismos_hallazgos(
    integration_session: AsyncSession,
) -> None:
    """El caso del informe: un `REMEDIATION_PROPOSED` que el anillo no contaba.

    ## Por qué los dos endpoints en la misma prueba

    Porque el defecto era **entre** ellos. Con dos pruebas separadas, cada una mira su endpoint y
    las dos pueden estar bien por separado mientras el conjunto no coincide: que es exactamente
    lo que pasaba. Comparar las dos respuestas de la misma base, con los mismos cuatro hallazgos,
    es lo que convierte «dos números» en «una pregunta con dos respuestas».

    ## Por qué se siembran también los estados que no cuentan

    Porque el filtro antiguo `!= IGNORED` se comía un `SNOOZED` y contaba un `FIXED`. Sin filas
    de esos dos estados, la prueba pasaría con el defecto puesto en una de las dos mitades, y la
    mitad que no se midió seguiría rota sin que nadie lo supiera.
    """

    assert integration_session is not None
    cabeceras, run = await _tenant_con_escaneo(session=integration_session)
    hallazgos = [
        _hallazgo(
            run.organization_id,
            run.id,
            SeverityEnum.CRITICAL,
            IssueStatusEnum.REMEDIATION_PROPOSED,
            1,
        ),
        _hallazgo(run.organization_id, run.id, SeverityEnum.HIGH, IssueStatusEnum.OPEN, 2),
        _hallazgo(run.organization_id, run.id, SeverityEnum.MEDIUM, IssueStatusEnum.OPEN, 3),
        _hallazgo(run.organization_id, run.id, SeverityEnum.LOW, IssueStatusEnum.FIXED, 4),
        _hallazgo(run.organization_id, run.id, SeverityEnum.INFO, IssueStatusEnum.SNOOZED, 5),
        _hallazgo(run.organization_id, run.id, SeverityEnum.LOW, IssueStatusEnum.IGNORED, 6),
    ]
    integration_session.add_all(hallazgos)
    await integration_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        resumen = await cliente.get("/api/v1/dashboard/summary", headers=cabeceras)
        ficha = await cliente.get(
            f"/api/v1/pentests/{run.id}/findings", headers=cabeceras
        )

    assert resumen.status_code == 200
    assert ficha.status_code == 200
    cuerpo_dashboard = resumen.json()
    cuerpo_ficha = ficha.json()

    severidad_dashboard = {
        item["severity"]: item["total"] for item in cuerpo_dashboard["severity_distribution"]
    }
    severidad_ficha = {
        item["severity"]: item["total"] for item in cuerpo_ficha["severity_distribution"]
    }

    # El caso del informe, medido: el `CRITICAL` en `REMEDIATION_PROPOSED` cuenta en los dos.
    assert severidad_dashboard["CRITICAL"] == 1
    assert severidad_ficha["CRITICAL"] == 1
    # Y las dos mitades dicen lo mismo, que es lo que se estaba comprobando.
    assert severidad_dashboard == severidad_ficha

    # Los tres que no son riesgo vivo no cuentan en ninguno de los dos sitios. El `FIXED` es el
    # que el filtro `!= IGNORED` habría contado, y el `SNOOZED` el que habría dejado fuera sin
    # querer al cambiar a `in_`.
    assert severidad_dashboard["LOW"] == 0
    assert severidad_dashboard["INFO"] == 0
    assert cuerpo_dashboard["open_issues"] == 3
    assert cuerpo_ficha["total"] == 3

    # Y el histórico sigue completo en el sitio que tiene que enseñarlo entero: el inventario.
    assert cuerpo_dashboard["total_issues"] == 6
    estados = {item["status"]: item["total"] for item in cuerpo_dashboard["status_distribution"]}
    assert estados == {
        "OPEN": 2,
        "IN_PROGRESS": 0,
        "REMEDIATION_PROPOSED": 1,
        "FIXED": 1,
        "SNOOZED": 1,
        "IGNORED": 1,
    }


@pytest.mark.asyncio
async def test_el_listado_de_ejecuciones_no_cuenta_hallazgos_corregidos(
    integration_session: AsyncSession,
) -> None:
    """La columna `findings` del listado usa el mismo criterio que la ficha.

    ## Por qué esta fila del listado y no solo el desglose

    Porque `_findings_by_run` tenía **su propio** filtro, aparte del de `get_pentest_findings`.
    Arreglar uno y no el otro deja la pantalla diciendo «3 hallazgos» arriba y «1» abajo, que es
    la misma clase de contradicción y en la misma pantalla. Y era además el único punto donde
    la consulta a `vulnerabilities` no filtraba por organización.
    """

    assert integration_session is not None
    cabeceras, run = await _tenant_con_escaneo(session=integration_session)
    integration_session.add_all(
        [
            _hallazgo(
                run.organization_id,
                run.id,
                SeverityEnum.CRITICAL,
                IssueStatusEnum.REMEDIATION_PROPOSED,
                1,
            ),
            _hallazgo(
                run.organization_id, run.id, SeverityEnum.HIGH, IssueStatusEnum.FIXED, 2
            ),
            _hallazgo(
                run.organization_id, run.id, SeverityEnum.MEDIUM, IssueStatusEnum.IGNORED, 3
            ),
        ]
    )
    await integration_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        listado = await cliente.get("/api/v1/pentests/", headers=cabeceras)

    assert listado.status_code == 200
    filas = {item["id"]: item for item in listado.json()["items"]}
    assert filas[str(run.id)]["findings"] == 1
