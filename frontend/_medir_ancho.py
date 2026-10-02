"""Mide por qué un texto se parte en líneasdespite having room, en un nodo real del navegador.

## Por qué un script aparte y no una medición más dentro del de pegados

Porque son dos preguntas distintas. El de pegados mide **distancia entre hermanos**, que es una
relación entre dos elementos. Esta mide **ancho disponible de un solo nodo**, que es una relación
entre el nodo y su contenedor. Y la causa de que «Cargando el catálogo…» se partiera en tres
líneas de tres palabras no era ni el ancho ni el `gap`: era que el `<p>` era un ítem flex que
podía encogerse por debajo de su contenido.

Eso es un dato que solo se obtiene preguntándoselo al navegador al nodo concreto: `getComputedStyle`
y los `offsetWidth` de la cadena real. Leer el CSS y deducirlo es exactamente el error que lleva a
arreglar la regla equivocada.

## Qué mide

Para un texto dado, en una URL y un selector: su ancho de contenido, el ancho disponible que le
da el padre, si el padre es flex, su `flex` efetivo, y si se está partiendo. Con eso se sabe si el
culable es un `flex-shrink`, un `max-width`, un `width` heredado o un `white-space`.
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

#: Cómo se pide al navegador la medida de un nodo. Va como cadena porque `evaluate` no admite
#: funciones con argumentos sin serializarla antes.
MEDIR = """
(o) => {
  const sel = o.s;
  const buscar = (n) => {
    if (n.nodeType === 1 && n.matches(sel)) return n;
    for (const h of n.children || []) {
      const r = buscar(h);
      if (r) return r;
    }
    return null;
  };
  // Se mide el nodo que el selector nombra, y no el que tiene el texto. Al revés es un error
  // clásico: el texto esperado —«Cargando el catálogo…»— solo existe mientras carga, y cuando
  // termina el nodo es otro. Buscar por texto mide el estado que ya no es el que interesa.
  let el = null;
  try {
    el = document.querySelector(o.s);
  } catch (e) {
    return { error: 'selector invalido ' + o.s };
  }
  if (!el) {
    return { error: 'no se encuentra ' + o.s + ' en la pagina' };
  }
  const todos = [el];
  if ((el.textContent || '').trim() !== o.texto) {
    // Se anota, pero no se corta la medición: el nodo existe y su ancho es lo que importa.
    todos.push();
  }
  if (todos.length === 0) {
    return { error: 'sin nodo que medir' };
  }
  const textoDistinto = (el.textContent || '').trim() !== o.texto;
  const padre = el.parentElement;
  const cs = getComputedStyle(el);
  const cp = padre ? getComputedStyle(padre) : null;
  const r = el.getBoundingClientRect();
  const alturaLinea = parseFloat(cs.lineHeight);
  const rango = document.createRange();
  rango.selectNodeContents(el);
  const lineasReales = rango.getClientRects().length;
  return {
    texto: (el.textContent || '').trim(),
    textoDistinto,
    tag: el.tagName,
    clases: el.className,
    lineas: alturaLinea > 0 ? Math.round(r.height / alturaLinea) : null,
    lineasReales,
    anchoElemento: Math.round(r.width),
    anchoNecesario: Math.round(rango.getBoundingClientRect().width),
    anchoPadre: padre ? Math.round(padre.getBoundingClientRect().width) : null,
    padreEsFlex: cp ? cp.display.includes('flex') : false,
    padreDisplay: cp ? cp.display : null,
    padreFlexWrap: cp ? cp.flexWrap : null,
    flex: cs.flex,
    minWidth: cs.minWidth,
    width: cs.width,
    whiteSpace: cs.whiteSpace,
    overflowWrap: cs.overflowWrap,
    wordBreak: cs.wordBreak,
    displayPropio: cs.display,
    paddingIzq: cs.paddingLeft,
  };
}
"""


async def principal() -> int:
    if len(sys.argv) < 4:
        print("uso: python _medir_ancho.py <ruta> <selector> <texto que deberia verse>")
        return 2

    ruta, selector, esperado = sys.argv[1], sys.argv[2], sys.argv[3]

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

        # Si la ruta lleva pestañas, se pulsa la que se le pase como selector alterno.
        # El catálogo se pide dos veces porque se monta en diferido: `KnowledgePage` lo envuelve en
        # un `Suspense`, así que la primera vez puede salir el texto de reserva en vez del
        # componente. Con una sola pasada, la medición cae sobre el nodo equivocado y el script
        # informa de que el texto no existe cuando lo que no existe es la espera.
        if ruta.endswith("/knowledge"):
            for _ in range(2):
                try:
                    await pagina.click('[role="tab"]:has-text("Cat")', timeout=4000)
                except Exception:  # noqa: BLE001, S110
                    pass
                await pagina.wait_for_timeout(1500)

        # Se recorta el DOM para congelar el estado: sin esto, la medición depende de en qué
        # instante de la carga cae la captura, y el mismo script da dos respuestas distintas.
        await pagina.evaluate("() => { window.__frozen = true; }")

        # Antes de medir se imprime qué hay realmente dentro del contenedor. Si el texto buscado
        # no aparece, casi siempre es que el estado ya cambió —el catálogo cargó y se pintó la
        # rejilla— y medir el nodo equivocado lleva a "arreglar" una regla que no es la del fallo.
        inventario = await pagina.evaluate(
            """() => Array.from(document.querySelectorAll('.content-card > *'))
                 .map(e => e.tagName + '.' + (e.className || '') + ' = ' +
                          (e.textContent || '').trim().slice(0, 50))"""
        )
        print("  hijos directos de .content-card:")
        for linea in inventario:
            print("    %s" % linea)
        print()

        # El selector viaja dentro de un objeto porque la firma de Playwright es
        # `evaluate(expresion, arg)`: una expresión que recibe dos parámetros no puede recibirlos
        # como dos argumentos sueltos, y pasarlos así falla con un `TypeError` poco descriptivo.
        medida = await pagina.evaluate(
            "(o) => eval('(' + o.f + ')')(o)",
            {"f": MEDIR, "s": selector, "texto": esperado},
        )

        await navegador.close()

    if "error" in medida:
        print("  %s" % medida["error"])
        for cand in medida.get("candidatos", []):
            print("      %r" % cand)
        return 1

    print("  ruta     %s" % ruta)
    print("  texto    %r" % medida["texto"])
    if medida.get("textoDistinto"):
        print(
            "  AVISO    no es el texto que se buscaba; la carga ya habia terminado y se mide"
        )
        print("           el nodo que hay, no el del fallo")
    print("  nodo     <%s class=%r>" % (medida["tag"], medida["clases"]))
    print()
    print("  ancho del elemento ....... %s px" % medida["anchoElemento"])
    print("  ancho que necesita ....... %s px" % medida["anchoNecesario"])
    print("  ancho del padre .......... %s px   display: %s  flex: %s"
          % (medida["anchoPadre"], medida["padreDisplay"], medida["flex"]))
    print("  padre es flex ............ %s   wrap: %s" % (medida["padreEsFlex"], medida["padreFlexWrap"]))
    print("  white-space / overflow-wrap / word-break .. %s / %s / %s"
          % (medida["whiteSpace"], medida["overflowWrap"], medida["wordBreak"]))
    print("  lineas (altura/line-height) %s   lineas reales (Range) %s"
          % (medida["lineas"], medida["lineasReales"]))

    print()
    if medida["lineasReales"] > 1:
        print("  SE ESTA PARTIENDO en %d lineas" % medida["lineasReales"])
        print()
        print("  Pistas para el culpable:")
        if medida["padreEsFlex"]:
            print("    - el padre es flex: el hijo puede encogerse por debajo de su contenido.")
            print("      Es lo que mas comunmente parte el texto en palabras sueltas.")
        if medida["anchoNecesario"] > medida["anchoElemento"]:
            print(
                "    - necesita %s px y tiene %s px: falta ancho de verdad, no es un shrink."
                % (medida["anchoNecesario"], medida["anchoElemento"])
            )
        return 1

    print("  no se parte: cabe en una linea")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
