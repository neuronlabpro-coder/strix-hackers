"""Cancelar un escaneo en curso **mata el árbol de procesos**, con una prueba que lo hace de verdad.

## Por qué esta prueba y no una que solo mire el código

Porque el defecto que se vigila es silencioso. `matar_arbol` puede afirmar en su docstring que mata
el árbol entero y seguir matando solo al padre: los nietos —las herramientas que el motor lanza—
siguen vivos, siguen gastando tokens del proveedor y ocupan la máquina del worker, con el run ya
en estado terminal. Nadie lo ve: no hay logs, no hay eventos, y el panel dice que el escaneo se
abortó.

Por eso aquí **se lanzan procesos de verdad**, se forma un árbol de dos niveles y se comprueba que
los dos mueren. Un doble que devuelva `None` al Comprobaria tanto un árbol como un proceso: es
indistinguible del comportamiento correcto, y por eso no sirve.

## Por qué en esta máquina y no solo en POSIX

Porque el árbol se mata de forma distinta según el sistema: `killpg` en POSIX y `taskkill /T` en
Windows. Comprobar solo uno deja el otro sin vigilar, y este proyecto se ejecuta en los dos: el
worker de Dokploy es Linux y el modo host se ha probado en Windows. La prueba es **de la misma
familia** en ambos y cada aserción depende de lo que el sistema puede prometer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.workers.runner.host import PREFIJO_REFERENCIA_HOST, HostRunResult, StrixHostRunner

#: Cuánto se espera a que un proceso muerto desaparezca del sistema antes de declarar que lo está.
#:
#: `taskkill` y `killpg` son asíncronos en la práctica: devuelven cuando han enviado la señal, y el
#: proceso puede tardar unos milisegundos en salir del planificador. Un bucle corto con espera es
#: lo que hace que la prueba no sea una ruleta: afirmar «no existe ya mismo» en Windows daría un
#: falso negativo intermitente, y un test intermitente se acaba ignorando.
TIEMPO_MAXIMO_DE_MUERTE_MS = 5_000


def _vivo(pid: int) -> bool:
    """¿Existe todavía este PID?

    ## Por qué en Windows **no** se usa `os.kill(pid, 0)`

    Porque medido en esta máquina, Python 3.13 sobre Windows: `os.kill(pid, 0)` **no lanza** ni
    mata. Un proceso vivo sobrevive, y un proceso ya terminado por `taskkill` tampoco provoca
    excepción: el PID sigue siendo válido durante toda la sesión de la máquina y `os.kill` responde
    que sí. Solo lanza `OSError [WinError 87]` con un PID que nunca existió, como `999999`.

    Esa comprobación habría hecho que esta prueba **diera verde sin matar nada**: `_vivo` habría
    dicho «vivo» después del `taskkill`, el bucle de espera habría agotado el tiempo y la
    aserción habría caído por el motivo equivocado... o peor, si el margen hubiera sido corto,
    habría pasado sin comprobar nada. Un instrumento de medición que miente hace inútil la prueba
    que lo usa.

    ## Por qué `tasklist` y no un `ctypes`

    Porque `tasklist` es la herramienta del sistema y su salida es un CSV con el PID en el segundo
    campo, y leerla es una línea. Un `ctypes` a `OpenProcess` mediría mejor pero añadiría código
    nativo que puede romperse entre versiones de Python sin que nadie se entere: el fallo sería
    «el proceso sigue vivo», que es exactamente la clase de falso verde que aquí se quiere evitar.
    """

    if os.name == "nt":  # pragma: no cover - la bateria corre en el sistema que la ejecuta
        completado = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for linea in completado.stdout.splitlines():
            campos = linea.split('","')
            if len(campos) >= 2 and campos[1].strip().strip('"').isdigit():
                if int(campos[1].strip().strip('"')) == pid:
                    return True
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _esperar_a_morir(pid: int) -> bool:
    limite = time.monotonic() + TIEMPO_MAXIMO_DE_MUERTE_MS / 1000
    while time.monotonic() < limite:
        if not _vivo(pid):
            return True
        time.sleep(0.05)
    return not _vivo(pid)


#: El padre: lanza un nieto de verdad, espera a que esté vivo y se queda esperando.
#:
#: ## Por qué el nieto se lanza con `subprocess` y no dentro del mismo script
#:
#: Porque un árbol de dos niveles es lo que distingue «mató el proceso» de «mató el árbol». Con un
#: solo proceso, `taskkill /F` sin `/T` lo mata igual y la prueba daría verde con el defecto
#: puesto —que es exactamente lo que pasó en la primera versión de este fichero: la comprobación
#: del nieto usaba `tasklist /FI PID eq <padre>`, que devuelve el proceso **padre**, así que la
#: aserción se cumplía sin que hubiera ningún nieto que matar. Con `/T` quitado de `taskkill`, esa
#: versión daba verde. El nieto ahora lo anuncia el propio padre.
#:
#: ## Por qué el nieto escribe en `stderr`
#:
#: Para que el padre no pueda cerrar el pipe por accidente: si el padre termina, el nieto hereda el
#: descriptor y sigue escribiendo. Por eso `stderr=subprocess.PIPE` y por eso nadie lee de ese
#: pipe: leerlo lo cerraría y el nieto moriría de `EPIPE`, que es un árbol que muere sin que nadie
#: lo mate.
CODIGO_DEL_PADRE = """
import subprocess, sys, time
nieto = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
sys.stderr.write("nieto-listo %d\\n" % nieto.pid)
sys.stderr.flush()
time.sleep(300)
"""


@pytest.fixture
def arbol_de_procesos(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int]:
    """Lanza un proceso que a su vez lanza otro, y devuelve el padre y el nieto.

    ## Por qué se lanza con `sys.executable` y no con el ejecutable del motor

    Porque la prueba no prueba el motor: prueba que **la muerte de árbol funciona**. Un motor de
    verdad costaría tokens del proveedor, que este proyecto no puede gastar, y su fallo sería un
    dato que esta prueba no puede interpretar. Dos procesos de Python que se esperan tienen la
    misma forma de árbol y coste cero.

    El nieto escribe en `stderr` para que el padre no pueda cerrar por accidente: si el padre
    termina, el nieto hereda el pipe y se queda solo. Por eso `stderr=subprocess.PIPE` y por eso
    nadie lee de ese pipe: leerlo lo cerraría y el nieto moriría de `EPIPE`, que es un árbol que
    muere sin que nadie lo mate.
    """

    padre = _lanzar_padre()
    assert padre.stderr is not None
    # Se espera a que el nieto exista de verdad: matar el árbol antes de que el hijo haya lanzado
    # al nieto mediría un árbol de un nivel, y un árbol de un nivel lo mata hasta `kill`.
    linea = padre.stderr.readline().decode("utf-8", errors="replace")
    assert linea.startswith("nieto-listo "), (
        f"el nieto no llegó a arrancar; la prueba mediría un árbol de un solo nivel: {linea!r}"
    )
    pid_del_nieto = int(linea.split()[1])

    # El nieto se toma **de lo que el padre anuncie**, no de una consulta al sistema.
    #
    # La versión anterior de esta prueba lo hacía al revés, con `tasklist /FI PID eq <padre>`, y
    # eso devuelve el proceso **padre**, no sus hijos: la comprobación se cumplía siempre y la
    # prueba medía un árbol de un solo nivel. El PID del nieto lo dice el propio padre, que es
    # quien lo lanzó y por tanto no puede equivocarse.
    assert _vivo(pid_del_nieto), "el nieto anunciado no existe"
    return padre, pid_del_nieto


def _lanzar_padre() -> subprocess.Popen[bytes]:
    """Lanza el proceso padre con la bandera de grupo que corresponde a este sistema.

    ## Por qué dos ramas y no un diccionario desplegado con `**`

    Porque la bandera **no existe** en el otro sistema operativo: `creationflags` es de Windows y
    `start_new_session` es de POSIX. Un `dict[str, object]` con una de las dos claves no encaja en
    ninguna de las sobrecargas de `Popen` y el typechecker no puede saber cuál se está usando. Con
    dos ramas cada llamada tiene sus argumentos concretos y se comprueban solos.

    En POSIX, `start_new_session` mete al hijo en su propio grupo; sin eso, `killpg` mataría también
    al proceso de pytest. En Windows, `CREATE_NEW_PROCESS_GROUP` es lo que permite actuar sobre el
    grupo sin tocar la consola de quien ejecuta la prueba. Es la misma razón por la que
    `_flags_de_proceso()` existe en `host.py`.
    """

    comando = [sys.executable, "-c", CODIGO_DEL_PADRE]
    if os.name == "nt":  # pragma: no cover - la bateria corre en el sistema que la ejecuta
        return subprocess.Popen(  # noqa: S603
            comando,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    return subprocess.Popen(  # noqa: S603
        comando,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _limpiar(padre: subprocess.Popen[bytes], nietos: list[int]) -> None:
    """Mata lo que quede, para que un fallo no deje procesos huérfanos en la máquina."""

    for pid in nietos:
        if _vivo(pid):
            _matar(pid)
    if padre.poll() is None:
        _matar(padre.pid)


def _matar(pid: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - la bateria corre en el sistema que la ejecuta
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(pid)],  # noqa: S607
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            os.kill(pid, 9)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# La prueba de verdad
# --------------------------------------------------------------------------- #


def test_cancelar_un_run_en_curso_mata_el_arbol_de_procesos(arbol_de_procesos) -> None:
    """Matar el árbol desde fuera deja al padre **y** al nieto sin vida.

    ## Por qué esto es la prueba del aborto y no una del runner

    Porque el camino real del aborto es: el panel manda el identificador guardado, el proceso de la
    API lo lee, y ese proceso —**otro distinto**, no el worker que lanzó el motor— mata el árbol por
    PID. Aquí se reproduce exactamente eso: se toma el PID de un árbol vivo, se construye la
    referencia `host-pid:` que el panel recibiría, se la pasa por `parsear_referencia` —el mismo
    lector que usa `kill_sandbox_container`— y se llama a `matar_por_pid`.

    ## Por qué el nieto es la aserción importante

    Porque un árbol de un solo nivel lo mata hasta `proceso.kill()`. El nieto es lo que
    distingue «mató el proceso» de «mató el árbol», y es exactamente lo que el motor deja vivo:
    `httpx`, `nmap`, navegadores.
    """

    padre, nieto = arbol_de_procesos
    try:
        assert _vivo(padre.pid)
        assert _vivo(nieto)

        # La referencia tal como la escribiría el worker y la leería el proceso de la API.
        referencia = f"{PREFIJO_REFERENCIA_HOST}{padre.pid}"
        assert HostRunResult.parsear_referencia(referencia) == padre.pid

        StrixHostRunner.matar_por_pid(padre.pid)

        assert _esperar_a_morir(padre.pid), "el proceso del motor sigue vivo después de abortar"
        assert _esperar_a_morir(nieto), (
            "un nieto del motor sigue vivo: el abortion mato al padre y dejo las herramientas "
            "gastando tokens del proveedor con el run ya terminal"
        )
    finally:
        _limpiar(padre, [nieto])


def test_matar_un_arbol_ya_muerto_no_falla(arbol_de_procesos) -> None:
    """Matar dos veces no lanza, porque el camino de aborto puede llegar tarde.

    ## Por qué este caso merece prueba

    Porque el aborto corre en el proceso de la API y el timeout corre en el worker: los dos pueden
    matar el mismo árbol. Si el segundo lanzara, elAbortwouldends en error en vez de confirmar que
    el escaneo paró, y el panel mostraría un fallo donde solo se puso de acuerdo con el worker.
    """

    padre, nieto = arbol_de_procesos
    try:
        StrixHostRunner.matar_por_pid(padre.pid)
        assert _esperar_a_morir(padre.pid)

        # El segundo intento, con el árbol ya fuera, tiene que ser inocuo.
        StrixHostRunner.matar_por_pid(padre.pid)
        StrixHostRunner.matar_por_pid(0)
        StrixHostRunner.matar_por_pid(-1)
    finally:
        _limpiar(padre, [nieto])


def test_una_referencia_de_contenedor_no_mata_ningun_proceso() -> None:
    """`parsear_referencia` devuelve `None` para lo que no es un PID, y `None` no se mata.

    ## Por qué esto no es una formalidad

    Porque `kill_sandbox_container` decide por la referencia: si una referencia antigua o escrita a
    mano devolviera un número, el abortaría de un proceso **ajeno**. El PID de otro proceso del
    servidor es adivinable, y `os.kill` no pide permiso para matar lo que sea de tu usuario.
    """

    contenedor = "fenix-strix-11111111-1111-1111-1111-111111111111"
    assert HostRunResult.parsear_referencia(contenedor) is None
    assert HostRunResult.parsear_referencia("replay:mindguard-site_23ee") is None
    assert HostRunResult.parsear_referencia("host-pid:abc") is None
    assert HostRunResult.parsear_referencia("host-pid:0") is None
    assert HostRunResult.parsear_referencia("host-pid:-5") is None
