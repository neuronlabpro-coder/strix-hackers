"""El contrato real de los artefactos del motor, probado con artefactos reales.

## Qué hay aquí y por qué con estos datos

Los tres ficheros de `fixtures/strix_run_referencia/` son la salida de un escaneo real y
terminado, copiados sin retocar. No son unos datos inventados para que una prueba pase: si el
parser leyera otra cosa, la prueba caería.

Y ese escaneo es el caso más difícil que hay, no el más cómodo: **terminó bien y no encontró
nada**. `findings_filed: 0`, 18 superficies revisadas, 17 resultados `pass` y **uno `open`** que
es una fila de cobertura (`needs_follow_up`), no una vulnerabilidad. Un parser que usara
`kind != "pass"` publicaría un hallazgo falso; uno que tratara cero hallazgos como fallo
mentiría sobre el motor. Las dos cosas se afirman aquí.

## La regla de este fichero

Cada prueba afirma un contrato que se puede **romper**. Las que se pueden romper con una línea de
código se prueban con una línea de código, y las que no —el symlink, el fichero que se sustituye
entre el `stat` y el `open`— se prueban con el sistema de ficheros de verdad.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from backend.apps.vulnerabilities.models import SeverityEnum
from backend.workers.runner.exceptions import SandboxOutputError, StrixRunIncompleteError
from backend.workers.runner.strix_artefactos import (
    leer_cobertura,
    leer_ejecucion,
    leer_hallazgos,
    leer_texto_protegido,
    localizar_directorio_de_run,
    severidad_desde_cvss,
)

FIXTURES = Path(__file__).parent / "fixtures" / "strix_run_referencia"
NOMBRE_DEL_RUN = "mindguard-site_23ee"


def _fixture(nombre: str) -> dict[str, Any]:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


def _workspace_con_run(workspace: Path, nombre: str = NOMBRE_DEL_RUN) -> Path:
    """Copia los tres artefactos al layout que el motor deja en `<cwd>/strix_runs/<run>/`."""

    destino = workspace / "strix_runs" / nombre
    destino.mkdir(parents=True)
    for fichero in ("run.json", "findings.sarif", "coverage.json"):
        shutil.copyfile(FIXTURES / fichero, destino / fichero)
    return destino


# --------------------------------------------------------------------------- #
# El run de referencia: escaneado entero, cero hallazgos
# --------------------------------------------------------------------------- #


def test_el_run_de_referencia_da_cero_hallazgos_y_dieciocho_registros_de_cobertura() -> None:
    """El caso central: el motor funcionó y no encontró nada, y eso son dos cifras distintas."""

    hallazgos, registros = leer_hallazgos(
        _fixture("findings.sarif"),
        objetivo_por_defecto="https://mindguard.site",
    )

    assert hallazgos == ()
    assert registros == 18, (
        "el SARIF de referencia tiene 18 resultados y los 18 son de cobertura: 17 `pass` y uno "
        "`open`. Si el recuento baja, se está tratando una fila de cobertura como hallazgo."
    )


def test_la_fila_open_del_sarif_de_referencia_es_cobertura_y_no_una_vulnerabilidad() -> None:
    """El `kind:"open"` de `needs_follow_up` no es un hallazgo, aunque no sea `pass`.

    Es la prueba que separa el discriminador correcto del ingenuo. `mindguard-site_23ee` tiene
    exactamente un resultado `open`, y su `ruleId` empieza por `strix-coverage/` con
    `coverage_outcome: "needs_follow_up"`: el motor diciendo que la superficie
    `https://mindguardredteam.com/contact.php` quedó fuera del alcance autorizado. Con
    `kind != "pass"` eso entraría en `vulnerabilities` y el panel publicaría una vulnerabilidad
    que no existe.
    """

    documento = _fixture("findings.sarif")
    resultados = documento["runs"][0]["results"]
    abiertos = [r for r in resultados if r.get("kind") == "open"]

    assert len(abiertos) == 1, "el run de referencia tiene exactamente una fila `open`"
    assert abiertos[0]["ruleId"].startswith("strix-coverage/")
    assert abiertos[0]["properties"]["strix"]["coverage_outcome"] == "needs_follow_up"

    hallazgos, registros = leer_hallazgos(documento, objetivo_por_defecto=None)
    assert hallazgos == ()
    assert registros == len(resultados)


def test_el_escaneo_limpio_se_distingue_de_un_fallo_y_de_un_run_sin_terminar() -> None:
    """Cero hallazgos es un resultado; `status` distinto de `completed` es un fallo."""

    workspace = Path(os.environ["TEMP"]) / "fenix-prueba-run-referencia"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    try:
        _workspace_con_run(workspace)
        ejecucion = leer_ejecucion(workspace)

        assert ejecucion.status == "completed"
        assert ejecucion.hallazgos == ()
        assert ejecucion.escaneo_limpio is True
        assert ejecucion.start_time is not None
        assert ejecucion.end_time is not None
        assert ejecucion.end_time > ejecucion.start_time
        assert ejecucion.objetivo == "https://mindguard.site"
        assert ejecucion.scan_mode == "quick"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_un_status_distinto_de_completed_es_un_fallo_y_no_un_escaneo_limpio(tmp_path: Path) -> None:
    """`status: "failed"` lanza, y lanza `StrixRunIncompleteError`, no un error genérico.

    Es un tipo propio y no `SandboxOutputError` porque son dos cosas que el panel explica de dos
    maneras: un artefacto ilegible puede mejorar con otro modelo, y un artefacto que **dice** que
    el run no terminó es el motor que se quedó sin tiempo o sin presupuesto.
    """

    destino = _workspace_con_run(tmp_path)
    registro = json.loads((destino / "run.json").read_text(encoding="utf-8"))
    registro["status"] = "failed"
    (destino / "run.json").write_text(json.dumps(registro), encoding="utf-8")

    with pytest.raises(StrixRunIncompleteError) as error:
        leer_ejecucion(tmp_path)

    assert "failed" in str(error.value)


def test_un_run_en_curso_tampoco_es_un_escaneo_limpio(tmp_path: Path) -> None:
    """`status: "running"` es el caso peor: hay hallazgos y no se puede cerrar el run."""

    destino = _workspace_con_run(tmp_path)
    registro = json.loads((destino / "run.json").read_text(encoding="utf-8"))
    registro["status"] = "running"
    (destino / "run.json").write_text(json.dumps(registro), encoding="utf-8")

    with pytest.raises(StrixRunIncompleteError):
        leer_ejecucion(tmp_path)


# --------------------------------------------------------------------------- #
# Los huecos de cobertura son información del motor, no basura
# --------------------------------------------------------------------------- #


def test_el_hueco_de_cobertura_llega_con_su_detalle_y_no_se_traga() -> None:
    """`coverage.json` declara un hueco y el parser lo trae entero, con superficie y motivo.

    El hueco es la fila `needs_follow_up` de `contact.php`: el motor diciendo que encontró una
    superficie dinámica y no pudo probarla por estar fuera del alcance. Tragárselo convertiría un
    escaneo con límites en un escaneo limpio, que es la misma mentira que el otro lado.
    """

    cobertura = leer_cobertura(_fixture("coverage.json"))

    assert cobertura is not None
    assert cobertura.findings_filed == 0
    assert cobertura.surfaces_reviewed == 18
    assert cobertura.complete is True
    assert cobertura.scan_status == "completed"
    assert len(cobertura.gaps) == 1
    hueco = cobertura.gaps[0]
    assert hueco.kind == "needs_follow_up"
    assert hueco.surface == "https://mindguardredteam.com/contact.php"
    assert "PHP" in hueco.risk_area
    assert "unverified" in hueco.detail
    assert hueco.detail in cobertura.advertencias


def test_los_agentes_del_motor_llegan_con_su_estado() -> None:
    """`machine_observed.agents` es la cuenta de quién corrió, y también se persiste."""

    cobertura = leer_cobertura(_fixture("coverage.json"))

    assert cobertura is not None
    nombres = {nombre for nombre, _ in cobertura.agentes}
    assert "Root Agent" in nombres
    assert all(estado == "completed" for _, estado in cobertura.agentes)


def test_el_informe_markdown_del_run_real_se_lee(tmp_path: Path) -> None:
    """El cuarto artefacto se lee del run real, con sus diez kilobytes de prosa.

    ## Por qué esta prueba y no la de «se lee cuando existe»

    La otra construye un `.md` de dos líneas y comprueba que el parser lo trae. Eso demuestra que
    el lector funciona; **no** que el informe de verdad —10 KB de prosa escrita por el motor, con
    tildes, tablas y acentos— sobreviva a la lectura con la decodificación estricta en UTF-8, que es
    donde un informe real puede romperse y un fichero inventado no.

    Se lee del directorio del run completo, que es el único que trae el informe: el de referencia se
    recortó a tres ficheros y el `README` dice que el `.md` se dejó fuera por tamaño. El recorte es
    correcto para lo que ese directorio prueba y **no** sirve para esto.

    ## Por qué se compara con el fichero entero y no con un mínimo de caracteres

    Porque un informe truncado por el techo de tamaño **parece** entero: sale prosa que empieza bien
    y se corta sin avisar. Un mínimo de caracteres no lo distingue de uno completo; la igualdad
    con el fichero, sí.

    Y se lee el fichero en **binario** y se decodifica, no con `read_text`: el lector abre en
    binario a propósito, así que conserva los finales de línea tal cual están, y `read_text` los
    normalizaría. Con `read_text` la comparación fallaría por los `\\r\\n` del fichero de Windows y
    la prueba estaría midiendo el final de línea del repo, no la lectura.
    """

    origen = Path(__file__).parent / "fixtures" / "strix_run_completado_real"
    destino = tmp_path / "strix_runs" / NOMBRE_DEL_RUN
    destino.mkdir(parents=True)
    for fichero in ("run.json", "findings.sarif", "coverage.json", "penetration_test_report.md"):
        shutil.copyfile(origen / fichero, destino / fichero)

    ejecucion = leer_ejecucion(tmp_path)

    assert ejecucion.informe_markdown is not None
    assert ejecucion.informe_markdown.startswith("#")
    # La comparación es de **longitud exacta** contra el fichero, no de «más de 5000 caracteres».
    # Un techo de tamaño mal puesto, o una decodificación que perdiera el final, saldrían como un
    # informe más corto: y un informe más corto que parece entero es lo peor que puede pasar aquí.
    original = (destino / "penetration_test_report.md").read_bytes().decode("utf-8")
    assert len(original) > 5_000
    assert ejecucion.informe_markdown == original


def test_el_consumo_se_desglosa_desde_la_raiz_y_no_desde_los_proveedores() -> None:
    """La salida sale de la raíz de `llm_usage`, porque `providers` **no** la publica.

    Los tres proveedores del run de referencia traen `requests`, `input_tokens`, `cached_tokens`,
    `cost`, `cache_misses` y `missed_tokens`, y ninguno trae `output_tokens`. Sumarlos daría
    una salida de cero, que es tarificar como si el modelo no hubiera generado nada.
    """

    workspace = Path(os.environ["TEMP"]) / "fenix-prueba-consumo"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    try:
        _workspace_con_run(workspace)
        ejecucion = leer_ejecucion(workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    assert ejecucion.total_tokens == 16865493
    consumo = ejecucion.consumo_desglosado
    assert consumo is not None
    assert consumo.prompt_tokens == 16819076
    assert consumo.completion_tokens == 46417
    assert consumo.prompt_tokens + consumo.completion_tokens == ejecucion.total_tokens

    providers = _fixture("run.json")["llm_usage"]["providers"]
    assert providers, "el run de referencia sí publica proveedores"
    assert all("output_tokens" not in bloque for bloque in providers.values()), (
        "si algún proveedor publicara `output_tokens`, la suma de proveedores volvería a ser "
        "una opción y habría que decidir cuál de las dos manda"
    )


def test_sin_desglose_no_se_reparte_el_total_entre_entrada_y_salida(tmp_path: Path) -> None:
    """Un run que solo publica `total_tokens` no se tarifica repartiendo el total.

    La entrada y la salida tienen precios distintos, así que partir el total en dos mitades es un
    cargo inventado. Lo que se hace es devolver `None`, que es «no lo sabemos», y el run se queda
    con la reserva.
    """

    destino = _workspace_con_run(tmp_path)
    registro = json.loads((destino / "run.json").read_text(encoding="utf-8"))
    registro["llm_usage"] = {"total_tokens": 1000}
    (destino / "run.json").write_text(json.dumps(registro), encoding="utf-8")

    ejecucion = leer_ejecucion(tmp_path)

    assert ejecucion.total_tokens == 1000
    assert ejecucion.consumo_desglosado is None


# --------------------------------------------------------------------------- #
# Hallazgos de verdad
# --------------------------------------------------------------------------- #


def _sarif_con_hallazgos(
    *resultados: dict[str, Any],
    reglas: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    catalogo = [{"id": clave, **valor} for clave, valor in (reglas or {}).items()]
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "Strix", "rules": catalogo}},
                "results": list(resultados),
            }
        ],
    }


def _hallazgo_sarif(
    *,
    severity: str | None = "high",
    cvss: float | None = 8.1,
    rule_id: str = "CWE-89",
    titulo: str = "SQL injection en el login",
) -> dict[str, Any]:
    resultado: dict[str, Any] = {
        "ruleId": rule_id,
        "level": "error",
        "message": {"text": f"{titulo}\n\nEl parametro `id` va concatenado a la consulta."},
        "locations": [
            {"logicalLocations": [{"fullyQualifiedName": "POST /login", "kind": "endpoint"}]}
        ],
        "partialFingerprints": {"primaryLocationLineHash": "abc123"},
    }
    strix: dict[str, Any] = {"id": "vuln-0001", "target": "https://mindguard.site/login"}
    if severity is not None:
        strix["severity"] = severity
    if cvss is not None:
        strix["cvss"] = cvss
    resultado["properties"] = {
        "security-severity": "8.1",
        "strix": strix,
    }
    return resultado


@pytest.mark.parametrize(
    ("severity", "cvss", "esperado"),
    [
        ("critical", 9.8, SeverityEnum.CRITICAL),
        ("high", 8.1, SeverityEnum.HIGH),
        ("medium", 5.4, SeverityEnum.MEDIUM),
        ("low", 3.1, SeverityEnum.LOW),
        ("informational", None, SeverityEnum.INFO),
    ],
)
def test_un_resultado_fail_produce_un_hallazgo_con_su_severidad(
    severity: str,
    cvss: float | None,
    esperado: SeverityEnum,
) -> None:
    """`kind: "fail"` (o su ausencia) es un hallazgo, y la severidad es la del motor."""

    documento = _sarif_con_hallazgos(
        _hallazgo_sarif(severity=severity, cvss=cvss),
        reglas={"CWE-89": {"shortDescription": {"text": "SQL injection en el login"}}},
    )

    hallazgos, registros = leer_hallazgos(documento, objetivo_por_defecto=None)

    assert registros == 0, "un resultado `fail` no es un registro de cobertura"
    assert len(hallazgos) == 1
    hallazgo = hallazgos[0]
    assert hallazgo.severity is esperado
    assert hallazgo.title == "SQL injection en el login"
    assert hallazgo.source_finding_id == "vuln-0001"
    assert hallazgo.affected_target == "https://mindguard.site/login"
    assert hallazgo.cvss_score == pytest.approx(cvss if cvss is not None else 1.0)


def test_un_resultado_sin_kind_es_un_hallazgo_porque_sarif_lo_da_por_fallido() -> None:
    """La ausencia de `kind` **no** es cobertura: el valor por defecto de SARIF es `fail`.

    El motor no escribe `kind` en los hallazgos (`_build_result` no lo pone), así que tratar la
    ausencia como cobertura perdería el hallazgo entero.
    """

    resultado = _hallazgo_sarif()
    assert "kind" not in resultado

    hallazgos, registros = leer_hallazgos(
        _sarif_con_hallazgos(resultado), objetivo_por_defecto=None
    )

    assert len(hallazgos) == 1
    assert registros == 0


def test_un_hallazgo_sin_severidad_cae_en_medium_y_no_inventa_una_puntuacion() -> None:
    """Sin etiqueta ni CVSS, la severidad es `MEDIUM` y el CVSS es el de esa severidad.

    `INFO` escondería un hallazgo que el motor decidió registrar; `HIGH` inventaría una gravedad
    que nadie midió. El número sale del mismo mapa que usa el propio motor para
    `security-severity`, así que no contradice al artefacto.
    """

    documento = _sarif_con_hallazgos(_hallazgo_sarif(severity=None, cvss=None))

    hallazgos, _ = leer_hallazgos(documento, objetivo_por_defecto=None)

    assert hallazgos[0].severity is SeverityEnum.MEDIUM
    assert hallazgos[0].cvss_score == 5.5


def test_un_hallazgo_sin_severidad_pero_con_cvss_deriva_la_severidad_del_numero() -> None:
    """Con CVSS y sin etiqueta, la severidad sale de las bandas de CVSS v3."""

    documento = _sarif_con_hallazgos(_hallazgo_sarif(severity=None, cvss=9.4))

    hallazgos, _ = leer_hallazgos(documento, objetivo_por_defecto=None)

    assert hallazgos[0].severity is SeverityEnum.CRITICAL
    assert hallazgos[0].cvss_score == pytest.approx(9.4)


@pytest.mark.parametrize(
    ("puntuacion", "esperado"),
    [
        (9.8, SeverityEnum.CRITICAL),
        (7.0, SeverityEnum.HIGH),
        (4.0, SeverityEnum.MEDIUM),
        (0.1, SeverityEnum.LOW),
        (0.0, SeverityEnum.INFO),
    ],
)
def test_las_bandas_de_cvss_cortan_donde_dice_el_rango(
    puntuacion: float, esperado: SeverityEnum
) -> None:
    assert severidad_desde_cvss(puntuacion) is esperado


def test_un_hallazgo_usa_el_objetivo_del_run_cuando_el_sarif_no_lo_dice() -> None:
    """Un hallazgo sin `target`, sin endpoint y sin anclaje se ancla al objetivo del run.

    Sin ese respaldo el parser no podría rellenar `affected_target`, que es `NOT NULL`, y un
    hallazgo sin target es un hallazgo al que no se le puede volver a preguntar.
    """

    resultado: dict[str, Any] = {
        "ruleId": "CWE-89",
        "kind": "fail",
        "level": "error",
        "message": {"text": "SQLi\n\nDetalle"},
        "locations": [],
        "properties": {"strix": {"id": "vuln-9", "severity": "high", "cvss": 7.5}},
    }

    hallazgos, _ = leer_hallazgos(
        _sarif_con_hallazgos(resultado), objetivo_por_defecto="https://mindguard.site"
    )

    assert hallazgos[0].affected_target == "https://mindguard.site"


def test_un_sarif_sin_hallazgos_sigue_siendiendo_un_documento_valido() -> None:
    """`write_sarif` emite el documento aunque no haya hallazgos, y el parser lo acepta.

    Es el caso del run de referencia, y es el que obliga a que «cero hallazgos» sea un resultado y
    no la ausencia de documento.
    """

    hallazgos, registros = leer_hallazgos(
        {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Strix"}}, "results": []}]},
        objetivo_por_defecto=None,
    )

    assert hallazgos == ()
    assert registros == 0


def test_un_sarif_sin_runs_es_un_artefacto_ilegible_y_no_un_escaneo_vacio() -> None:
    """Un SARIF sin `runs` no describe un escaneo: es un fichero de otra cosa."""

    with pytest.raises(SandboxOutputError):
        leer_hallazgos({"version": "2.1.0"}, objetivo_por_defecto=None)


def test_la_evidencia_del_hallazgo_no_se_perse_nunca_porque_es_not_null() -> None:
    """`poc_reproduction_raw` es `NOT NULL`, así que cae al mensaje antes que a una cadena vacía.

    El SARIF **no** lleva el payload —el motor lo saca a propósito—, así que lo único disponible
    es la descripción del PoC, el impacto o el análisis. Si no hay ninguno, se persiste el mensaje
    del resultado: una evidencia pobre es mejor que una columna de evidencia vacía.
    """

    resultado: dict[str, Any] = {
        "ruleId": "CWE-79",
        "kind": "fail",
        "level": "error",
        "message": {"text": "XSS reflejado\n\nSe inyecta en el parametro q."},
        "properties": {"strix": {"id": "vuln-x", "severity": "medium", "target": "https://x.test"}},
    }

    hallazgos, _ = leer_hallazgos(_sarif_con_hallazgos(resultado), objetivo_por_defecto=None)

    assert hallazgos[0].poc_reproduction_raw
    assert "Se inyecta" in hallazgos[0].poc_reproduction_raw


# --------------------------------------------------------------------------- #
# El directorio del run: por patrón, porque el nombre lo pone el motor
# --------------------------------------------------------------------------- #


def test_un_workspace_sin_strix_runs_no_tiene_run_que_leer(tmp_path: Path) -> None:
    with pytest.raises(SandboxOutputError, match="strix_runs"):
        localizar_directorio_de_run(tmp_path)


def test_un_strix_runs_vacio_no_te_devuelve_el_primer_directorio_por_pura_suerte(
    tmp_path: Path,
) -> None:
    (tmp_path / "strix_runs").mkdir()
    (tmp_path / "strix_runs" / "mindguard-site_9999").mkdir()

    with pytest.raises(SandboxOutputError, match="ningún directorio de run"):
        localizar_directorio_de_run(tmp_path)


def test_con_varios_runs_gana_el_mas_reciente_por_start_time_y_no_por_mtime(tmp_path: Path) -> None:
    """El `run.json.start_time` manda, y no el orden del directorio ni su `mtime`.

    El nombre del run lo genera el motor, así que dos runs pueden convivir en el workspace. El
    criterio es el instante que escribió el motor, y la prueba pone el `mtime` **al revés**: el
    run más reciente es el más antiguo en disco, así que un criterio de `mtime` elige el otro.

    Y la inversión se **comprueba antes** de afirmar nada. Un `os.utime` que el sistema ignora
    dejaría los dos `mtime` iguales, y entonces la prueba pasaría por casualidad —el `max` de un
    empate es el orden de iteración— estaría probando lo contrario de lo que dice. Fallar aquí es
    mejor que pasar por la razón equivocada.
    """

    viejo = tmp_path / "strix_runs" / "mindguard-site_1111"
    nuevo = tmp_path / "strix_runs" / "mindguard-site_2222"
    viejo.mkdir(parents=True)
    nuevo.mkdir(parents=True)
    (viejo / "run.json").write_text(
        json.dumps({"start_time": "2026-10-07T08:45:05+00:00", "status": "completed"}),
        encoding="utf-8",
    )
    (nuevo / "run.json").write_text(
        json.dumps({"start_time": "2026-10-07T09:07:34+00:00", "status": "completed"}),
        encoding="utf-8",
    )
    ahora = time.time()
    os.utime(nuevo / "run.json", (ahora - 7200, ahora - 7200))
    os.utime(viejo / "run.json", (ahora, ahora))
    assert (nuevo / "run.json").stat().st_mtime < (viejo / "run.json").stat().st_mtime, (
        "el sistema no aplicó `utime`: la inversión no existe y la prueba no probaría nada"
    )

    assert localizar_directorio_de_run(tmp_path) == nuevo


def test_un_directorio_de_run_sin_run_json_no_cuenta_como_run(tmp_path: Path) -> None:
    """Un directorio con los artefactos pero sin `run.json` no se puede elegir por fecha."""

    destino = _workspace_con_run(tmp_path)
    (destino / "run.json").unlink()

    with pytest.raises(SandboxOutputError):
        localizar_directorio_de_run(tmp_path)


def test_sin_cobertura_el_parser_devuelve_none_y_no_ceros_inventados(tmp_path: Path) -> None:
    """`coverage.json` ausente da `None`, no un `CoberturaStrix` con ceros.

    Con ceros, el panel leería «se revisaron 0 superficies» de un run que sí se escaneó. `None` es
    «no hay dato», y el panel tiene que poder pintar las dos cosas.
    """

    destino = _workspace_con_run(tmp_path)
    (destino / "coverage.json").unlink()

    ejecucion = leer_ejecucion(tmp_path)

    assert ejecucion.cobertura is None
    assert ejecucion.hallazgos == ()
    assert ejecucion.escaneo_limpio is True, (
        "sin dato de cobertura el run sigue siendo un escaneo limpio si terminó y no dejó "
        "hallazgos: lo que no se puede afirmar es cuántas superficies se miraron"
    )


# --------------------------------------------------------------------------- #
# Las defensas de lectura
# --------------------------------------------------------------------------- #


def test_la_lectura_rechaza_un_enlace_simbolico(tmp_path: Path) -> None:
    """Un symlink en lugar del artefacto no se sigue: es el ataque que `O_NOFOLLOW` prevents."""

    real = tmp_path / "real.json"
    real.write_text('{"status":"completed"}', encoding="utf-8")
    enlace = tmp_path / "enlace.json"
    try:
        enlace.symlink_to(real)
    except OSError as error:  # pragma: no cover - depende de los permisos del sistema
        pytest.skip(f"symlink no disponible: {error}")

    with pytest.raises(SandboxOutputError, match="no es un archivo regular"):
        leer_texto_protegido(enlace, "run.json")


def test_la_lectura_rechaza_un_fichero_sustituido_entre_el_stat_y_el_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La comparación de `st_dev`/`st_ino` es la defensa, y aquí se rompe de verdad.

    Se sustituye el fichero **después** del `lstat` y **antes** del `open`, que es la ventana que
    `O_NOFOLLOW` no cubre: `O_NOFOLLOW` solo rechaza un enlace simbólico en el momento del
    `open`, y aquí el `open` es legítimo —el fichero ya no es un enlace— pero no es el mismo
    fichero que se comprobó. Sin la comparación de inodo, lo que se leería sería lo que el atacante
    puso.

    Es la prueba que corre **en todas partes**: el caso del symlink depende de que el sistema
    permita crear enlaces, y en Windows eso pide un privilegio que una máquina de desarrollo
    normalmente no tiene.
    """

    objetivo = tmp_path / "run.json"
    objetivo.write_text('{"status":"completado"}', encoding="utf-8")
    sustituto = tmp_path / "sustituto.json"
    sustituto.write_text('{"status":"inyectado"}', encoding="utf-8")

    abrir_real = os.open

    def abrir_que_sustituye(ruta: Any, flags: int, modo: int = 0o777) -> int:
        if Path(ruta) == objetivo:
            os.replace(sustituto, objetivo)
        return abrir_real(ruta, flags, modo)

    monkeypatch.setattr(os, "open", abrir_que_sustituye)

    with pytest.raises(SandboxOutputError, match="cambió durante la lectura"):
        leer_texto_protegido(objetivo, "run.json")


def test_la_lectura_rechaza_un_directorio_que_hace_de_fichero(tmp_path: Path) -> None:
    """Un directorio no es un artefacto, y leerlo no puede «salir vacio y continuar»."""

    (tmp_path / "directorio.json").mkdir()

    with pytest.raises(SandboxOutputError, match="no es un archivo regular"):
        leer_texto_protegido(tmp_path / "directorio.json", "run.json")


def test_la_lectura_rechaza_un_fichero_que_supera_el_techo(tmp_path: Path) -> None:
    """El techo de tamaño se respeta, y por defecto sale de la configuración.

    `max_bytes` es un parámetro solo para poder probar el techo: `Settings` es **congelado** a
    propósito —es la garantía de la que se sirve `llm_router.client`— y destrabarlo con
    `object.__setattr__` en una prueba sería el truco que esa inmutabilidad existe para evitar.
    """

    grande = tmp_path / "grande.json"
    grande.write_text("x" * 2048, encoding="utf-8")

    with pytest.raises(SandboxOutputError, match="tamaño máximo"):
        leer_texto_protegido(grande, "run.json", max_bytes=128)

    assert len(leer_texto_protegido(grande, "grande.json")) == 2048, (
        "con el techo por defecto el mismo fichero se lee: la defensa es el techo, no el fichero"
    )


def test_la_lectura_rechaza_un_fichero_que_no_es_utf8(tmp_path: Path) -> None:
    """La decodificación es estricta: un binario no se pasa por texto."""

    binario = tmp_path / "binario.json"
    binario.write_bytes(b"\xff\xfe\x00status")

    with pytest.raises(SandboxOutputError, match="UTF-8"):
        leer_texto_protegido(binario, "run.json")


def test_el_artefacto_ilegible_se_reporta_como_artefacto_y_no_como_json_roto(
    tmp_path: Path,
) -> None:
    """Un `run.json` que no es JSON es un artefacto ilegible, con su propio tipo de error."""

    destino = _workspace_con_run(tmp_path)
    (destino / "run.json").write_text("{esto no es json", encoding="utf-8")

    with pytest.raises(SandboxOutputError, match="JSON válido"):
        leer_ejecucion(tmp_path)


def test_un_json_que_no_es_objeto_no_es_un_run(tmp_path: Path) -> None:
    """Una lista donde se espera un objeto no se reinterpreta: se rechaza."""

    destino = _workspace_con_run(tmp_path)
    (destino / "run.json").write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(SandboxOutputError, match="objeto JSON"):
        leer_ejecucion(tmp_path)
