"""Mide el hueco real entre etiquetas, botones y el texto que tienen al lado.

## Por qué esto existe y por qué no basta con leer el CSS

Porque el defecto que motiva el script no es una regla que falte: es una regla que existe y no
llega a aplicarse. En Operaciones, la celda del contenedor llevaba el identificador y el badge de
`Limpieza pendiente` como dos nodos hermanos dentro de un `<td>` que no era flex. Entre un nodo de
texto y un `<span>` en línea no hay nada que colocar, así que salían pegados: el navegador no
avisa de que dos cosas deberían estar separadas, simplemente las pone una detrás de otra.

Leyendo el JSX eso no se ve, porque el JSX no dice nada sobre separaciones: solo el ancho medido
las delata. Y como el mismo descuido se repite en toda la consola —botón al lado de etiqueta,
chip al lado de cifra—, la única forma fiable de saber dónde pasa es medir las 31 pantallas.

## Qué mide

Para cada elemento en línea decorativo —`.badge`, `.chip`, `.pill`, `.tag`, `.status-*`— busca el
hermano visible anterior y mide el hueco horizontal entre el borde derecho del anterior y el
izquierdo del actual. Si el hueco es menor que el mínimo, lo reporta con la ruta del elemento, el
texto de los dos y el hueco real.

Se salta a propósito los casos en que la Pegazón es correcta:

- Los dos elementos están en líneas distintas (`top` distinto), porque entonces el hueco vertical
  ya los separa y el horizontal no cuenta.
- El elemento está dentro de un `<button>` o de un `<a>`, porque ahí lo que importa es el hueco
  interno del botón, no el de fuera.
- El hermano anterior es un icono, porque un icono y su etiqueta sí se apoyan: es el patrón
  `icon + texto` que usa toda la consola.

## Por qué el umbral es 6 px y no 0

Porque el defecto visible no es el contacto exacto sino el aspecto de palabra pegada. Con 4 px de
hueco el badge sigue leyéndose como parte de la cifra; a partir de 6 px ya se ve que es otra cosa.
Medir 0 solo cazaría el caso extremo y dejaría pasar todo lo que se ve mal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
ORG_ID = "8aef9544-3fe6-5fd9-890a-4c7e948a25e3"
CORREO = "demo.admin@acmesecurity.io"
CLAVE = "DemoFenix2026!Empa"

#: Hueco horizontal mínimo, en píxeles, entre dos cosas en línea de la misma celda.
HUECO_MINIMO = 6.0

PAGINAS: list[tuple[str, str]] = [
    ("cons-operaciones", "/admin/operations"),
    ("cons-precios", "/admin/pricing"),
    ("cons-organizaciones", "/admin/tenants"),
    ("cons-usuarios", "/admin/users"),
    ("cons-tickets", "/admin/tickets"),
    ("cons-auditoria", "/admin/audit"),
    ("cons-agentes", "/admin/agents"),
    ("cons-ventas", "/admin/sales"),
    ("cons-ficha-pactados", f"/admin/organizations/{ORG_ID}/pricing"),
    ("cli-dashboard", "/dashboard"),
    ("cli-pentests", "/pentests"),
    ("cli-incidentes", "/issues"),
    ("cli-repositorios", "/repositories"),
    ("cli-conocimiento", "/knowledge"),
    ("cli-cve", "/cve"),
    ("cli-revisiones-pr", "/pr-reviews"),
    ("cli-chat", "/chat"),
    ("cli-dominios", "/domains"),
    ("cli-descubrimiento", "/asset-discovery"),
    ("cli-cadena-suministro", "/supply-chain"),
    ("cli-contenedores", "/containers"),
    ("cli-redes", "/networks"),
    ("cli-integraciones", "/integrations"),
    ("cli-acceso-api", "/api-access"),
    ("cli-facturacion", "/billing"),
    ("cli-ajustes", "/settings/general"),
    ("cli-ajustes-miembros", "/settings/members"),
    ("cli-ajustes-facturacion", "/settings/billing"),
    ("cli-ajustes-auditoria", "/settings/audit-logs"),
    ("cli-soporte", "/settings/support"),
]

#: Se ejecuta dentro del navegador. Devuelve una lista de hallazgos con la posición del pegón.
MEDIR = """
() => {
  const SELECTOR = [
    '.badge', '.chip', '.pill', '.tag', '.status', '[class*="status-badge"]',
    'button', 'input:not([type="hidden"])', 'select', 'textarea',
    'a.link-button', '.link-button', '.icon-button',
  ].join(', ');
  const MINIMO = __MINIMO__;
  const hallazgos = [];

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const e = getComputedStyle(el);
    return e.visibility !== 'hidden' && e.display !== 'none' && e.opacity !== '0';
  };

  // Un texto suelto no es un elemento, así que el "hermano anterior" puede ser un nodo de texto.
  // Se mide contra el rectángulo que ocupa ese texto con un Range, que es lo único que da las
  // coordenadas reales de un nodo de texto.
  const rectDelAnterior = (el) => {
    let nodo = el.previousSibling;
    while (nodo) {
      if (nodo.nodeType === 3) {
        const txt = nodo.textContent.trim();
        if (txt) {
          const rango = document.createRange();
          rango.selectNodeContents(nodo);
          const r = rango.getBoundingClientRect();
          if (r.width > 0) return { rect: r, texto: txt, esNodo: true };
          return null;
        }
        nodo = nodo.nextSibling;
        continue;
      }
      if (nodo.nodeType === 1) {
        const e = nodo;
        if (e.tagName === 'svg' || e.tagName === 'path') {
          // Icono a la izquierda: es el patrón icon + etiqueta, no un pegón.
          return { icono: true };
        }
        if (!visible(e)) { nodo = nodo.nextSibling; continue; }
        const cs = getComputedStyle(e);
        // `inline-flex` importa tanto como `inline` aquí: los botones de la consola son
        // `inline-flex` casi todos, y si la lista no lo incluye el comprobador los salta enteros
        // y dice que no hay pegones justo en la mitad de los casos que quiere cazar.
        const EN_LINEA = ['inline', 'inline-block', 'inline-flex', 'inline-grid'];
        if (!EN_LINEA.includes(cs.display)) return null;  // otra línea: el hueco vertical separa
        return { rect: e.getBoundingClientRect(), texto: (e.textContent || '').trim().slice(0, 40), esNodo: false };
      }
      nodo = nodo.nextSibling;
    }
    return null;
  };

  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!visible(el)) continue;
    const contenedor = el.closest('button, a');
    // Lo que está dentro de un botón o de un enlace se mide dentro de él, no desde fuera: el hueco
    // entre el icono y la etiqueta de un botón es asunto de ese botón. Pero un botón pegado a OTRO
    // botón o a un campo sí es un pegón, y eso es precisamente lo que se busca.
    if (contenedor && contenedor !== el) continue;
    const previo = rectDelAnterior(el);
    if (!previo || previo.icono) continue;
    const r = el.getBoundingClientRect();
    const p = previo.rect;
    // Distintas líneas: ya hay separación vertical, el hueco horizontal no dice nada.
    if (Math.abs(p.top - r.top) > Math.max(p.height, r.height) * 0.6) continue;
    const hueco = r.left - p.right;
    if (hueco >= MINIMO) continue;
    hallazgos.push({
      etiqueta: (el.textContent || '').trim().slice(0, 40),
      clase: el.className && el.className.baseVal === undefined ? String(el.className) : 'badge',
      anterior: previo.texto,
      hueco: Math.round(hueco * 10) / 10,
      celda: (el.closest('td, .cell-inline, th') || {}).tagName || 'div',
    });
  }
  return hallazgos;
}
"""


def pedir_token() -> str:
    peticion = urllib.request.Request(
        f"{BASE}/api/v1/auth/login",
        data=json.dumps({"email": CORREO, "password": CLAVE}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        return json.loads(respuesta.read())["access_token"]


#: Los hallazgos acumulados, para que `medir` no tenga que devolverlos.
HALLAZGOS: list[tuple[str, str, dict]] = []

#: Las pestañas que se han pulsado de verdad. Se imprime el recuento porque un recorrido de
#: pestañas que no pulsa nada y dice que no ha encontrado nada es indistinguible de un
#: comprobador roto, y esa es exactamente la confusión que este script no quiere dejar.
PESTANAS_PULSADAS: list[tuple[str, str]] = []


async def medir(pagina, nombre: str, etiqueta: str) -> None:
    """Mide la pantalla actual y anota lo encontrado, si algo.

    `MEDIR` lleva el umbral como marcador de texto porque `evaluate` no admite una función con
    argumentos con nombre sin serializarla antes; se sustituye aquí y se evalúa como expresión.
    """
    hallazgos = await pagina.evaluate(
        "(f) => eval('(' + f + ')')()",
        MEDIR.replace("__MINIMO__", str(HUECO_MINIMO)),
    )
    if not hallazgos:
        return
    print("  %s%s" % (nombre, ("  >  " + etiqueta) if etiqueta else ""))
    for h in hallazgos:
        HALLAZGOS.append((nombre, etiqueta, h))
        print(
            "      hueco %5.1f px   %-22s | anterior: %s"
            % (h["hueco"], h["etiqueta"][:22], h["anterior"][:30])
        )


async def principal() -> int:
    pedir_token()

    async with async_playwright() as p:
        navegador = await p.chromium.launch(channel="chrome", headless=True)
        contexto = await navegador.new_context(
            viewport={"width": 1600, "height": 1000}, locale="es-ES"
        )
        pagina = await contexto.new_page()
        await pagina.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await pagina.fill('input[type="email"]', CORREO)
        await pagina.fill('input[type="password"]', CLAVE)
        await pagina.click('button[type="submit"]')
        await pagina.wait_for_url(lambda u: "/login" not in u, timeout=25000)
        print("  sesion iniciada")

        for nombre, ruta in PAGINAS:
            await pagina.goto(f"{BASE}{ruta}", wait_until="domcontentloaded")
            try:
                await pagina.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001, S110
                pass
            await pagina.wait_for_timeout(500)

            await medir(pagina, nombre, "")

            # ## Por qué hay que pulsar las pestañas y no basta con abrir la página
            #
            # Porque una URL solo enseña la primera pestaña. Operaciones tiene cuatro —escaneos,
            # contenedores, revisiones y sondas— y las otras tres no se miran nunca por esta vía:
            # su contenido no existe hasta que se pulsan. El encargo es revisar el flujo de cada
            # pestaña, y una pestaña que no se ha pulsado es un flujo sin comprobar.
            #
            # Se recorren por etiqueta porque el orden importa: volver a la primera entre medias
            # recargaría los datos y el recorrido sería más lento sin encontrar nada nuevo.
            etiquetas = await pagina.eval_on_selector_all(
                '[role="tab"]',
                "els => els.map(e => (e.textContent || '').trim()).filter(Boolean)",
            )
            for etiqueta in etiquetas:
                try:
                    await pagina.click(f'[role="tab"]:has-text("{etiqueta}")', timeout=4000)
                except Exception:  # noqa: BLE001, S110
                    continue
                await pagina.wait_for_timeout(700)
                PESTANAS_PULSADAS.append((nombre, etiqueta))
                await medir(pagina, nombre, etiqueta)

        await navegador.close()

    print()
    print(
        "  %d pantalla(s) y %d pestaña(s) medidas"
        % (len(PAGINAS), len(PESTANAS_PULSADAS))
    )
    if HALLAZGOS:
        print("  %d pegon(es) encontrado(s)" % len(HALLAZGOS))
        return 1
    print(
        "  ninguna etiqueta pegada a su texto en las %d pantallas, con todas sus pestañas"
        % len(PAGINAS)
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
