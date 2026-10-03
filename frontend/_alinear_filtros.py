"""Mide la alineación vertical de los controles de una barra de filtros.

## Por qué esto y no mirar la captura

Porque «el checkbox está desalineado» tiene tres causas distintas que en una imagen se ven igual:
que el checkbox tenga un margen, que el contenedor que lo envuelve empuje con `padding-top`, o que
la línea base del texto del `<label>` no sea la misma que la del control. Arreglar la causa
equivocada deja el síntoma igual, y eso ya pasó hoy con el texto partido del catálogo.

## Qué mide

Para cada control de la barra —el buscador, el `select`, el checkbox— su caja, la de su etiqueta y
la línea base del texto. Con eso se ve cuál de los dos elementos está fuera de sitio, en vez de
suponerlo. Y compara contra el `select` de al lado, que es la referencia: los filtros de una barra
comparten línea de control, y si uno no coincide es porque tiene algo encima que el otro no tiene.

## Por qué importa la línea base y no solo el borde superior

Porque «alineado» en un formulario quiere decir que las etiquetas se leen en la misma horizontal,
no que las cajas empiecen en el mismo píxel: un checkbox es más bajo que un `select` por diseño.
Comparar solo el borde superior da un falso positivo en cuanto hay dos alturas distintas.
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

MEDIR = """
(sel) => {
  const barra = document.querySelector(sel);
  if (!barra) return { error: 'no se encuentra ' + sel };
  const hijos = Array.from(barra.children).filter(
    (e) => e.className && String(e.className).includes('filter-field')
  );
  return hijos.map((campo) => {
    const control = campo.querySelector('input, select, textarea');
    const etiqueta = campo.querySelector('label');
    if (!control) return null;
    const rc = control.getBoundingClientRect();
    const re = etiqueta ? etiqueta.getBoundingClientRect() : null;
    const cc = getComputedStyle(campo);
    const ct = getComputedStyle(control);
    // La línea base del texto es lo que de verdad se lee: caja del texto, no borde de la caja.
    let lineaBase = null;
    if (etiqueta) {
      const rango = document.createRange();
      rango.selectNodeContents(etiqueta);
      const rt = rango.getBoundingClientRect();
      lineaBase = Math.round(rt.top + rt.height);
    }
    return {
      claseCampo: String(campo.className),
      etiqueta: etiqueta ? (etiqueta.textContent || '').trim() : null,
      tipoControl: control.tagName + (control.type ? '[' + control.type + ']' : ''),
      controlTop: Math.round(rc.top),
      controlAlto: Math.round(rc.height),
      etiquetaTop: re ? Math.round(re.top) : null,
      etiquetaAlto: re ? Math.round(re.height) : null,
      lineaBase,
      paddingTopCampo: cc.paddingTop,
      displayCampo: cc.display,
      alignItems: cc.alignItems,
      altoControl: ct.height,
    };
  }).filter(Boolean);
}
"""


async def principal() -> int:
    if len(sys.argv) < 3:
        print("uso: python _alinear_filtros.py <ruta> <selector de la barra> [pestaña]")
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
        json.loads(respuesta.read())

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
        await pagina.wait_for_timeout(1500)

        datos = await pagina.evaluate(
            "(o) => eval('(' + o.f + ')')(o.s)", {"f": MEDIR, "s": selector}
        )
        await navegador.close()

    if isinstance(datos, dict) and "error" in datos:
        print("  %s" % datos["error"])
        return 1
    if not datos:
        print("  no hay campos de filtro")
        return 1

    for c in datos:
        print(
            "  %-28s %-16s control top=%4d alto=%3d  etiqueta top=%s  padding-top=%s  display=%s"
            % (
                (c["etiqueta"] or "(sin etiqueta)")[:28],
                c["tipoControl"],
                c["controlTop"],
                c["controlAlto"],
                c["etiquetaTop"],
                c["paddingTopCampo"],
                c["displayCampo"],
            )
        )

    # Referencia: el primer control con etiqueta. El resto debe compartir su línea de control.
    conEtiqueta = [c for c in datos if c["etiqueta"]]
    if len(conEtiqueta) > 1:
        referencia = conEtiqueta[0]
        ref_centro = referencia["controlTop"] + referencia["controlAlto"] / 2
        print()
        print(
            "  referencia: %-24s centro de control = %.1f px"
            % (referencia["etiqueta"][:24], ref_centro)
        )
        for c in conEtiqueta[1:]:
            centro = c["controlTop"] + c["controlAlto"] / 2
            delta = centro - ref_centro
            # Se comparan CENTROS y no bordes superiores. Un checkbox mide 16 px y un select 36, así
            # que sus bordes superiores nunca coinciden aunque estén perfectamente centrados: medir
            # por el borde da un falso positivo en cuanto hay dos alturas distintas, que es
            # exactamente el caso de esta barra.
            if abs(delta) > 2:
                print(
                    "  DESALINEADO %-24s centro = %.1f  (%.1f px)"
                    % (c["etiqueta"][:24], centro, delta)
                )
            else:
                print(
                    "  alineado     %-24s centro = %.1f  (%.1f px)"
                    % (c["etiqueta"][:24], centro, delta)
                )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
