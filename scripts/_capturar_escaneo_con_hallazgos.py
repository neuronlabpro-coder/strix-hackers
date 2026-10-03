"""Captura el detalle de un escaneo **con hallazgos**, por URL directa.

## Por qué este script y no `scripts/_capturar_escaneo.py`

Porque ese script entra por la lista y cae en el **primer** run, que en el workspace de
demostración es uno que no dejó hallazgos: su tarjeta sale con el estado vacío y no prueba
nada del gráfico. Este entra por URL, y el run se le pasa por parámetro, para poder verificar
la rama que pinta barras.

El resto —login, captura, espera al canvas— es el mismo camino que el script existente.
"""

from __future__ import annotations

import asyncio
import os
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "capturas")
CORREO = "demo.admin@acmesecurity.io"
CLAVE = "DemoFenix2026!Empa"

#: Cuánto se espera a que el chunk de ECharts descargue y pinte. El `networkidle` no sirve: con
#: el sondeo de la ejecución abierta la red nunca está quieta.
ESPERA_CANVAS_MS = 4000


async def main(run_id: str) -> int:
    # La comprobación de que el backend responde se hace con una llamada real antes de abrir el
    # navegador: si el API no contesta, un fallo de red en Playwright se lee como un fallo de la
    # pantalla y se investiga en el sitio equivocado.
    peticion = urllib.request.Request(
        f"{BASE}/api/v1/auth/login",
        data=('{"email":"%s","password":"%s"}' % (CORREO, CLAVE)).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        import json

        print("  backend responde: token de %d caracteres" % len(json.loads(respuesta.read())["access_token"]))

    async with async_playwright() as p:
        # `channel="chrome"` y no el Chromium de Playwright: el Chromium empaquetado no está
        # instalado en esta máquina, y los dos scripts de captura del proyecto ya usan el Chrome
        # del sistema por esa razón. Un `chromium.launch()` a secas falla al arrancar.
        navegador = await p.chromium.launch(channel="chrome", headless=True)
        pagina = await navegador.new_page(viewport={"width": 1600, "height": 1200}, locale="es-ES")
        avisos: list[str] = []
        pagina.on("console", lambda m: avisos.append(f"[{m.type}] {m.text}"))
        pagina.on(
            "response",
            lambda r: avisos.append(f"[http {r.status}] {r.url}")
            if r.status >= 400 and "favicon" not in r.url
            else None,
        )

        # El login se hace **por el formulario**, injecting el token en `localStorage` no vale:
        # el `AuthContext` guarda la sesión con su propia clave y su propio formato, y una clave
        # inventada deja al panel en `/login` sin un solo error en consola. Es el mismo camino que
        # `_capturas_console.py`.
        await pagina.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await pagina.fill('input[type="email"]', CORREO)
        await pagina.fill('input[type="password"]', CLAVE)
        await pagina.click('button[type="submit"]')
        await pagina.wait_for_url(lambda u: "/login" not in u, timeout=25000)

        await pagina.goto(f"{BASE}/pentests/{run_id}", wait_until="domcontentloaded")
        await pagina.wait_for_timeout(ESPERA_CANVAS_MS)

        # Lo que se ve, medido: cuántos canvas hay, cuánto miden y qué dicen sus aria-label.
        midido = await pagina.evaluate(
            """
            () => Array.from(document.querySelectorAll('.chart-canvas')).map((c) => {
              const caja = c.getBoundingClientRect();
              const lienzo = c.querySelector('canvas');
              return {
                ancho: Math.round(caja.width),
                alto: Math.round(caja.height),
                role: c.getAttribute('role'),
                aria: c.getAttribute('aria-label'),
                canvasPintado: lienzo ? (lienzo.width * lienzo.height) > 0 : false,
              };
            })
            """
        )
        print("graficos en la pagina (div.chart-canvas):")
        for grafico in midido:
            print(
                f"  {grafico['ancho']}x{grafico['alto']} role={grafico['role']!r}"
                f" aria={grafico['aria']!r} canvas_con_pixels={grafico['canvasPintado']}"
            )
        sin_aria = [g for g in midido if not g["aria"]]
        sin_pintar = [g for g in midido if not g["canvasPintado"]]
        print(f"graficos sin aria-label: {len(sin_aria)}")
        print(f"graficos con canvas sin pintar: {len(sin_pintar)}")

        print("textos de la tarjeta de resultado:")
        for texto in await pagina.evaluate(
            """
            () => {
              const seccion = Array.from(document.querySelectorAll('section.content-card'))
                .find((s) => s.getAttribute('aria-labelledby') === 'run-findings-title');
              return seccion ? seccion.innerText.split('\\n').filter(Boolean) : ['NO ESTA'];
            }
            """
        ):
            print(f"  {texto}")

        destino = os.path.join(SALIDA, "cli-pentest-detalle-hallazgos.png")
        await pagina.screenshot(path=destino, full_page=True)
        print(f"captura {destino}")
        for aviso in avisos:
            print(f"  {aviso}")
        await navegador.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("uso: _capturar_escaneo_con_hallazgos.py <run_id>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))