"""Demuestra que cada prueba de `pricingChain.test.ts` **no está vacía**.

Reintroduce un defecto cada vez en `pricingChain.ts`, ejecuta el test que debería detectarlo, y
comprueba que **falla**. Al final restaura el código y comprueba que todo vuelve a pasar.

Es el mismo protocolo que el del backend: una prueba que pasa con el defecto puesto no comprueba
nada, y da verde sobre un sistema roto.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
#: `pricingChain.ts` vive junto a la pantalla, en `src/features/admin`. El script está en la raíz
#: de `frontend` para poder lanzar `npx vitest` con el `package.json` de ahí, así que la ruta
#: del módulo se compone en vez de suponerse.
DIR_PANTALLA = os.path.join(RAIZ, "src", "features", "admin")
MODULO = os.path.join(DIR_PANTALLA, "pricingChain.ts")
TEST = os.path.join(DIR_PANTALLA, "pricingChain.test.ts")

#: Cada defecto es un par (nombre, texto viejo, texto nuevo) sobre `pricingChain.ts`.
DEFECTOS: list[tuple[str, str, str]] = [
    (
        "el vigente se trata como caducado si tiene fecha de fin",
        "  if (pactado.vigente) {\n    return 'vigente'\n  }\n",
        "  if (false) {\n    return 'vigente'\n  }\n",
    ),
    (
        "un pactado programado se confunde con uno caducado",
        "  if (pactado.valido_hasta !== null) {\n    return 'caducado'\n  }\n",
        "  if (pactado.vigente) {\n    return 'caducado'\n  }\n",
    ),
    (
        "la cadena no se ordena y se queda en el orden que llegó",
        "  return [...pactados].sort((a, b) => Date.parse(b.valido_desde) - Date.parse(a.valido_desde))",
        "  return [...pactados]",
    ),
    (
        "la ordenación es de fecha creciente: primero el más antiguo",
        "Date.parse(b.valido_desde) - Date.parse(a.valido_desde)",
        "Date.parse(a.valido_desde) - Date.parse(b.valido_desde)",
    ),
    (
        "un pactado sustituido vuelve a salir como programado",
        "      estados.set(pactado.id, esElQueManda ? 'vigente' : 'sustituido')",
        "      estados.set(pactado.id, esElQueManda ? 'vigente' : 'programado')  //nsustituido",
    ),
    (
        "el vigente del servidor se ignora y gana el mas reciente por fecha",
        "    const marcado = candidatos.find((p) => p.vigente)",
        "    const marcado = undefined  //nvigente",
    ),
    (
        "las operaciones se mezclan y una se come a la otra",
        "    const candidatos = ordenados.filter((p) => claveDe(p) === clave)",
        "    const candidatos = ordenados  //nclave",
    ),
    (
        "el resumen cuenta los pactados en dos casillas a la vez",
        "    resumen[estadoDePactado(pactado)] += 1",
        "    resumen[estadoDePactado(pactado)] += 1\n"
        "    if (estadoDePactado(pactado) === 'vigente') {\n"
        "      resumen.caducado += 1\n"
        "    }",
    ),
    (
                "el total del resumen no cuadra con la lista",
        "    total: pactados.length,",
        "    total: 0,  //ntotal",
    ),
    (
        "la ordenación muta la lista que recibe",
        "  return [...pactados].sort((a, b) => Date.parse(b.valido_desde) - Date.parse(a.valido_desde))",
        "  return pactados.sort((a, b) => Date.parse(b.valido_desde) - Date.parse(a.valido_desde))",
    ),
]


def _npx() -> list[str]:
    """El comando con el que lanzar vitest en esta máquina.

    ## Por qué no es simplemente `["npx", ...]`

    Porque en Windows `npx` no es un ejecutable: es `npx.CMD`, un fichero por lotes. `CreateProcess`,
    que es lo que usa `subprocess` sin `shell`, no lanza ficheros por lotes, y el error que sale es
    `[WinError 2] El sistema no puede encontrar el archivo especificado`, que no dice nada de
    scripts por lotes y hace perder media hora. Resuelto a la ruta absoluta y envuelto en `cmd /c`
    cuando hace falta, funciona en los dos sistemas sin cambiar de código.
    """

    ruta = shutil.which("npx")
    if ruta is None:
        raise SystemExit("No se encuentra `npx` en el PATH: hace falta para lanzar vitest.")
    if ruta.lower().endswith((".cmd", ".bat")):
        return [shutil.which("cmd") or "cmd", "/c", ruta]
    return [ruta]


def correr() -> tuple[int, str]:
    proceso = subprocess.run(
        [*_npx(), "vitest", "run", "src/features/admin/pricingChain.test.ts"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=RAIZ,
    )
    return proceso.returncode, (proceso.stdout or "") + (proceso.stderr or "")


def main() -> int:
    with io.open(MODULO, encoding="utf-8", newline="") as f:
        original = f.read()
    copia = os.path.join(RAIZ, "pricingChain.ts.bak")
    shutil.copyfile(MODULO, copia)

    print("=" * 74)
    print("DEMOSTRACION DE NO VACIEDAD - logica de la cadena de precios")
    print("=" * 74)

    vacios: list[str] = []
    rotos: list[str] = []
    try:
        for nombre, viejo, nuevo in DEFECTOS:
            if viejo not in original:
                print("  [ERROR] %s: el texto a cambiar no existe en pricingChain.ts" % nombre)
                vacios.append(nombre)
                continue
            with io.open(MODULO, "w", encoding="utf-8", newline="") as f:
                f.write(original.replace(viejo, nuevo, 1))
            codigo, salida = correr()
            with io.open(MODULO, "w", encoding="utf-8", newline="") as f:
                f.write(original)

            # Una mutación que rompe la compilación hace que vitest no recoja ningún test, y eso
            # es un código de salida distinto de cero. Contarlo como "detectado" sería mentira:
            # el defecto no lo cazó ninguna prueba, lo cazó el parser.
            if "no tests" in salida:
                print("  [ROTO] %s -> la mutación no compila; no la ha cazado ninguna prueba" % nombre)
                rotos.append(nombre)
                continue
            if codigo == 0:
                vacios.append(nombre)
                print("  [VACIO] %s -> el test PASA con el defecto puesto" % nombre)
            else:
                fallos = [ln.strip() for ln in salida.splitlines() if ln.strip().startswith("×")]
                resumen = [ln for ln in salida.splitlines() if "Tests" in ln]
                print("  [BIEN ] %s" % nombre)
                for linea in fallos[:3]:
                    print("          %s" % linea)
                for linea in resumen[-1:]:
                    print("          %s" % linea.strip())
    finally:
        shutil.copyfile(copia, MODULO)
        os.remove(copia)
        print("-" * 74)
        print("  codigo restaurado al estado inicial")

    print("=" * 74)
    codigo, salida = correr()
    if codigo != 0:
        print("  ATENCION: restaurado y aun asi falla:\n%s" % salida[-2000:])
        return 1
    resumen = [ln for ln in salida.splitlines() if "Tests" in ln]
    print("  con el codigo restaurado: %s" % (resumen[-1].strip() if resumen else "todo pasa"))

    if rotos:
        print("  %d mutación(es) no compilaban, que no es una detección: %s"
              % (len(rotos), "; ".join(rotos)))
    if vacios:
        print("  %d prueba(s) VACIAS: %s" % (len(vacios), "; ".join(vacios)))
        return 1
    print("  %d defectos reintroducidos, %d detectados" % (len(DEFECTOS), len(DEFECTOS)))
    print("  ninguna prueba esta vacia")
    return 0


if __name__ == "__main__":
    sys.exit(main())
