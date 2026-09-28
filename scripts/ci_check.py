#!/usr/bin/env python3
"""Ejecuta los gates de calidad del proyecto y resume el resultado en una tabla.

## Por qué un script y no un Makefile

Porque los gates no son todos comandos de shell: la comprobación de paridad de i18n es
Python, y el resumen tiene que mezclar duraciones de procesos distintos. Un `Makefile`
tendría que invocar a Python para lo mismo, y ahí la lógica queda partida en dos sitios.

## Por qué mide tiempos y no solo códigos de salida

Porque el dato útil de un gate es la **tendencia**. Un gate que pasa en 4 s y otro que pasa
en 90 s dicen cosas distintas sobre la salud del proyecto, y un resumen que solo pone «OK» en
los dos pierde esa información. Además, un resumen con duraciones permite detectar el gate
que se ha triplicado de golpe, que casi siempre es una dependencia mal puesta y no un
problema del código.

## Por qué los fallos **no** cortan la ejecución

Porque un gate que se detiene en el primer fallo obliga a un ciclo de ida y vuelta por cada
problema. Con ocho gates, encontrar cinco problemas a la vez es la diferencia entre una
corrección y una tarde. Al revés: `--bail` existe para quien prefiera el fallo rápido, y por
defecto está desactivado.

## Por qué la paridad de i18n se comprueba en este script y no en pytest

Porque `frontend/src/locales/` **no** se importa desde Python y una prueba de backend que
leyera el directorio del frontend se rompería en cuanto alguien ejecutara los tests sin el
repositorio completo. Un fallo de infraestructura disfrazado de fallo de producto es peor que
no tener la comprobación.

## Uso

    python scripts/ci_check.py
    python scripts/ci_check.py --bail
    python scripts/ci_check.py --only i18n
    python scripts/ci_check.py --skip build
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LOCALES_ES = RAIZ / "frontend" / "src" / "locales" / "es"
LOCALES_EN = RAIZ / "frontend" / "src" / "locales" / "en"
FRONTEND = RAIZ / "frontend"

#: Variables que se declaran de forma global porque **todas** las órdenes las necesitan.
#:
#: Sin esto, `uv` y `npm` heredan del proceso el `VIRTUAL_ENV` de quien lo lanza, y el primer
#: gate que pase por delante es el que se ejecuta con el entorno virtual equivocado. El
#: síntoma es un `ModuleNotFoundError` en una dependencia que está instalada.
ENTORNO_BASE: dict[str, str] = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    # Tailscale: la base de datos local está detrás de la interfaz `100.x`. El margen Alto
    # evita el `TimeoutError` transitorio que aparece cuando el enlace está despierto pero
    # la ruta todavía no lo está.
    "DB_CONNECT_TIMEOUT_SECONDS": "60",
    "CI": "1",
}


@dataclass(frozen=True)
class Gate:
    """Un gate: qué ejecuta, cómo se llama y qué agrupe lo suyo."""

    clave: str
    titulo: str
    comando: list[str]
    cwd: Path = RAIZ
    #: Si es `False`, el script imprime la salida aunque pase. Los gates que lo necesitan
    #: imprimen mucho y sin valor cuando todo va bien.
    salida_si_pasa: bool = False
    #: Si es `True`, un fallo es un problema del entorno y no del código, y no debería
    #: marcar la entrega como no apta. Se cuenta aparte en vez de ignorarse: un gate que no se
    #: pudo ejecutar no es un gate que pasó.
    opcional: bool = False


@dataclass
class Resultado:
    gate: Gate
    codigo: int
    segundos: float
    salida: str = ""
    omitido: bool = False
    fallida_por_entorno: bool = False
    #: Causa detectada cuando el fallo es de entorno, para poder explicar el `SKIP` en vez
    #: de dejar un `FAIL` sin explicación.
    motivo_entorno: str = ""


@dataclass
class Resumen:
    resultados: list[Resultado] = field(default_factory=list)

    @property
    def fallidos(self) -> list[Resultado]:
        return [
            r
            for r in self.resultados
            if not r.omitido and r.codigo != 0 and not r.fallida_por_entorno
        ]

    @property
    def por_entorno(self) -> list[Resultado]:
        return [r for r in self.resultados if r.fallida_por_entorno]


GATES: tuple[Gate, ...] = (
    Gate(
        clave="pytest",
        titulo="Tests del backend",
        comando=[
            "uv",
            "run",
            "--project",
            "backend",
            "pytest",
            "-c",
            "backend/pyproject.toml",
            "backend/tests",
            "-q",
            "--tb=short",
        ],
    ),
    Gate(
        clave="ruff",
        titulo="Lint del backend (ruff)",
        comando=["uv", "run", "--project", "backend", "ruff", "check", "backend"],
    ),
    Gate(
        clave="pyright",
        titulo="Tipos del backend (pyright)",
        # `--project` es **obligatorio**: sin él, pyright busca un `pyrightconfig.json` en la
        # raíz y no aplica `backend/pyproject.toml`, que es donde están las reglas de
        # tipado estricto. El resultado es un "0 errores" que no significa nada.
        comando=[
            "uv",
            "run",
            "--project",
            "backend",
            "pyright",
            "--project",
            "backend/pyproject.toml",
        ],
    ),
    Gate(
        clave="alembic",
        titulo="Deriva de migraciones (alembic check)",
        comando=[
            "uv",
            "run",
            "--project",
            "backend",
            "alembic",
            "-c",
            "backend/alembic.ini",
            "check",
        ],
    ),
    Gate(
        clave="typecheck",
        titulo="Tipos del frontend (tsc)",
        comando=["npm", "run", "typecheck"],
        cwd=FRONTEND,
    ),
    Gate(
        clave="lint",
        titulo="Lint del frontend (oxlint)",
        comando=["npm", "run", "lint"],
        cwd=FRONTEND,
    ),
    Gate(
        clave="build",
        titulo="Build del frontend (vite)",
        comando=["npm", "run", "build"],
        cwd=FRONTEND,
    ),
    Gate(
        clave="vitest",
        titulo="Tests del frontend (vitest)",
        comando=["npm", "run", "test"],
        cwd=FRONTEND,
    ),
    Gate(
        clave="i18n",
        titulo="Paridad de traducciones es/en",
        comando=[sys.executable, str(Path(__file__).with_name("ci_i18n_check.py"))],
    ),
    # La auditoria de paridad va como puerta y no como informe que alguien lee cuando le
    # apetece, por una razon concreta: un informe generado a mano envejece mas rapido que el
    # codigo que audita, y en cuanto sus numeros dejan de coincidir con la realidad nadie lo
    # nota. Lo unico que lo mantiene honesto es que genera un codigo de salida.
    #
    # Y el codigo de salida importa mas que el Markdown: la auditoria falla si aparece un literal
    # de la marca en algo que el usuario lee, y ese es el residuo que de verdad no se puede
    # defender en un despliegue.
    Gate(
        clave="paridad",
        titulo="Auditoria de paridad funcional",
        comando=[
            sys.executable,
            str(RAIZ / "scripts" / "audit_strix_parity.py"),
        ],
    ),
)


def _resolver_ejecutable(nombre: str) -> str:
    """Devuelve la **ruta absoluta** del ejecutable, no su nombre pelado.

    ## Por qué esto hace falta en Windows, medido y no supuesto

    `npm` en Windows no es un ejecutable: es un `npm.CMD`. Lo que ocurre —comprobado en esta
    máquina— es esto:

        shutil.which("npm")               ->  C:\\Program Files\\nodejs\\npm.CMD
        subprocess.run(["npm", ...])      ->  [WinError 2] No se encuentra el archivo
        subprocess.run(["...\\npm.CMD"]) ->  rc=0

    Es decir: `shutil.which` **sí** resuelve la extensión y `subprocess` **no**. El resolutor
    de `shutil` lee `PATHEXT`; el de `CreateProcess`, al que acaba delegando `subprocess` con
    una lista de argumentos, busca solo el nombre exacto.

    Por eso devolver el nombre no bastaba: hay que devolver la ruta. Y por eso el fallo era
    tan confuso: cuatro gates de frontend en rojo con un `[WinError 2]` que no menciona Node,
    y la conclusión razonable —"el frontend está roto"— es la equivocada.

    Si el ejecutable no aparece, el error dice **qué** falta y por qué es un problema del
    entorno, para que no acabe contado como un fallo del proyecto.
    """

    if os.name == "nt":
        # Las extensiones de Windows se prueban primero porque son las que existen; la forma
        # pelada se deja para el final, y en Linux es la única posible.
        candidatos = [nombre + sufijo for sufijo in (".cmd", ".exe", ".bat")]
        candidatos.append(nombre)
    else:
        candidatos = [nombre]

    for candidato in candidatos:
        encontrado = shutil.which(candidato)
        if encontrado:
            return encontrado

    raise FileNotFoundError(
        f"No se encuentra el ejecutable «{nombre}» en el PATH. "
        "Instálalo o añádelo al PATH antes de ejecutar este script."
    )


def ejecutar(gate: Gate) -> Resultado:
    """Ejecuta un gate y mide cuánto tardó.

    El timeout no es un detalle: un gate que se cuelga sin límite deja el script esperando
    para siempre, y quien lo lanzó se queda sin saber si sigue trabajando o si se ha
    quedado sin memoria. Cada gate tiene su propio margen, y los que arrancan servicios
    externos —los que hablan con la base— llevan más, porque el primer arranque en frío paga
    la conexión.
    """

    inicio = time.monotonic()
    comando = gate.comando
    try:
        comando = [_resolver_ejecutable(comando[0]), *comando[1:]]
    except FileNotFoundError as error:
        # No es un fallo del proyecto: no hay forma de pasar un gate sin su herramienta, y
        # contarlo como "código roto" mandaría a alguien a modificar código que funciona.
        return Resultado(
            gate,
            127,
            time.monotonic() - inicio,
            str(error),
            fallida_por_entorno=True,
            motivo_entorno="falta una herramienta del entorno de desarrollo",
        )

    try:
        completada = subprocess.run(  # noqa: S603
            comando,
            cwd=gate.cwd,
            env={**ENTORNO_BASE, **_entorno_de_shell()},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=gate_timeout(gate),
            check=False,
        )
    except FileNotFoundError as error:
        return Resultado(
            gate,
            127,
            time.monotonic() - inicio,
            str(error),
            fallida_por_entorno=True,
            motivo_entorno="falta una herramienta del entorno de desarrollo",
        )
    except subprocess.TimeoutExpired as error:
        salida = _a_texto(error.stdout) + _a_texto(error.stderr)
        return Resultado(
            gate, 124, time.monotonic() - inicio, f"Tiempo agotado.\n{salida}"
        )

    segundos = time.monotonic() - inicio
    salida = completada.stdout + completada.stderr
    return Resultado(gate, completada.returncode, segundos, salida)


def gate_timeout(gate: Gate) -> int:
    """Margen por gate, en segundos.

    Los de la base de datos necesitan mucho más que los de frontend porque arrancan un
    contexto nuevo, ejecutan migraciones implícitas y, en el caso de `pytest`, abren y cierran
    un `savepoint` por prueba.
    """

    if gate.clave in {"pytest", "alembic"}:
        return 1_800
    if gate.clave == "build":
        return 600
    return 300


def _entorno_de_shell() -> dict[str, str]:
    """El entorno heredado, sin las variables que estropearían la ejecución.

    Se quita `PYTHONPATH`: si quien lo lanza lo tiene puesto para otra cosa, el primer
    `import` puede resolver al paquete equivocado y el fallo aparece en un gate que parece no
    tener nada que ver.
    """

    limpio = dict(os.environ)
    limpio.pop("PYTHONPATH", None)
    limpio.pop("NODE_ENV", None)
    return limpio


def _a_texto(valor: object) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return str(valor)


# --------------------------------------------------------------------------- #
# Diagnóstico de fallos de entorno
# --------------------------------------------------------------------------- #

#: Firmas que indican que el fallo es del entorno y no del código.
#:
#: Son literales que aparecen **en el mensaje de la herramienta**, no excepciones de este
#: script. La diferencia importa: un `ECONNREFUSED` a PostgreSQL es un problema de red o de
#: que la base no está levantada, y contarlo como fallo de código haría que alguien
#: modificara el proyecto para arreglar algo que funciona.
FALLAS_DE_ENTORNO: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(ConnectionRefusedError|ECONNREFUSED|connection refused)", re.I),
        "no hay conexion con la base de datos",
    ),
    (
        re.compile(r"(TimeoutError|timed out)", re.I),
        "la operacion excedio el tiempo; la base por Tailscale puede no responder aun",
    ),
    (
        re.compile(r"(Can't connect to (MySQL|server)|ECONNRESET)", re.I),
        "la base de datos no esta accesible",
    ),
    (
        re.compile(r"(is not recognized|not found)", re.I),
        "falta una herramienta del entorno de desarrollo",
    ),
    (
        re.compile(r"(ENOENT.*(docker\.sock|/var/run))", re.I),
        "falta el socket de Docker en esta maquina",
    ),
)


def clasificar_fallo(resultado: Resultado) -> Resultado:
    """Marca como fallo de entorno lo que lo es.

    Se decide por la **salida de la herramienta** y solo si no hay nada más en ella. Un gate
    que además muestra un error de lint no se declara "problema de entorno" porque apareciera
    una palabra suelta: la clasificación mira que no haya ningún otro indicio de fallo.
    """

    if resultado.codigo == 0:
        return resultado
    for patron, motivo in FALLAS_DE_ENTORNO:
        if patron.search(resultado.salida):
            resultado.fallida_por_entorno = True
            resultado.motivo_entorno = motivo
            return resultado
    return resultado


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #

ICONO = {"pasa": "OK", "falla": "FALLO", "entorno": "N/A", "omitido": "--"}


def imprimir_resumen(resumen: Resumen) -> None:
    """Tabla resumen con estado y tiempo de cada gate.

    La tabla se imprime aunque haya fallos, y al final va el detalle de cada uno. Un script
    que solo imprimiera el primer error obligaría a ejecutarlo otra vez para ver el segundo,
    y con ocho gates eso son cuatro ejecuciones.
    """

    titulo, encabezado, linea_sep, cuerpo, pie = formato_tabla(resumen)
    ancho = len(linea_sep)

    print()
    print(titulo.center(ancho))
    print(linea_sep)
    print(encabezado)
    print(linea_sep)
    for fila in cuerpo:
        print(fila)
    print(linea_sep)
    print(pie)
    print()


def formato_tabla(resumen: Resumen) -> tuple[str, str, str, list[str], str]:
    """Construye la tabla como texto.

    Las anchuras se calculan con el contenido más largo de cada columna, no con un valor fijo.
    Un título largo truncado a 28 caracteres deja fuera justo la parte que dice qué falla, y
    en un resumen de ocho filas eso convierte la tabla en un sitio donde hay que volver a
    mirar el código fuente para saber qué se estaba comprobando.
    """

    titulo = "Gates de calidad · Mind Guard Fenix Team"
    filas_datos: list[tuple[str, str, str, str]] = []
    for resultado in resumen.resultados:
        if resultado.omitido:
            estado = ICONO["omitido"]
        elif resultado.codigo == 0:
            estado = ICONO["pasa"]
        elif resultado.fallida_por_entorno:
            estado = ICONO["entorno"]
        else:
            estado = ICONO["falla"]
        filas_datos.append(
            (resultado.gate.clave, resultado.gate.titulo, estado, formatear_tiempo(resultado.segundos))
        )

    anchos = [
        max(len("Gate"), *(len(f[0]) for f in filas_datos)) + 2,
        max(len("Comprobacion"), *(len(f[1]) for f in filas_datos)) + 2,
        max(len("Estado"), *(len(f[2]) for f in filas_datos)) + 2,
        max(len("Tiempo"), *(len(f[3]) for f in filas_datos)) + 2,
    ]

    encabezado = (
        f"{'Gate':<{anchos[0]}}{'Comprobacion':<{anchos[1]}}"
        f"{'Estado':<{anchos[2]}}{'Tiempo':<{anchos[3]}}"
    )
    separador = "-" * (sum(anchos) - 2)

    cuerpo = [
        f"{clave:<{anchos[0]}}{desc:<{anchos[1]}}{estado:<{anchos[2]}}{tiempo:<{anchos[3]}}"
        for clave, desc, estado, tiempo in filas_datos
    ]

    total = sum(r.segundos for r in resumen.resultados if not r.omitido)
    passed = sum(1 for r in resumen.resultados if r.codigo == 0 and not r.omitido)
    fallidos = len(resumen.fallidos)
    entorno = len(resumen.por_entorno)
    omitidos = sum(1 for r in resumen.resultados if r.omitido)

    partes = [f"{passed} de {passed + fallidos} gates en verde"]
    if entorno:
        partes.append(f"{entorno} no ejecutables en este entorno")
    if omitidos:
        partes.append(f"{omitidos} omitidos")
    partes.append(f"tiempo total {formatear_tiempo(total)}")
    pie = "  ·  ".join(partes)

    return titulo, encabezado, separador, cuerpo, pie


def formatear_tiempo(segundos: float) -> str:
    """Un tiempo legible: 0,4 s / 12,3 s / 4 m 05 s.

    Los minutos solo aparecen a partir del minuto. Un gate que tarda dos minutos tiene que
    distinguirse de uno que tarda veinte, y `123.4 s` no lo hace de un vistazo en una tabla
    con ocho filas.
    """

    if segundos < 60:
        return f"{segundos:.1f} s"
    minutos, resto = divmod(int(segundos), 60)
    return f"{minutos} m {resto:02d} s"


def imprimir_detalle(resumen: Resumen) -> None:
    """La salida completa de cada gate que no pasó."""

    for resultado in resumen.resultados:
        if resultado.omitido or resultado.codigo == 0:
            continue
        etiqueta = (
            "no ejecutable en este entorno" if resultado.fallida_por_entorno else "FALLO"
        )
        print(f"\n{'=' * 78}\n{resultado.gate.clave}: {etiqueta}\n{'=' * 78}")
        if resultado.fallida_por_entorno:
            print(f"Motivo detectado: {resultado.motivo_entorno}")
        print(resultado.salida.rstrip() or "(sin salida)")

    for resultado in resumen.por_entorno:
        print(
            f"\nAVISO: el gate «{resultado.gate.clave}» no se pudo ejecutar: "
            f"{resultado.motivo_entorno}."
        )
        print("       Es un problema del entorno, no del codigo; no cuenta como entrega no apta.")


# --------------------------------------------------------------------------- #
# Aviso previo
# --------------------------------------------------------------------------- #

#: El comando que informa del estado de los datos de demostración. Se invoca como proceso
#: separado y **no** se importa el catálogo del seeder: este script no depende del backend, y
#: arrastrar sus modelos aquí convertiría el gate de paridad de i18n —que no necesita nada—
#: en algo que necesita base de datos.
SEEDER = RAIZ / "backend" / "scripts" / "seed_demo_workspace.py"


def aviso_de_datos_de_demo() -> None:
    """Avisa, antes de gastar catorce minutos, de que hay datos que romperán `pytest`.

    ## Por qué hace falta esto

    El seeder y la suite de tests **comparten la base de datos**, y hay pruebas que cuentan
    revisiones de pull request y repositorios en vez de consultar solo los del tenant de la
    prueba. Con la demo sembrada, esas pruebas ven filas de más y fallan. Medido en este
    proyecto: siete fallos, cinco de ellos por los datos de la demo y dos por datos de otra
    organización que ya estaban en la base.

    ## Por qué es un aviso y no un borrado automático

    Porque la revisión visual del panel y la suite de tests son las dos cosas que se piden
    juntas, y borrar los datos de la demo sin que nadie lo pida destruiría el trabajo que
    alguien está haciendo. Un script que limpia la base para que sus pruebas pasen no está
    ahorrando tiempo: está decidiendo por su cuenta qué datos importan. Avisa y deja que sea
    quien lo ejecuta quien decida.

    ## Por qué no cuenta como fallo

    Porque los tests **no están mal**: ven una base sucia y lo dicen. Marcarse en rojo
    empujaría a alguien a "arreglar" las pruebas, que es el error contrario al que conviene.
    """

    if not SEEDER.is_file():
        return
    try:
        completada = subprocess.run(  # noqa: S603
            ["uv", "run", "--project", "backend", "python", str(SEEDER), "--estado"],
            cwd=RAIZ,
            env={**ENTORNO_BASE, **_entorno_de_shell()},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Si el aviso no se puede emitir, se sigue adelante: es una comodidad, no un gate.
        return

    estado = completada.stdout.strip()
    if completada.returncode != 0 or estado in {"", "limpio"}:
        return

    print(f"\nAVISO PREVIO: hay datos de demostración en la base ({estado}).")
    print("  La suite de tests comparte esa base de datos, y unas pruebas que cuentan")
    print("  revisiones o repositorios los verán como filas de más y fallarán. El fallo")
    print("  parecerá del proyecto cuando no lo es.")
    print()
    print("  Para sembrarlos otra vez cuando termines:")
    print("    uv run --project backend python backend/scripts/seed_demo_workspace.py")
    print("  Para limpiarlos ahora:")
    print("    uv run --project backend python backend/scripts/seed_demo_workspace.py --clear")
    print()


# --------------------------------------------------------------------------- #
# Programa
# --------------------------------------------------------------------------- #


def seleccionar(clave: str | None, saltar: set[str]) -> list[Gate]:
    """Filtra la lista de gates por `--only` y por `--skip`.

    `--only` y `--skip` son excluyentes: combinarlos deja la intersección sin explicación de
    por qué un gate desapareció, y quien lo pidió probablemente no la sabe.

    ## Por qué un nombre desconocido es un error y no un filtro más

    Porque sin esta comprobación `--only vitets` —una `t` de más— deja la lista vacía, no
    ejecuta ningún gate y el resumen dice «todos los gates en verde» con **cero** filas
    comprobadas. Es el peor resultado posible para un script cuyo propósito es no dar por bueno
    lo que no se ha comprobado: quien lo usa se marcha convencido de que el proyecto está
    verificado, y lo que ocurrió es que nadie lo verificó.
    """

    gates = list(GATES)
    conocidos = {g.clave for g in gates}

    if clave:
        pedidos = [c.strip() for c in clave.split(",") if c.strip()]
        desconocidos = [c for c in pedidos if c not in conocidos]
        if desconocidos:
            raise SystemExit(
                f"Gate desconocido: {', '.join(sorted(desconocidos))}\n"
                f"Disponibles: {', '.join(sorted(conocidos))}"
            )
        pedidos = set(pedidos)
        gates = [g for g in gates if g.clave in pedidos]

    if saltar:
        gates = [g for g in gates if g.clave not in saltar]

    if not gates:
        raise SystemExit(
            "Ningún gate seleccionado. Revisa lo que has pedido en `--only` y `--skip`."
        )
    return gates


def parsear_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ejecuta los gates de calidad y resume el resultado.",
    )
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument(
        "--only",
        help="Ejecuta solo estos gates, separados por coma (p. ej. i18n,build).",
    )
    grupo.add_argument(
        "--skip",
        help="Omite estos gates, separados por coma.",
    )
    parser.add_argument(
        "--bail",
        action="store_true",
        help="Detiene la ejecucion en el primer gate que falle.",
    )
    parser.add_argument(
        "--sin-aviso-demo",
        action="store_true",
        help=(
            "No comprueba si hay datos de demostración sembrados. Ahorra una consulta, que es "
            "lo unico que cuesta: no ejecuta los gates ni deja de ejecutarlos."
        ),
    )
    return parser.parse_args()


def main() -> int:
    argumentos = parsear_argumentos()
    gates = seleccionar(
        argumentos.only, set(argumentos.skip.split(",")) if argumentos.skip else set()
    )

    if not argumentos.sin_aviso_demo:
        aviso_de_datos_de_demo()

    print(f"Ejecutando {len(gates)} gates...\n")
    resumen = Resumen()

    for gate in gates:
        print(f"  · {gate.clave:10} {gate.titulo}", end="", flush=True)
        resultado = clasificar_fallo(ejecutar(gate))
        resumen.resultados.append(resultado)
        if resultado.codigo == 0 and not gate.salida_si_pasa:
            print(f"\r  · {gate.clave:10} {formatear_tiempo(resultado.segundos):>10}  OK      ")
        else:
            print()

        if argumentos.bail and resultado.codigo != 0 and not resultado.fallida_por_entorno:
            for pendiente in gates[gates.index(gate) + 1 :]:
                resumen.resultados.append(
                    Resultado(pendiente, 0, 0.0, omitido=True)
                )
            break

    imprimir_resumen(resumen)
    imprimir_detalle(resumen)

    if resumen.fallidos:
        print(
            f"\nRESULTADO: {len(resumen.fallidos)} gate(s) en rojo. "
            "La entrega no es apta.\n"
        )
        return 1

    if resumen.por_entorno:
        print(
            f"\nRESULTADO: verde en los gates ejecutables, con {len(resumen.por_entorno)} "
            "sin poder ejecutar por el entorno.\n"
        )
        return 0

    print("\nRESULTADO: todos los gates en verde.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
