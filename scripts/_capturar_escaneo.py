"""Captura la página de un escaneo, entrando por la lista, y mide la cronología.

## Por qué se entra por la lista y no por URL directa

Porque un token de SuperAdmin recibe `403` en los endpoints de tenant —aislamiento multi-tenant, R3—
así que no se puede fabricar la URL de un run con ese token. Y porque el camino que recorre una
persona para ver un escaneo es el que hay que verificar: lista, clic en la fila, detalle. Si el
enlace de la tabla estuviera roto, una captura directa de la URL lo ocultaría.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "capturas")
CORREO = "demo.admin@acmesecurity.io"
CLAVE = "DemoFenix2026!Empa"

#: Los tres pares etiqueta/hora de la cronología, medidos como cajas para ver si se tocan.
MEDIR_CRONOLOGIA = """
() => {
  const items = Array.from(document.querySelectorAll('.timeline-item'));
  return items.map((li) => {
    const et = li.querySelector('.timeline-label');
    const hr = li.querySelector('.timeline-time');
    if (!et || !hr) return null;
    const a = et.getBoundingClientRect();
    const b = hr.getBoundingClientRect();
    return {
      etiqueta: (et.textContent || '').trim(),
      hora: (hr.textContent || '').trim(),
      hueco: Math.round((b.left - a.right) * 10) / 10,
      mismaLinea: Math.abs(a.top - b.top) < Math.max(a.height, b.height) * 0.6,
      anchoEtiqueta: Math.round(a.width),
      anchoHora: Math.round(b.width),
      display: getComputedStyle(et.parentElement).display,
    };
  }).filter(Boolean);
}
"""


async def principal() -> int:
    peticion = urllib.request.Request(
        "%s/api/v1/auth/login" % BASE,
        data=json.dumps({"email": CORREO, "password": CLAVE}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        json.loads(respuesta.read())

    os.makedirs(SALIDA, exist_ok=True)

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

        await pagina.goto("%s/pentests" % BASE, wait_until="domcontentloaded")
        try:
            await pagina.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001, S110
            pass

        # Se entra por el enlace de la primera fila, que es el camino real de un usuario: la fila
        # entera no es clicable, solo el identificador. Pulsar la fila no navegaría a nada.
        #
        # Y se espera al nodo, no a la URL: es una SPA, así que no hay evento `load` que esperar y
        # `wait_for_url` se quedaría esperando hasta agotar el tiempo.
        await pagina.click("tbody tr:first-child a", timeout=8000)
        try:
            await pagina.wait_for_selector(".timeline", timeout=12000)
        except Exception:  # noqa: BLE001, S110
            pass
        await pagina.wait_for_timeout(2000)
        destino = os.path.join(SALIDA, "cli-pentest-detalle.png")
        await pagina.screenshot(path=destino, full_page=True)
        print("  url    %s" % pagina.url)
        print("  captura %s" % os.path.normpath(destino))

        medidas = await pagina.evaluate("(f) => eval('(' + f + ')')()", MEDIR_CRONOLOGIA)
        await navegador.close()

    if not medidas:
        print("  no hay cronologia en esta pagina (el run puede no tenerla todavia)")
        return 0

    print()
    print("  cronologia:")
    for m in medidas:
        estado = "PEGADOS" if (m["mismaLinea"] and m["hueco"] < 6) else "separados"
        print(
            "    %-28s %-10s hueco=%5.1f px  %s  (padre display: %s)"
            % (m["etiqueta"], m["hora"], m["hueco"], estado, m["display"])
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
