"""Informe de las filas y celdas de una tabla: alto real, ancho y qué se ha solapado.

## Por qué existe

Porque «hay una barra oscura bajo una insignia» en una captura puede ser cinco cosas distintas —una
celda con fondo, una fila estirada por un hijo que se sale, un `border-bottom` de un elemento
flotante, un `overflow` que pinta el fondo del contenedor, o un hijo con `position` mal—which ninguna
se distingue leyendo el JSX. Todas tienen el mismo aspecto y causas opuestas, así que adivinar es
peor que no hacer nada: el riesgo es "arreglar" una regla que no participa en el fallo y dejar el
defecto igual, que es exactamente lo que pasó con la cronología.

## Qué mide

Por celda: su rectángulo, la altura del contenido que realmente pinta dentro, y la lista de hijos
con su caja. Con eso se ve cuál hijo es más alto que la celda —que es el que estira la fila— y cuál
se sale por debajo —que es el que pinta la barra—.

## Por qué mide el `scrollHeight` además del `getBoundingClientRect`

Porque el síntoma es contenido que se sale de su caja, y `getBoundingClientRect` solo devuelve la
caja. El desbordamiento se ve comparando `scrollHeight` con `clientHeight`: si el primero es
mayor, hay algo dentro que no cabe, y ese algo es el culpable.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
CORREO = "demo.admin@acmesecurity.io"
CLAVE = "DemoFenix2026!Empa"

INFORME = """
(o) => {
  const tabla = document.querySelector(o.s);
  if (!tabla) return { error: 'no se encuentra ' + o.s };
  const filas = Array.from(tabla.querySelectorAll('tbody tr'));
  return {
    columnas: Array.from(tabla.querySelectorAll('thead th')).map(
      (th) => (th.textContent || '').trim() || '(sin rotulo)'
    ),
    filas: filas.map((tr, i) => {
      const caja = tr.getBoundingClientRect();
      const celdas = Array.from(tr.children).map((td) => {
        const r = td.getBoundingClientRect();
        const hijos = Array.from(td.children).map((h) => {
          const hr = h.getBoundingClientRect();
          const cs = getComputedStyle(h);
          return {
            tag: h.tagName,
            clase: h.className || '',
            texto: (h.textContent || '').trim().slice(0, 28),
            top: Math.round(hr.top),
            bottom: Math.round(hr.bottom),
            ancho: Math.round(hr.width),
            alto: Math.round(hr.height),
            fondo: cs.backgroundColor,
            borde: cs.borderBottomColor + ' ' + cs.borderBottomWidth,
            desbordeVertical: td.scrollHeight - td.clientHeight,
          };
        });
        return {
          rotulo: (Array.from(tabla.querySelectorAll('thead th'))[td.cellIndex] || {}).textContent || '',
          texto: (td.textContent || '').trim().slice(0, 30),
          x: Math.round(r.x),
          ancho: Math.round(r.width),
          alto: Math.round(r.height),
          top: Math.round(r.top),
          fondo: getComputedStyle(td).backgroundColor,
          desbordeVertical: td.scrollHeight - td.clientHeight,
          hijos,
        };
      });
      return {
        indice: i,
        alto: Math.round(caja.height),
        celdas,
      };
    }),
  };
}
"""


async def principal() -> int:
    if len(sys.argv) < 3:
        print("uso: python _informe_tabla.py <ruta> <selector de tabla> [pestaña]")
        return 2

    ruta, selector = sys.argv[1], sys.argv[2]
    pestana = sys.argv[3] if len(sys.argv) > 3 else None

    peticion = urllib.request.Request(
        "%s/api/v1/auth/login" % BASE,
        data=json.dumps({"email": CORREO, "password": CLAVE}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        token = json.loads(respuesta.read())["access_token"]

    async with async_playwright() as p:
        navegador = await p.chromium.launch(channel="chrome", headless=True)
        contexto = await navegador.new_context(
            viewport={"width": 1600, "height": 1000}, locale="es-ES"
        )
        pagina = await contexto.new_page()
        await pagina.goto("%s/login" % BASE, wait_until="domcontentloaded")
        await pagina.fill('input[type="email"]', CORREO)
        await pagina.fill('input[type="password"]', CLAVE)
        await pagina.click('button[type="submit"]')
        await pagina.wait_for_url(lambda u: "/login" not in u, timeout=25000)

        await pagina.goto(BASE + ruta, wait_until="domcontentloaded")
        try:
            await pagina.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001, S110
            pass
        if pestana:
            await pagina.click('[role="tab"]:has-text("%s")' % pestana, timeout=5000)
            await pagina.wait_for_timeout(1200)
        else:
            await pagina.wait_for_timeout(1200)

        datos = await pagina.evaluate(
            "(o) => eval('(' + o.f + ')')(o)", {"f": INFORME, "s": selector}
        )
        await navegador.close()

    if "error" in datos:
        print("  %s" % datos["error"])
        return 1

    print("  columnas: %s" % " | ".join(datos["columnas"]))
    for fila in datos["filas"]:
        print()
        print("  FILA %d  alto=%d px" % (fila["indice"], fila["alto"]))
        for celda in fila["celdas"]:
            marca = "  <<< SE DESBORDA" if celda["desbordeVertical"] > 0 else ""
            print(
                "    %-14s ancho=%4d alto=%3d  %r%s"
                % (
                    (celda["rotulo"] or "?").strip()[:14],
                    celda["ancho"],
                    celda["alto"],
                    celda["texto"],
                    marca,
                )
            )
            for hijo in celda["hijos"]:
                print(
                    "        <%s class=%r> %r  %dx%d fondo=%s desborde=%d"
                    % (
                        hijo["tag"],
                        hijo["clase"][:28],
                        hijo["texto"],
                        hijo["ancho"],
                        hijo["alto"],
                        hijo["fondo"],
                        hijo["desbordeVertical"],
                    )
                )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
