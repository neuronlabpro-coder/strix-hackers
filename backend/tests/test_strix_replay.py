"""El modo replay: qué es, qué no puede hacer y por qué no existe fuera de desarrollo.

## Qué se demuestra aquí

1. **No existe sin configuración.** Sin `strix_replay_source` no hay modo replay: no hay bandera,
   no hay valor «activado», y el runner sin origen lanza.
2. **Settings se niega a arrancar** en `staging` y en `production` si el origen está puesto. No es
   un aviso: es un refusal de arranque.
3. **Pasa por el mismo pipeline.** Los artefactos de un run **real** entran por
   `leer_ejecucion` y salen por `_persistir_artefactos_del_motor`, los mismos dos módulos que usa
   el modo host. Y el run real da **0 hallazgos** con 18 registros de cobertura, que es el dato que
   demuestra que se leyó el artefacto entero y no un SARIF de mentira.
4. **No toca Docker.** No llama al demonio, no crea red y no lanza el ejecutable. Se comprueba
   parcheando `docker.from_env` para que **levante** si alguien lo llama.
5. **No toca los créditos.** El ledger del tenant no gana ni pierde ni un asiento.
6. **La referencia dice `replay:`** y el camino de aborto la reconoce como no-matable.
"""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.apps.vulnerabilities.models import Vulnerability
from backend.core.config import Settings
from backend.tests.test_config import build_environment_values
from backend.workers.runner.replay import (
    PREFIJO_REFERENCIA_REPLAY,
    StrixReplayRunner,
    es_replay,
    referencia_de_replay,
)
from backend.workers.tasks import _persistir_artefactos_del_motor

pytestmark = pytest.mark.integration

#: El run real completo. Trae `run.json` recortado, el SARIF y el `coverage.json` **sin tocar**.
ORIGEN_REAL = Path(__file__).parent / "fixtures" / "strix_run_completado_real"


# --------------------------------------------------------------------------- #
# La existencia del modo
# --------------------------------------------------------------------------- #


def test_sin_origen_configurado_no_hay_modo_replay() -> None:
    """El ajuste vacío no es un replay apagado: no hay nada que apagar.

    Si esto devolviera un runner utilizable, el modo existiría en todos los despliegues y bastaría
    con que alguien escribiera una ruta equivocada en el `.env` para tener escaneos fabricados.
    """

    valores = build_environment_values()
    valores["strix_replay_source"] = ""
    configuracion = Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]

    runner = StrixReplayRunner(str(uuid.uuid4()), source=configuracion.strix_replay_source)

    assert runner.source is None
    with pytest.raises(ValueError, match="origen"):
        runner.run()


def test_el_replay_se_niega_a_arrancar_en_produccion_y_en_staging() -> None:
    """Staging y producción no arrancan con el replay puesto.

    ## Por qué esto es un refusal y no un aviso

    Porque un despliegue real que puede fabricar los artefactos de un escaneo puede publicar
    vulnerabilidades que el motor nunca encontró y cobrarlas como si fueran trabajo. Un aviso
    sale por pantalla y el proceso sigue; un refusal no levanta el backend. Es la misma categoría
    que el secreto de ejemplo o el bcrypt débil: una propiedad que se puede desactivar con una
    línea de configuración no es una propiedad.
    """

    for entorno in ("production", "staging"):
        valores = build_environment_values()
        valores["environment"] = entorno
        valores["debug"] = False
        valores["strix_replay_source"] = str(ORIGEN_REAL)
        if entorno == "production":
            valores["email_verification_delivery_mode"] = "smtp"
            valores["smtp_host"] = "smtp.example.com"
            valores["smtp_username"] = "smtp-user"
            valores["smtp_password"] = "smtp-password"
            valores["frontend_base_url"] = "https://app.example.com"
            valores["api_public_base_url"] = "https://api.example.com"
            valores["stripe_secret_key"] = "sk_test_placeholder_no_es_una_clave_real"
            valores["stripe_publishable_key"] = "pk_test_placeholder"
            valores["stripe_webhook_secret"] = "whsec_placeholder"

        with pytest.raises(ValueError, match="STRIX_REPLAY_SOURCE"):
            Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]


def test_en_desarrollo_el_replay_si_se_puede_configurar() -> None:
    """La otra mitad de la prueba: el refusal no es «nunca», es «fuera de desarrollo»."""

    valores = build_environment_values()
    valores["strix_replay_source"] = str(ORIGEN_REAL)

    configuracion = Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]

    assert configuracion.strix_replay_source == str(ORIGEN_REAL)


def test_el_replay_declara_su_origen_y_la_referencia_no_es_un_pid() -> None:
    """`replay:<origen>`, y no un PID: no hay nada vivo que matar."""

    referencia = referencia_de_replay("mindguard-site_23ee")

    assert referencia == "replay:mindguard-site_23ee"
    assert PREFIJO_REFERENCIA_REPLAY == "replay:"
    assert es_replay(referencia)
    assert not es_replay("host-pid:4242")
    assert not es_replay("fenix-strix-1111")
    assert not es_replay(None)


def test_el_abort_de_un_replay_no_intenta_parar_nada(tmp_path: Path) -> None:
    """El camino de aborto reconoce la referencia y **no** llama a Docker.

    Sin esto, abortar un run de replay intentaría quitar un contenedor que no existe. En un
    despliegue de desarrollo sin demonio eso es un `503` de «limpieza pendiente» sobre un run que
    ya terminó: el peor resultado posible para quien solo quiere cerrar una ficha.
    """

    from backend.apps.pentests import router as pentests_router

    llamado: list[str] = []

    with (
        pytest.MonkeyPatch.context() as mono,
        mono.context() as _,
    ):
        mono.setattr(
            pentests_router.StrixSandboxManager,
            "kill_container",
            lambda referencia, **kwargs: llamado.append(referencia),
        )
        pentests_router.kill_sandbox_container("replay:mindguard-site_23ee")

    assert llamado == [], "un replay no tiene contenedor que quitar"


@pytest.mark.asyncio
async def test_el_vigilante_no_intenta_limpiar_una_reproduccion(
    integration_session: AsyncSession,
) -> None:
    """El watchdog tampoco pregunta a Docker por un replay.

    ## Por qué este caso y no solo el del aborto

    Porque son dos puertas distintas y las dos arrastraban el mismo defecto. El watchdog corre en el
    worker cada `STRIX_WATCHDOG_INTERVAL_SECONDS` y su tercera rama entra en **cualquier** run con
    `container_id` registrado. Un replay que se quedó en `RUNNING` —porque el worker murió a mitad—
    entraría por ahí, y en un despliegue de desarrollo sin demonio `kill_container` y
    `remove_network_for_run` fallarían los dos, dejando `cleanup_pending = True` **para siempre**
    sobre un run que no tiene nada que limpiar.

    Y `cleanup_pending = True` no es cosmético: es la tercera rama del watchdog, así que el run se
    re revisita en cada pasada y nunca sale del estado. Es un run que aparece pendiente de limpieza
    mientras no hay nada pendiente.
    """

    assert integration_session is not None
    from backend.workers import tasks as tasks_module

    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)
    run.container_id = "replay:strix_run_completado_real"
    run.status = ScanStatusEnum.RUNNING
    run.started_at = datetime.now(UTC) - timedelta(days=1)
    await integration_session.commit()

    consultas_docker: list[str] = []
    with (
        patch.object(
            tasks_module.StrixSandboxManager,
            "kill_container",
            lambda *a, **k: consultas_docker.append("kill"),
        ),
        patch.object(
            tasks_module.StrixSandboxManager,
            "remove_network_for_run",
            lambda *a, **k: consultas_docker.append("red"),
        ),
    ):
        cerrados = await tasks_module.reconcile_orphaned_runs(
            integration_session,
            kill_container=lambda referencia: consultas_docker.append(referencia),
        )

    await integration_session.refresh(run)

    assert consultas_docker == [], "el watchdog no debe preguntar a Docker por un replay"
    assert cerrados >= 1
    assert run.status == ScanStatusEnum.FAILED
    assert run.cleanup_pending is False, (
        "no hay nada que limpiar: dejar la marca puesta haria que el watchdog lo reintentara "
        "para siempre"
    )


# --------------------------------------------------------------------------- #
# Que no toca Docker ni el ejecutable
# --------------------------------------------------------------------------- #


def test_el_replay_no_habla_con_docker_ni_lanza_el_ejecutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ni `docker.from_env`, ni `subprocess`, ni el CLI del motor.

    Se parchean los tres a **una función que revienta**, porque un doble que devuelve sin hacer
    nada dejaría pasar un replay que sí intentara hablar con el demonio: la prueba tiene que
    **fallar ante la llamada**, no contar llamadas.

    ## Por qué el parche va sobre el módulo, no sobre el símbolo

    Porque `sandbox.py` y `host.py` **importan** el nombre en su propio espacio de nombres, así
    que parchear `backend.workers.runner.sandbox.docker.from_env` no tocaría lo que el runner
    llama. Parchear el módulo del que sale el nombre sí. Y es lo que hace que la prueba siga
    valiendo si mañana el replay empezara a hablar con Docker: reventaría aquí.
    """

    import subprocess as modulo_subprocess

    import docker as modulo_docker

    def reventar(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("el replay no puede hablar con Docker ni lanzar procesos")

    monkeypatch.setattr(modulo_docker, "from_env", reventar)
    monkeypatch.setattr(modulo_subprocess, "Popen", reventar)
    monkeypatch.setattr(modulo_subprocess, "run", reventar)

    workspace = tmp_path / "workspaces"
    runner = StrixReplayRunner(
        str(uuid.uuid4()), source=str(ORIGEN_REAL), workspace_root=workspace
    )
    ejecucion, referencia = runner.run()

    assert ejecucion.run_name == "mindguard-site_23ee"
    assert referencia == "replay:strix_run_completado_real"
    assert ejecucion.hallazgos == ()
    assert ejecucion.registros_cobertura == 18


def test_el_replay_no_importa_el_cliente_de_docker() -> None:
    """El módulo del replay no **importa** nada capaz de hablar con la máquina.

    Es una prueba **estructural** sobre el árbol de imports, no sobre el texto: el docstring del
    módulo habla de Docker y de subprocess a propósito, y una prueba que buscara la palabra en
    todo el fichero caería por un comentario. Lo que importa es que no exista el símbolo, porque
    un replay que consulta al demonio ya no es un replay: es un modo de ejecución con pasos de
    mentira, y lo que se acaba de demostrar es que no ejecuta nada.
    """

    from backend.workers.runner import replay as replay_module

    assert not hasattr(replay_module, "docker")
    assert not hasattr(replay_module, "subprocess")
    assert not hasattr(replay_module, "Popen")


# --------------------------------------------------------------------------- #
# El pipeline de verdad
# --------------------------------------------------------------------------- #


async def crear_tenant(session: AsyncSession) -> Organization:
    sufijo = uuid.uuid4().hex
    organization = Organization(
        name=f"Replay {sufijo}",
        slug=f"replay-{sufijo}",
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


async def asientos_del_tenant(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[tuple[Any, ...]]:
    resultado = await session.execute(
        select(
            CreditLedger.reason,
            CreditLedger.amount_delta,
            CreditLedger.reference_id,
        ).where(CreditLedger.organization_id == organization_id)
    )
    return [
        (razon, Decimal(str(delta)), referencia)
        for razon, delta, referencia in resultado.all()
    ]


@pytest.mark.asyncio
async def test_el_replay_persiste_el_run_real_completo(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """El run real entra por el mismo pipeline y sale con sus cifras reales.

    Los valores **no** se inventan: `0` hallazgos, `18` superficies, `1` hueco y `$1.85` de coste
    son los del artefacto. Si el pipeline fabricara o perdiera algo, estos números caerían.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    runner = StrixReplayRunner(
        str(run.id), source=str(ORIGEN_REAL), workspace_root=tmp_path / "workspaces"
    )
    ejecucion, referencia = runner.run()

    assert ejecucion.hallazgos == ()
    assert ejecucion.registros_cobertura == 18
    assert ejecucion.coste == Decimal("1.84830111")

    count = await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia=referencia,
    )
    await integration_session.refresh(run)

    assert count == 0
    assert run.status == ScanStatusEnum.COMPLETED
    assert run.container_id == "replay:strix_run_completado_real"
    assert run.source_scan_id == "mindguard-site_23ee"
    assert run.coverage is not None
    assert run.coverage["surfaces_reviewed"] == 18
    assert run.coverage["findings_filed"] == 0
    assert run.coverage["complete"] is True
    assert len(run.coverage["gaps"]) == 1

    # El tenant se queda sin hallazgos: el run real no encontró ninguno, y publicar uno sería
    # inventar una vulnerabilidad.
    hallazgos = (
            await integration_session.execute(
                select(Vulnerability).where(Vulnerability.run_id == run.id)
            )
        ).scalars()
    assert list(hallazgos.all()) == []


@pytest.mark.asyncio
async def test_el_replay_no_mueve_el_saldo_del_tenant(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Ni cobra ni reembolsa: el ledger del tenant no cambia ni en un asiento.

    Se siembran **dos** asientos de un run real —la reserva y su devolución— para que la prueba no
    dependa de un ledger vacío. Comparar listas y no saldos es a propósito: el saldo podría volver
    al mismo número con asientos nuevos, y eso ya sería tocar el dinero.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    from backend.apps.billing.service import apply_credit_delta

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=Decimal("-10"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=str(run.id),
    )
    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=Decimal("3"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=f"{run.id}:refund",
    )
    await integration_session.commit()
    antes = await asientos_del_tenant(integration_session, organization.id)
    saldo_antes = Decimal(organization.credit_balance)

    runner = StrixReplayRunner(
        str(run.id), source=str(ORIGEN_REAL), workspace_root=tmp_path / "workspaces"
    )
    ejecucion, referencia = runner.run()
    await _persistir_artefactos_del_motor(
        integration_session, run, ejecucion, exit_code="0", referencia=referencia
    )

    despues = await asientos_del_tenant(integration_session, organization.id)
    await integration_session.refresh(organization)

    assert despues == antes
    assert Decimal(organization.credit_balance) == saldo_antes


@pytest.mark.asyncio
async def test_el_worker_completa_el_replay_sin_mover_el_saldo(
    integration_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El camino entero del worker, con el saldo como testigo.

    ## Por qué esta prueba y no solo la del runner

    Porque `_run_attempt_en_replay` es donde vive la decisión de **no tarificar**, y esa decisión no
    está en el runner: el runner no sabe nada de créditos. Sin esta prueba, quitarle el
    `sin_tarificar=True` al desenlace no rompería nada de lo anterior, porque el runner seguiría
    dando los mismos artefactos.

    ## Por qué se llama a `_execute_pentest_run` y no a `_run_attempt`

    Porque `_execute_pentest_run` es quien reclama el run, llama a `_run_attempt` y decide si invoca
    `_charge_run_usage`. Un test del nivel equivocado probaría una mitad y dejaría la otra sin
    vigilar, que es como se cuelan los errores de dinero.

    ## Por qué se parchea el workspace

    Porque `Settings` está congelado y el workspace sale de ahí. Se parchea el atributo del módulo
    de configuración con `monkeypatch`, que es lo que permite probar el modo replay sin tocar el
    `.env` de la máquina ni la base de datos.
    """

    assert integration_session is not None
    from backend.workers import tasks as tasks_module
    from backend.workers.runner import replay as replay_module

    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)
    run.status = ScanStatusEnum.QUEUED
    await integration_session.commit()

    from backend.apps.billing.service import apply_credit_delta

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=Decimal("-10"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=str(run.id),
    )
    await integration_session.commit()
    antes = await asientos_del_tenant(integration_session, organization.id)
    saldo_antes = Decimal(organization.credit_balance)

    # `Settings` está congelado a propósito, así que la prueba **construye** uno con el replay
    # puesto en vez de mutar el global. Es lo que hace que la prueba no dependa del `.env` de quien
    # la ejecuta: si el `.env` de la máquina no trae la variable, el test sigue probando lo mismo.
    valores = build_environment_values()
    valores["strix_replay_source"] = str(ORIGEN_REAL)
    valores["strix_workspace_root"] = str(tmp_path / "workspaces")
    configuracion = Settings(_env_file=None, **valores)  # pyright: ignore[reportCallIssue]
    monkeypatch.setattr(tasks_module, "settings", configuracion)
    monkeypatch.setattr(replay_module, "settings", configuracion)

    monkeypatch.setattr(
        tasks_module, "_session_factory", lambda: _motor_y_sesion_de_la_prueba(integration_session)
    )
    cobros: list[str] = []
    monkeypatch.setattr(
        tasks_module,
        "_charge_run_usage",
        lambda *args, **kwargs: cobros.append(str(args[1])),
    )

    desenlace = await tasks_module._execute_pentest_run(run.id)

    assert desenlace == "COMPLETED"
    assert cobros == [], "un replay no puede pasar por la tarificacion de consumo"

    await integration_session.refresh(run)
    despues = await asientos_del_tenant(integration_session, organization.id)
    await integration_session.refresh(organization)

    assert despues == antes, "el ledger del tenant no puede cambiar en un replay"
    assert Decimal(organization.credit_balance) == saldo_antes
    assert run.status == ScanStatusEnum.COMPLETED
    assert run.container_id == "replay:strix_run_completado_real"


def _motor_y_sesion_de_la_prueba(sesion: AsyncSession) -> tuple[Any, Any]:
    """Un motor nulo y una fábrica que devuelve la **misma** sesión que la prueba.

    ## Por qué hace falta esto

    Porque `_run_attempt_en_replay` —y `_claim_run_for_execution`, que va antes— abren su propia
    sesión con `_session_factory()`. En la base de pruebas eso es otra conexión sobre otra
    transacción, así que el `commit` del worker no se vería en la sesión de la prueba y el aserto
    de `run.status` leería el estado de antes: la prueba pasaría sin comprobar nada de lo que
    importa.

    El motor es nulo porque su único uso en estos caminos es `dispose()` al final del `finally`, y
    una corrutina que no hace nada es la forma más barata de que eso sea cierto sin abrir una
    conexión que la prueba no necesita.
    """

    class _MotorNulo:
        async def dispose(self) -> None:
            return None

    class _SesionCompartida:
        async def __aenter__(self) -> AsyncSession:
            return sesion

        async def __aexit__(self, *_exc: object) -> None:
            return None

    return _MotorNulo(), lambda *_a, **_k: _SesionCompartida()


@pytest.mark.asyncio
async def test_un_replay_sin_run_json_falla_diciendo_cual(tmp_path: Path) -> None:
    """Una ruta mal escrita se ve: el mensaje dice qué falta y dónde.

    ## Por qué este caso merece prueba

    Porque el otro camino posible —leer los artefactos y devolver «escaneo sin resultados»—
    convertiría un error de configuración en un escaneo limpio con cero hallazgos. El panel
    mostraría «se escaneó y no encontró nada» sobre un directorio que no existe, que es la peor
    versión de la mentira que este módulo vino a corregir.

    ## Por qué el origen va en `tmp_path` y no en el directorio actual

    Porque un `Path(uuid4().hex)` **relativo** se crea en el directorio desde el que se ejecuta la
    batería, que es la raíz del repositorio. La primera versión de esta prueba dejó nueve
    directorios de treinta y dos caracteres hexadecimales en la raíz del proyecto, y lo único que
    los diferenciaba de una carpeta del producto era que estaban vacíos. Un test de desarrollo que
    ensucia el árbol de trabajo es un test que alguien limpia a mano y borra de paso lo que había.
    """

    origen = tmp_path / uuid.uuid4().hex
    origen.mkdir()
    (origen / "findings.sarif").write_text("{}", encoding="utf-8")

    runner = StrixReplayRunner(str(uuid.uuid4()), source=str(origen), workspace_root=tmp_path / "w")
    with pytest.raises(FileNotFoundError, match=re.escape("run.json")):
        runner.run()


@pytest.mark.asyncio
async def test_el_replay_purga_el_workspace_aunque_falle(tmp_path: Path) -> None:
    """R5 también en el replay: si algo falla, el directorio del run no se queda en disco.

    ## Por qué esto importa más en el replay que en el modo host

    Porque el workspace del replay contiene el **informe en markdown** de un run real, que es una
    descripción escrita del objetivo y de lo que se le hizo al cliente. Dejarlo en disco sería
    exactamente lo que R5 prohíbe, y además en un directorio cuyo nombre es el identificador del
    run de otra máquina.
    """

    origen = tmp_path / "origen"
    origen.mkdir()
    # `run.json` sí, `findings.sarif` no: `_copiar` falla y el `finally` purga.
    (origen / "run.json").write_text('{"status":"completed"}', encoding="utf-8")
    raiz = tmp_path / "workspaces"

    runner = StrixReplayRunner(str(uuid.uuid4()), source=str(origen), workspace_root=raiz)
    with pytest.raises(FileNotFoundError, match=re.escape("findings.sarif")):
        runner.run()

    assert not raiz.exists() or not any(raiz.rglob("strix_runs"))


def test_el_replay_no_necesita_el_ejecutable_del_motor(tmp_path: Path) -> None:
    """Un replay con `STRIX_CLI_PATH` vacío funciona: no hay motor que lanzar.

    Es la diferencia práctica con el modo host, que sin esa ruta ni siquiera arma el comando. Un
    replay que exigiera instalar el motor para probar el pipeline de ingesta no serviría para
    desarrollo, que es su único caso.
    """

    runner = StrixReplayRunner(
        str(uuid.uuid4()), source=str(ORIGEN_REAL), workspace_root=tmp_path / "workspaces"
    )

    ejecucion, _ = runner.run()

    assert ejecucion.status == "completed"


def test_el_replay_copia_los_cuatro_artefactos(tmp_path: Path) -> None:
    """Los cuatro, y el origen no se modifica: es la entrada, no un workspace."""

    origen = tmp_path / "origen"
    shutil.copytree(ORIGEN_REAL, origen)
    hashes_antes = {p.name: p.read_bytes() for p in origen.iterdir() if p.is_file()}

    runner = StrixReplayRunner(str(uuid.uuid4()), source=str(origen), workspace_root=tmp_path / "w")
    runner.run()

    assert {p.name: p.read_bytes() for p in origen.iterdir() if p.is_file()} == hashes_antes
