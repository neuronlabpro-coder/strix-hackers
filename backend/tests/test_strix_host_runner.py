"""El modo host del runner: cómo lanza el motor, cómo lo mata y qué **no** busca.

## Qué se afirma aquí

Tres cosas, y las tres son las que hacen que este modo sirva de algo:

1. **El comando es el de verdad.** `strix -n --target <url> --scan-mode <modo>`, sin
   `--run-name` —que no existe— y sin `--output` —que tampoco—, y sin `--fail-on`, que
   convertiría «el escaneo encontró algo» en un código de salida de fallo.
2. **Los dos timeouts matan el árbol**, y no solo el padre.
3. **No se busca ningún `results.json`.** El contrato que el runner histórico esperaba no lo
   escribe ningún camino del motor, y una prueba que lo afirmara estaría fijando un defecto.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend.core.config import settings
from backend.workers.runner import host as host_module
from backend.workers.runner import sandbox as sandbox_module
from backend.workers.runner.exceptions import (
    SandboxExecutionError,
    SandboxOutputError,
    SandboxTimeoutError,
    SandboxWorkspaceError,
)
from backend.workers.runner.host import (
    PREFIJO_REFERENCIA_HOST,
    HostRunResult,
    StrixHostRunner,
)
from backend.workers.runner.strix_artefactos import leer_ejecucion

FIXTURES = Path(__file__).parent / "fixtures" / "strix_run_referencia"
RUN_ID = "11111111-1111-4111-8111-111111111111"


def _runner(tmp_path: Path, **kwargs: Any) -> StrixHostRunner:
    return StrixHostRunner(
        RUN_ID,
        kwargs.pop("target", "mindguard.site"),
        kwargs.pop("scan_mode", "QUICK"),
        workspace_root=tmp_path,
        cli_path=kwargs.pop("cli_path", "C:/herramientas/strix.exe"),
        **kwargs,
    )


def _workspace_con_run(workspace: Path, nombre: str = "mindguard-site_23ee") -> Path:
    destino = workspace / "strix_runs" / nombre
    destino.mkdir(parents=True)
    for fichero in ("run.json", "findings.sarif", "coverage.json"):
        shutil.copyfile(FIXTURES / fichero, destino / fichero)
    return destino


class _ProcesoFalso:
    """Un `Popen` que no ejecuta nada pero se comporta como uno terminado."""

    def __init__(self, pid: int = 4242, returncode: int = 0) -> None:
        self.pid = pid
        self.returncode: int | None = returncode
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
        del timeout
        return b"escaneo terminado", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


# --------------------------------------------------------------------------- #
# El comando
# --------------------------------------------------------------------------- #


def test_el_comando_del_modo_host_no_pide_run_name_ni_output(tmp_path: Path) -> None:
    """El comando real, y la lista de lo que **no** lleva.

    `--run-name` no existe: `-r` es `--resume`, y combinarla con `--target` es error. `--output`
    tampoco existe, y por eso este modo no espera ningún fichero de salida con nombre fijo.
    """

    comando = _runner(tmp_path).command()

    # El prefijo del comando, que es lo que esta prueba enumera: quién se ejecuta, contra qué y en
    # qué modo. Los topes de gasto van **detrás** y tienen su propia prueba, porque su valor sale
    # de configuración y fijarlos aquí haría que esta comprobara dos cosas y fallara por la que
    # no le toca.
    prefijo = comando[: comando.index("--max-budget")]
    assert prefijo[1:] == [
        "-n",
        "--target",
        "https://mindguard.site",
        "--scan-mode",
        "quick",
    ]
    texto = " ".join(comando)
    assert "--run-name" not in texto
    assert "--output" not in texto
    assert "results.json" not in texto
    assert "--fail-on" not in texto, (
        "con `--fail-on` el motor sale con 2 cuando encuentra hallazgos, y ese 2 es un escaneo "
        "que funcionó, no un fallo"
    )


def test_un_dominio_se_pasa_como_url_con_esquema_porque_el_motor_lo_exige(tmp_path: Path) -> None:
    """El esquema lo pone el runner, porque `PentestCreate` lo rechaza en el target.

    El dominio se guarda sin esquema a propósito —un dominio es un dominio— y el motor pide una
    URL. El esquema sale de `STRIX_DEFAULT_TARGET_SCHEME`, no del código.
    """

    assert _runner(tmp_path, target="mindguard.site").command()[3] == "https://mindguard.site"
    assert (
        _runner(tmp_path, target="https://api.test/openapi.json").command()[3]
        == "https://api.test/openapi.json"
    )


def test_un_repositorio_se_pasa_como_ruta_local_y_no_como_url(tmp_path: Path) -> None:
    """Un `REPOSITORY` es un directorio, y componiéndole un esquema dejaría de existir."""

    runner = _runner(tmp_path, target="https://github.com/acme/app", target_type="REPOSITORY")
    workspace = runner.setup_workspace()

    assert runner.command()[3] == str(workspace / "workspace")


def test_sin_ruta_de_ejecutable_el_modo_host_no_arma_un_comando(tmp_path: Path) -> None:
    """R1: sin `STRIX_CLI_PATH` no hay comando. Adivinar la ruta sería hardcodearla."""

    runner = _runner(tmp_path, cli_path="   ")

    with pytest.raises(SandboxExecutionError, match="STRIX_CLI_PATH"):
        runner.command()


def test_el_comando_lleva_el_tope_de_presupuesto_y_el_de_turnos(tmp_path: Path) -> None:
    """`--max-budget` y `--max-turns` van al motor, y salen de configuración (R1).

    Sin ellos el único tope es el muro de tiempo, y el muro de tiempo llega **después** de que el
    gasto ya ocurrió. Un QUICK real de este proyecto costó 1,85 USD sin tope declarado, así que el
    valor por defecto tiene que existir aunque nadie lo escriba en el `.env`.
    """

    comando = _runner(tmp_path).command()

    assert "--max-budget" in comando
    assert "--max-turns" in comando
    assert settings.strix_max_budget_usd > 0
    assert settings.strix_max_turns > 0
    # El valor que sale al motor es el de configuración, no uno escrito aquí.
    indice_presupuesto = comando.index("--max-budget")
    assert float(comando[indice_presupuesto + 1]) == float(settings.strix_max_budget_usd)
    indice_turnos = comando.index("--max-turns")
    assert int(comando[indice_turnos + 1]) == settings.strix_max_turns


def test_el_tope_de_presupuesto_se_puede_bajar_sin_tocar_el_codigo(tmp_path: Path) -> None:
    """R1: el tope es del despliegue, así que una prueba tiene que poder cambiarlo.

    Sin esto el valor por defecto sería lo único comprobable, y un despliegue que quisiera gastar
    menos no tendría forma de decirlo sin redesplegar el backend.
    """

    runner = _runner(tmp_path)

    # `Settings` está congelado a propósito, así que la prueba **construye** una copia con los
    # topes movidos en vez de mutar el global. Es la forma que no depende del `.env` de quien
    # ejecuta la prueba, que es justo lo que la hacía poco fiable antes.
    otro = settings.model_copy(
        update={"strix_max_budget_usd": Decimal("0.25"), "strix_max_turns": 7}
    )
    with patch("backend.workers.runner.host.settings", otro):
        comando = runner.command()

    assert comando[comando.index("--max-budget") + 1] == "0.25"
    assert comando[comando.index("--max-turns") + 1] == "7"


def test_el_comando_usa_los_topes_resueltos_para_este_run(tmp_path: Path) -> None:
    runner = _runner(tmp_path, max_budget_usd=Decimal("4.25"), max_turns=19)
    comando = runner.command()

    assert comando[comando.index("--max-budget") + 1] == "4.25"
    assert comando[comando.index("--max-turns") + 1] == "19"


def test_el_modo_host_no_pasa_el_tope_al_contenedor_que_no_lo_acepta(tmp_path: Path) -> None:
    """El tope va al **proceso**, y el modo contenedor no lo lleva.

    El modo contenedor construye su comando en `sandbox.py` y ese comando usa `--run-name` y
    `--output`, que el CLI real no tiene. Esa diferencia ya estaba escrita en `command()` y aquí
    solo se afirma que los dos modos no comparten el argumento: si compartieran la lista, un
    arreglo de uno rompería el otro.
    """

    fuente_host = Path(host_module.__file__).read_text(encoding="utf-8")

    assert "--max-budget" in fuente_host
    assert "--max-budget" not in Path(sandbox_module.__file__).read_text(encoding="utf-8")


def test_el_entorno_lleva_las_cuatro_variables_que_el_motor_necesita(tmp_path: Path) -> None:
    """`STRIX_LLM`, `LLM_API_KEY`, `LLM_API_BASE` y `STRIX_NON_INTERACTIVE`, más el entorno base.

    Se hereda `os.environ` porque el ejecutable instalado con `uv tool install` es un launcher que
    necesita su `PATH` y su `HOME`: un entorno vacío arrancaría el motor y fallaría al resolver el
    intérprete, que sería un fallo de este modo y no del motor.
    """

    runner = _runner(tmp_path)
    entorno = runner.environment()

    assert entorno["STRIX_NON_INTERACTIVE"] == "1"
    assert entorno["STRIX_RUN_ID"] == RUN_ID
    assert entorno["STRIX_LLM"]
    assert entorno["LLM_API_KEY"]
    assert "LLM_API_BASE" in entorno
    assert entorno.get("PATH") == __import__("os").environ.get("PATH")


def test_el_modo_host_no_pide_cerco_de_salida_ni_da_el_socket_de_docker(tmp_path: Path) -> None:
    """El motivo por el que este modo no puede dar el aislamiento de R3, escrito como prueba.

    No hay `iptables` que comprobar y no hay demonio al que hablar: el proceso hijo hereda los
    permisos del worker, con la red de Tailscale, PostgreSQL y Redis al alcance. Se afirma aquí
    para que nadie lo descubra deploying, y es la razón de que el contenedor siga siendo el valor
    por defecto.
    """

    fuente = (Path(host_module.__file__)).read_text(encoding="utf-8")

    assert "exigir_cerco_de_salida" not in fuente
    assert "docker" not in fuente.lower()
    assert "/var/run/docker.sock" not in fuente


# --------------------------------------------------------------------------- #
# La referencia del proceso
# --------------------------------------------------------------------------- #


def test_la_referencia_de_un_proceso_del_host_se_reconoce_y_una_de_contenedor_no() -> None:
    """El prefijo es lo que permite decidir sin mirar la configuración del otro proceso."""

    assert PREFIJO_REFERENCIA_HOST == "host-pid:"
    assert HostRunResult.parsear_referencia("host-pid:4242") == 4242
    assert HostRunResult.parsear_referencia("fenix-strix-1111") is None
    assert HostRunResult.parsear_referencia("host-pid:abc") is None
    assert HostRunResult.parsear_referencia("host-pid:0") is None


def test_la_referencia_se_escribe_con_el_pid_y_se_vuelve_a_leer(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    resultado = HostRunResult(
        exit_code=0,
        ejecucion=_ejecucion_minima(),
        process_id=4242,
    )

    referencia = resultado.referencia_de_proceso()

    assert referencia == "host-pid:4242"
    assert HostRunResult.parsear_referencia(referencia) == 4242
    del runner


def test_sin_pid_la_referencia_no_inventa_un_proceso() -> None:
    """Un PID `0` es el planificador, no un escaneo: se escribe, pero no se puede matar."""

    resultado = HostRunResult(exit_code=0, ejecucion=_ejecucion_minima(), process_id=None)

    assert HostRunResult.parsear_referencia(resultado.referencia_de_proceso()) is None


def _ejecucion_minima() -> Any:
    from backend.workers.runner.strix_artefactos import EjecucionStrix

    return EjecucionStrix(
        directorio=Path("."),
        run_id="r",
        run_name="r",
        status="completed",
        start_time=None,
        end_time=None,
        scan_mode="quick",
        objetivo=None,
        total_tokens=None,
        prompt_tokens=None,
        completion_tokens=None,
        coste=None,
        hallazgos=(),
        registros_cobertura=0,
        cobertura=None,
        informe_markdown=None,
    )


# --------------------------------------------------------------------------- #
# La ejecución
# --------------------------------------------------------------------------- #


def test_el_modo_host_lee_los_artefactos_y_purga_el_workspace(tmp_path: Path) -> None:
    """El ciclo entero: workspace, subproceso, artefactos leídos y workspace purgado (R5).

    El purgado va en `finally` siempre, y los artefactos se leen **antes**. Leerlos después sería
    leer un directorio que ya no existe, y el error aparecería como «artefacto ilegible» cuando el
    problema es que nadie los copió a tiempo.
    """

    runner = _runner(tmp_path)
    escritos: list[str] = []

    def popeno(comando: list[str], **kwargs: Any) -> _ProcesoFalso:
        escritos.append(comando[0])
        _workspace_con_run(Path(kwargs["cwd"]))
        return _ProcesoFalso()

    with patch.object(host_module.subprocess, "Popen", side_effect=popeno):
        resultado = runner.run(timeout_seconds=30, soft_timeout_seconds=0)

    assert escritos == ["C:/herramientas/strix.exe"]
    assert resultado.exit_code == 0
    assert resultado.ejecucion.hallazgos == ()
    assert resultado.ejecucion.registros_cobertura == 18
    assert resultado.process_id == 4242
    assert runner.temp_dir is None, "el workspace tiene que quedar purgado"
    assert not (tmp_path / RUN_ID).exists()


def test_el_modo_host_no_busca_ningun_results_json(tmp_path: Path) -> None:
    """El contrato falso no aparece en el camino de host: ni se escribe ni se lee.

    Se pone un `results.json` en el workspace con un contenido que **no** es el contrato antiguo,
    y se afirma que el run sigue saliendo bien. Con el contrato viejo, ese fichero se leería y se
    interpretaría como un reporte con `scan_id` y `findings`.
    """

    runner = _runner(tmp_path)
    falsos = json.dumps({"scan_id": "inventado", "status": "completed", "findings": []})

    def popeno(comando: list[str], **kwargs: Any) -> _ProcesoFalso:
        workspace = Path(kwargs["cwd"])
        _workspace_con_run(workspace)
        (workspace / "results.json").write_text(falsos, encoding="utf-8")
        return _ProcesoFalso()

    with patch.object(host_module.subprocess, "Popen", side_effect=popeno):
        resultado = runner.run(timeout_seconds=30, soft_timeout_seconds=0)

    assert resultado.ejecucion.run_id == "mindguard-site_23ee"
    assert resultado.ejecucion.hallazgos == ()


def test_el_timeout_duro_mata_el_arbol_y_deja_el_run_terminado(tmp_path: Path) -> None:
    """Al agotarse el timeout duro se mata el árbol y se lanza `SandboxTimeoutError`.

    El proceso, no solo el padre: el motor lanza `httpx`, `nmap` y navegadores, y los nietos que
    sobreviven siguen cobrando tokens del proveedor con el run ya terminal.
    """

    runner = _runner(tmp_path)
    muertos: list[object] = []

    class _ProcesoQueSeCuelga(_ProcesoFalso):
        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            raise subprocess.TimeoutExpired(cmd="strix", timeout=timeout or 0)

    with (
        patch.object(
            host_module.subprocess, "Popen", side_effect=lambda *a, **k: _ProcesoQueSeCuelga()
        ),
        patch.object(StrixHostRunner, "matar_arbol", side_effect=muertos.append),
        pytest.raises(SandboxTimeoutError),
    ):
        runner.run(timeout_seconds=1, soft_timeout_seconds=0)

    assert len(muertos) == 1, "el árbol se mata una vez, al agotarse el timeout duro"
    assert runner.temp_dir is None, "el workspace se purga aunque el escaneo se cuelgue"


def test_el_timeout_suave_mata_y_no_se_confunde_con_un_escaneo_limpio(tmp_path: Path) -> None:
    """El timeout suave también mata el árbol, y el run no se guarda como completado."""

    runner = _runner(tmp_path)
    muertos: list[object] = []
    evento = host_module.threading.Event()

    class _ProcesoLento(_ProcesoFalso):
        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            host_module.threading.Timer(0.01, StrixHostRunner.matar_arbol, args=(self,)).start()
            host_module.threading.Event().wait(0.2)
            return b"", b""

    with (
        patch.object(
            host_module.subprocess, "Popen", side_effect=lambda *a, **k: _ProcesoLento()
        ),
        patch.object(StrixHostRunner, "matar_arbol", side_effect=muertos.append),
        pytest.raises(SandboxTimeoutError, match="suave"),
    ):
        runner.run(timeout_seconds=30, soft_timeout_seconds=0.05)

    assert muertos, "el timeout suave tiene que matar el árbol"
    assert evento.is_set() is False


def test_el_modo_host_no_reintenta_solo_y_no_pide_mas_de_un_intento(tmp_path: Path) -> None:
    """Un fallo del proceso no dispara un segundo intento: cobraría tokens otra vez.

    El motivo es de dinero, no de estilo: este modo ejecuta escaneos reales de veinte minutos.
    La política de reintentos vive en `workers/tasks.py`; aquí solo se ejecuta **una** vez.
    """

    runner = _runner(tmp_path)
    llamadas: list[Any] = []

    def popeno(*args: Any, **kwargs: Any) -> _ProcesoFalso:
        llamadas.append(args)
        raise OSError("no se pudo arrancar")

    with patch.object(host_module.subprocess, "Popen", side_effect=popeno):
        with pytest.raises(SandboxExecutionError):
            runner.run(timeout_seconds=1, soft_timeout_seconds=0)

    assert len(llamadas) == 1


def test_si_el_workspace_no_se_puede_preparar_no_se_intenta_ejecutar(tmp_path: Path) -> None:
    """Un workspace tomado por otro es un fallo de host, y no se lanza un motor sin directorio.

    El workspace es de un solo run y se crea con `exist_ok=False`: reutilizar el de otro scan sería
    que dos escaneos compartieran directorio, y el motor escribe `strix_runs/` dentro.
    """

    ocupado = tmp_path / RUN_ID
    ocupado.mkdir(parents=True)

    runner = _runner(tmp_path)

    with patch.object(
        host_module.subprocess, "Popen", side_effect=AssertionError("no debería lanzarse")
    ):
        with pytest.raises(SandboxWorkspaceError):
            runner.run(timeout_seconds=1, soft_timeout_seconds=0)


def test_el_workspace_preparado_se_usa_tal_cual(tmp_path: Path) -> None:
    """`workspace_prepared=True` es lo que usa el pipeline de revisiones de PR."""

    runner = _runner(tmp_path)
    workspace = runner.setup_workspace()

    def popeno(comando: list[str], **kwargs: Any) -> _ProcesoFalso:
        _workspace_con_run(Path(kwargs["cwd"]))
        return _ProcesoFalso()

    with patch.object(host_module.subprocess, "Popen", side_effect=popeno):
        resultado = runner.run(timeout_seconds=30, soft_timeout_seconds=0, workspace_prepared=True)

    assert resultado.exit_code == 0
    assert not workspace.exists()


def test_el_informe_markdown_se_lee_cuando_existe(tmp_path: Path) -> None:
    """El cuarto artefacto se lee cuando está, y no se exige que esté.

    `penetration_test_report.md` es la prosa del motor: 10 KB en el run de referencia. El parser lo
    trae como `informe_markdown` porque es lo que el motor escribe, pero **no** lo usa para decidir
    nada: si un motor no lo deja, el run sigue siendo un escaneo válido.
    """

    destino = _workspace_con_run(tmp_path)
    (destino / "penetration_test_report.md").write_text(
        "# Security Penetration Test Report\n\nNo exploitable vulnerabilities.\n",
        encoding="utf-8",
    )

    ejecucion = leer_ejecucion(tmp_path)

    assert ejecucion.informe_markdown is not None
    assert ejecucion.informe_markdown.startswith("# Security Penetration Test Report")


def test_el_artefacto_ilegible_se_traduce_al_error_del_diagnostico(tmp_path: Path) -> None:
    """Un motor que no dejó nada utilizable sale con `STRIX_OUTPUT_UNUSABLE`, no genérico."""

    from backend.workers.runner.diagnostico import diagnosticar_fallo

    runner = _runner(tmp_path)

    def popeno(comando: list[str], **kwargs: Any) -> _ProcesoFalso:
        # Workspace sin `strix_runs`: el motor no dejó nada que leer.
        del kwargs
        assert comando
        return _ProcesoFalso()

    with patch.object(host_module.subprocess, "Popen", side_effect=popeno):
        with pytest.raises(Exception) as error:
            runner.run(timeout_seconds=30, soft_timeout_seconds=0)

    assert diagnosticar_fallo(error.value).codigo == "STRIX_OUTPUT_UNUSABLE"


def test_el_workspace_se_purga_aunque_el_motor_no_deje_artefactos(tmp_path: Path) -> None:
    """R5 es R5 pase lo que pase: el workspace no se queda en el disco."""

    runner = _runner(tmp_path)

    def popeno(comando: list[str], **kwargs: Any) -> _ProcesoFalso:
        del kwargs
        assert comando
        return _ProcesoFalso()

    with patch.object(host_module.subprocess, "Popen", side_effect=popeno):
        with pytest.raises(SandboxOutputError):
            runner.run(timeout_seconds=30, soft_timeout_seconds=0)

    assert not (tmp_path / RUN_ID).exists()


def test_el_workspace_fresco_contiene_los_cuatro_ficheros_del_motor(tmp_path: Path) -> None:
    """Los artefactos se leen del workspace y no de ninguna otra ruta.

    Y el informe `.md` se lee cuando existe, que en estas fixtures no: son 10 KB de prosa y el
    parser ya lo trata como opcional. La prueba de que se lee cuando está, y no solo cuando falta,
    va en `test_strix_artefactos.py`.
    """

    workspace = Path(tmp_path) / "workspace"
    workspace.mkdir()
    _workspace_con_run(workspace)

    ejecucion = leer_ejecucion(workspace)

    assert ejecucion.directorio.name == "mindguard-site_23ee"
    assert ejecucion.informe_markdown is None, "el informe no está en las fixtures por tamaño"
