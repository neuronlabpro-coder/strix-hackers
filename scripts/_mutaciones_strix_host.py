"""Mutaciones que demuestran que las pruebas del parser y del modo host **no están vacías**.

## Por qué existe

Porque un test verde que nunca ha caído no demuestra nada: demuestra que el test y el código
expresan la misma idea equivocada, y eso se lee como una garantía. La forma de romper esa
simetría es devolver el código al defecto y mirar cómo cae la prueba.

Cada mutación es **una línea** en un fichero del árbol: se aplica, se ejecutan las pruebas que la
deben mirar y se comprueba que **fallan**. Si alguna pasa, el contrato que la prueba dice defender
no está realmente en el código.

## Cómo se ejecuta

    uv run --project backend python scripts/_mutaciones_strix_host.py

Sale con código 1 si alguna mutación **no** hizo caer su prueba. No modifica el árbol al
terminar: cada mutación se deshace en un `finally`, porque un script de demostración que deja el
código cambiado es peor que no tener el script.

## La única mutación que no se puede demostrar aquí

La de `O_NOFOLLOW` necesita crear un enlace simbólico, y en Windows eso pide un privilegio que
una máquina de desarrollo normalmente no tiene. La defence que **sí** se demuestra en todas las
máquinas es la comparación de `st_dev`/`st_ino` entre el `stat` y el `open`
(`MUTACION_INODO`), porque es la que cubre la ventana que `O_NOFOLLOW` no cubre.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARTEFACTOS = RAIZ / "backend" / "workers" / "runner" / "strix_artefactos.py"
HOST = RAIZ / "backend" / "workers" / "runner" / "host.py"
TAREAS = RAIZ / "backend" / "workers" / "tasks.py"


@dataclass(frozen=True, slots=True)
class Mutacion:
    """Una mutación, las pruebas que la deben mirar, y por qué importa."""

    nombre: str
    fichero: Path
    original: str
    mutado: str
    pruebas: tuple[str, ...]
    porque: str


MUTACIONES: tuple[Mutacion, ...] = (
    Mutacion(
        nombre="cobertura-por-defecto",
        fichero=ARTEFACTOS,
        original=(
            "    kind = _texto(resultado.get(\"kind\")) or KIND_POR_DEFECTO\n"
            "    if kind in TIPOS_DE_COBERTURA:\n"
            "        return True\n"
            "    rule_id = _texto(resultado.get(\"ruleId\")) or \"\"\n"
            "    if rule_id.startswith(PREFIJO_REGLA_COBERTURA):\n"
            "        return True\n"
            "    strix = _bloque(_bloque(resultado.get(\"properties\")).get(\"strix\"))\n"
            "    return \"coverage_outcome\" in strix"
        ),
        mutado=(
            "    kind = _texto(resultado.get(\"kind\")) or KIND_POR_DEFECTO\n"
            "    return kind == \"pass\""
        ),
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_el_run_de_referencia_da_cero_hallazgos_y_dieciocho_registros_de_cobertura",
            "backend/tests/test_strix_artefactos.py::test_la_fila_open_del_sarif_de_referencia_es_cobertura_y_no_una_vulnerabilidad",
        ),
        porque=(
            "el discriminador ingenuo, tal como lo.describe el encargo: todo lo que no es `pass` "
            "es un hallazgo. Con esta mutación el run de referencia devuelve **1 hallazgo** —la "
            "fila `needs_follow_up`— y 17 registros de cobertura, que es una vulnerabilidad que "
            "el motor nunca encontró"
        ),
    ),
    Mutacion(
        nombre="sin-comprobar-el-estado-del-run",
        fichero=ARTEFACTOS,
        original='    if status != "completed":',
        mutado='    if False:',
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_un_status_distinto_de_completed_es_un_fallo_y_no_un_escaneo_limpio",
            "backend/tests/test_strix_artefactos.py::test_un_run_en_curso_tampoco_es_un_escaneo_limpio",
        ),
        porque=(
            "sin esta comprobación, un `run.json` que dice `failed` se leería como un escaneo "
            "terminado y el panel publicaría un escaneo que no ocurrió"
        ),
    ),
    Mutacion(
        nombre="severidad-de-relleno-informativa",
        fichero=ARTEFACTOS,
        original="    return SeverityEnum.MEDIUM, PUNTUACION_POR_SEVERIDAD[SeverityEnum.MEDIUM], False",
        mutado="    return SeverityEnum.INFO, PUNTUACION_POR_SEVERIDAD[SeverityEnum.INFO], False",
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_un_hallazgo_sin_severidad_cae_en_medium_y_no_inventa_una_puntuacion",
        ),
        porque=(
            "`INFO` escondería un hallazgo que el motor decidió registrar: en un panel cuyo "
            "recuento es riesgo vivo, desaparecería de la postura"
        ),
    ),
    Mutacion(
        nombre="sin-comparar-el-inodo",
        fichero=ARTEFACTOS,
        original=(
            "            if (path_stat.st_dev, path_stat.st_ino) != (abierto_stat.st_dev, abierto_stat.st_ino):\n"
            "                raise SandboxOutputError(f\"{etiqueta} cambió durante la lectura\")"
        ),
        mutado="            if False:\n                raise SandboxOutputError(f\"{etiqueta} cambió durante la lectura\")",
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_la_lectura_rechaza_un_fichero_sustituido_entre_el_stat_y_el_open",
        ),
        porque=(
            "la comparación de `st_dev`/`st_ino` es lo que detecta la sustitución **entre** el "
            "`stat` y el `open`: sin ella se lee lo que el atacante puso"
        ),
    ),
    Mutacion(
        nombre="sin-techo-de-tamano",
        fichero=ARTEFACTOS,
        original="    if len(datos) > techo:",
        mutado="    if False:",
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_la_lectura_rechaza_un_fichero_que_supera_el_techo",
        ),
        porque="un artefacto gigante se leería entero en memoria; el techo es lo que lo impide",
    ),
    Mutacion(
        nombre="utf8-permisivo",
        fichero=ARTEFACTOS,
        original='        return datos.decode("utf-8")',
        mutado='        return datos.decode("utf-8", errors="replace")',
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_la_lectura_rechaza_un_fichero_que_no_es_utf8",
        ),
        porque=(
            "con `replace`, un binario se pasa por texto y el `json.loads` falla después con un "
            "error que no dice qué ha pasado"
        ),
    ),
    Mutacion(
        nombre="el-run-mas-antiguo-por-mtime",
        fichero=ARTEFACTOS,
        original="        elegido = max(con_fecha, key=lambda par: par[0])[1]",
        mutado="        elegido = max(candidatos, key=lambda d: (d / FICHERO_RUN).stat().st_mtime)",
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_con_varios_runs_gana_el_mas_reciente_por_start_time_y_no_por_mtime",
        ),
        porque=(
            "elegir por `mtime` del fichero elige la copia más reciente del fichero, no el run más "
            "reciente: son cosas distintas y la segunda es la que responde al escaneo"
        ),
    ),
    Mutacion(
        nombre="tokens-repartidos-del-total",
        fichero=ARTEFACTOS,
        original='    entrada = _entero(uso.get("input_tokens"))\n    salida = _entero(uso.get("output_tokens"))',
        mutado=(
            "    entrada = _entero(uso.get(\"total_tokens\"))\n"
            "    salida = 0 if entrada is None else entrada // 2"
        ),
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_el_consumo_se_desglosa_desde_la_raiz_y_no_desde_los_proveedores",
        ),
        porque="repartir el total entre entrada y salida es un cargo inventado: los precios son distintos",
    ),
    Mutacion(
        nombre="nada-de-cobertura-es-cero",
        fichero=ARTEFACTOS,
        original="    if not isinstance(runs, list) or not runs:\n        raise SandboxOutputError(\"El SARIF no contiene ningún run\")",
        mutado="    if not isinstance(runs, list) or not runs:\n        return [], {}",
        pruebas=(
            "backend/tests/test_strix_artefactos.py::test_un_sarif_sin_runs_es_un_artefacto_ilegible_y_no_un_escaneo_vacio",
        ),
        porque=(
            "un SARIF sin `runs` es un fichero de otra cosa; devolver cero convertiría un "
            "artefacto ilegible en un escaneo que no encontró nada"
        ),
    ),
    Mutacion(
        nombre="comando-con-run-name",
        fichero=HOST,
        original='            "--scan-mode",\n            self.scan_mode,\n        ]',
        mutado='            "--scan-mode",\n            self.scan_mode,\n            "--run-name",\n            self.run_name,\n        ]',
        pruebas=(
            "backend/tests/test_strix_host_runner.py::test_el_comando_del_modo_host_no_pide_run_name_ni_output",
        ),
        porque=(
            "`--run-name` no existe: `-r` es `--resume` y combinarla con `--target` es error. "
            "El nombre del run lo pone el motor"
        ),
    ),
    Mutacion(
        nombre="nunca-matar-el-arbol",
        fichero=HOST,
        original=(
            "    def _raise_timeout(self) -> None:\n"
            "        self.matar_arbol(self.process)\n"
            "        raise SandboxTimeoutError(\"Strix superó el timeout de ejecución\")"
        ),
        mutado=(
            "    def _raise_timeout(self) -> None:\n"
            "        raise SandboxTimeoutError(\"Strix superó el timeout de ejecución\")"
        ),
        pruebas=(
            "backend/tests/test_strix_host_runner.py::test_el_timeout_duro_mata_el_arbol_y_deja_el_run_terminado",
        ),
        porque=(
            "sin matar al agotarse el timeout, el proceso del motor —y las herramientas que él "
            "lanzó— siguen vivos cobrando tokens del proveedor con el run ya terminal. Es el peor "
            "fallo posible de este modo, y por eso es el que la prueba mira"
        ),
    ),
    Mutacion(
        nombre="workspace-sin-purgar",
        fichero=HOST,
        original="            shutil.rmtree(temp_dir, ignore_errors=True)\n            if temp_dir.exists():",
        mutado="            pass\n            if temp_dir.exists():",
        pruebas=(
            "backend/tests/test_strix_host_runner.py::test_el_modo_host_lee_los_artefactos_y_purga_el_workspace",
            "backend/tests/test_strix_host_runner.py::test_el_workspace_se_purga_aunque_el_motor_no_deje_artefactos",
        ),
        porque="R5: el workspace es efímero y se purga siempre, también cuando el escaneo falla",
    ),
    Mutacion(
        nombre="buscar-el-contrato-falso",
        fichero=HOST,
        original="            ejecucion = leer_ejecucion(workspace_dir)",
        mutado=(
            "            salida_json = leer_texto_protegido(workspace_dir / 'output' / 'results.json', 'results.json')\n"
            "            del salida_json\n"
            "            ejecucion = leer_ejecucion(workspace_dir)"
        ),
        pruebas=(
            "backend/tests/test_strix_host_runner.py::test_el_modo_host_no_busca_ningun_results_json",
        ),
        porque=(
            "volver a leer `results.json` es volver al contrato que el motor nunca escribió; con "
            "esta mutación el modo host falla en cuanto existe un `results.json` en el workspace"
        ),
    ),
    Mutacion(
        nombre="cobertura-que-no-llega-al-panel",
        fichero=TAREAS,
        original="    run.coverage = ejecucion.cobertura.a_json() if ejecucion.cobertura is not None else None",
        mutado="    run.coverage = None",
        pruebas=(
            "backend/tests/test_strix_host_persistencia.py::test_un_escaneo_sin_hallazgos_completa_el_run_y_guarda_su_cobertura",
            "backend/tests/test_strix_host_persistencia.py::test_los_artefactos_de_un_run_no_pueden_tocar_el_de_otro_tenant",
        ),
        porque=(
            "sin `coverage`, el panel no puede distinguir «se escaneó y no había nada» de «el motor "
            "no pudo revisar una superficie»: son dos filas idénticas en `vulnerabilities`"
        ),
    ),
    Mutacion(
        nombre="reingesta-que-sobrescribe-la-evidencia",
        fichero=TAREAS,
        original="    if await _mismos_hallazgos_ingeridos(session, run, hallazgos):\n        await session.commit()\n        return len(hallazgos)",
        mutado="    if False:\n        await session.commit()\n        return len(hallazgos)",
        pruebas=(
            "backend/tests/test_strix_host_persistencia.py::test_una_reingesta_con_evidencia_distinta_se_rechaza",
        ),
        porque=(
            "una reingesta con contenido distinto reescribiría `vulnerabilities`, y R4 dice que la "
            "evidencia del escaneo es inmutable"
        ),
    ),
    Mutacion(
        nombre="ingesta-que-tapa-un-fallo",
        fichero=TAREAS,
        original='    if run.status not in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:\n        raise StrixIngestionError("El run no está en un estado susceptible de ingesta")\n\n    session.add_all(hallazgos)',
        mutado="    session.add_all(hallazgos)",
        pruebas=(
            "backend/tests/test_strix_host_persistencia.py::test_el_run_no_se_puede_ingerir_tras_un_estado_terminal_distinto",
        ),
        porque=(
            "sin la guarda, un run que ya estaba en `FAILED` se cerraría como `COMPLETED` porque "
            "llegaron artefactos: el fallo desaparece y el panel enseña un escaneo que no pasó"
        ),
    ),
)


def _probar(pruebas: tuple[str, ...]) -> int:
    comando = [
        "uv",
        "run",
        "--project",
        "backend",
        "pytest",
        "-c",
        "backend/pyproject.toml",
        *pruebas,
        "-q",
        "--tb=no",
    ]
    completada = subprocess.run(  # noqa: S603
        comando,
        cwd=RAIZ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completada.returncode


def aplicar(mutacion: Mutacion) -> None:
    contenido = mutacion.fichero.read_text(encoding="utf-8")
    if mutacion.original not in contenido:
        raise SystemExit(
            f"La mutación «{mutacion.nombre}» ya no aplica: el texto original no está en "
            f"{mutacion.fichero.name}. Alguien cambió el código y la mutación quedó vieja."
        )
    mutacion.fichero.write_text(
        contenido.replace(mutacion.original, mutacion.mutado, 1),
        encoding="utf-8",
        newline="",
    )


def main() -> int:
    print(f"{len(MUTACIONES)} mutaciones. Cada una tiene que hacer CAER su prueba.\n")
    supervivientes: list[str] = []
    for mutacion in MUTACIONES:
        try:
            aplicar(mutacion)
            codigo = _probar(mutacion.pruebas)
        finally:
            # Se deshace siempre: un script de demostración que deja el árbol cambiado es peor
            # que no tener el script, y si una mutación cuelga el árbol tiene que volver a estar
            # como estaba.
            _deshacer(mutacion)
        if codigo == 0:
            supervivientes.append(mutacion.nombre)
            estado = "NO CAE  <-- la prueba está vacía"
        else:
            estado = f"cae (rc={codigo})"
        print(f"  · {mutacion.nombre:34} {estado}")
        print(f"      {mutacion.porque}")

    print()
    if supervivientes:
        print(f"FALLAN {len(supervivientes)} mutaciones: {supervivientes}")
        print("Esas pruebas no miran lo que dicen mirar.")
        return 1
    print(f"Las {len(MUTACIONES)} mutaciones caen. Las pruebas no están vacías.")
    return 0


def _deshacer(mutacion: Mutacion) -> None:
    contenido = mutacion.fichero.read_text(encoding="utf-8")
    if mutacion.mutado not in contenido:
        raise SystemExit(
            f"La mutación «{mutacion.nombre}» no se puede deshacer: el árbol cambió mientras "
            f"corría. Revisa {mutacion.fichero.name} antes de tocar nada más."
        )
    mutacion.fichero.write_text(
        contenido.replace(mutacion.mutado, mutacion.original, 1),
        encoding="utf-8",
        newline="",
    )


if __name__ == "__main__":
    sys.exit(main())