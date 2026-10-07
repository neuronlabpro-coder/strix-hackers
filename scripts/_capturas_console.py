"""Capturas de las páginas de la consola, con el Chrome ya instalado en la máquina.

## Por qué esto existe

Porque no hay explorador conectado a la sesión, y la queja que motiva el rediseño era visual.
Analizar el CSS y los tipos no dice si una pantalla se ve bien; solo mirándola.

## Por qué Playwright con el Chrome del sistema y no el suyo

Porque `playwright install` descarga un navegador de unos 150 MB. El Chrome de la máquina hace lo
mismo, ya está instalado y ya pasa por la configuración del sistema. Con `channel="chrome"` no se
descarga nada.

## Por qué se inicia sesión por el formulario y no inyectando la sesión

Porque la sesión vive en `sessionStorage` con claves propias del proyecto (`lib/session.ts`), y
rellenarlas a mano ata el script a dos nombres de constante que cambiarán. Rellenar el formulario es
lo que hace una persona, no depende de nada y de paso comprueba que el login sigue funcionando.

## Qué deja

Un PNG por página en `capturas/`, más un resumen de los errores de consola y de red. Los errores de
consola son lo que delata un `undefined` que en la pantalla no se ve pero en el DOM sí.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "capturas")
ORG_ID = "2ce4e2c2-d900-4eb5-82a5-3d970013667d"
#: Cuenta de la base de demostracion. Verificada: `user1@mindguard.tech` existe, esta activa y es
#: ADMIN de la organizacion de arriba.
#:
#: ## Por que no la cuenta anterior
#:
#: Porque `demo.admin@acmesecurity.io` **no existe** en `fenix_team_dev`. El login fallaba y el
#: script se caia en el primer paso, lo que hacia que no hubiera ninguna captura que mirar: el
#: informe "sin errores" de una ejecucion que no llego a abrir el navegador es peor que no tener
#: informe, porque parece que todo esta bien.
#:
#: Y las credenciales se verifican contra la base antes de escribirlas aqui: una cuenta que se
#: inventa no es un dato, es una suplantacion con nombre de usuario.
CORREO = "user1@mindguard.tech"
CLAVE = "UserPass2026!"

#: Las páginas de la consola de SuperAdmin, con el nombre del fichero y si llegan a esperar datos.
#:
#: ## Por qué van aparte de las del panel
#:
#: Porque la cuenta de demostracion **no es superusuario**, y las rutas `/admin/*` exigen ese
#: permiso. Se recorren igual: lo que se busca en ellas son errores de consola y respuestas 4xx de
#: la propia pantalla, y una pantalla a la que se llega sin permiso enseña justo eso —la redirección
#: y el aviso—, que también es información. Lo que **no** se puede es presentar esas capturas como
#: la consola funcionando, y por eso el informe dice qué cuenta se usó.
PAGINAS_CONSOLA: list[tuple[str, str]] = [
    ("cons-operaciones", "/admin/operations"),
    ("cons-precios", "/admin/pricing"),
    ("cons-organizaciones", "/admin/tenants"),
    ("cons-usuarios", "/admin/users"),
    ("cons-tickets", "/admin/tickets"),
    ("cons-auditoria", "/admin/audit"),
    ("cons-agentes", "/admin/agents"),
    ("cons-ventas", "/admin/sales"),
    ("cons-ficha-pactados", f"/admin/organizations/{ORG_ID}/pricing"),
]

#: Las páginas del panel de cliente. Se revisan con el mismo script y el mismo Chrome porque el
#: encargo es "todas las páginas", y porque el defecto que se busca --botones pegados, iconos
#: descolocados, texto grande-- es del vocabulario compartido, no de una pantalla concreta.
PAGINAS_CLIENTE: list[tuple[str, str]] = [
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

#: Las dos págebras fuera del shell. Se capturan sin sesión a propósito.
PAGINAS_PUBLICAS: list[tuple[str, str]] = [
    ("pub-login", "/login"),
]


def pedir_token() -> str:
    """Un token de la API, para comprobar que el backend responde antes de abrir el navegador."""

    peticion = urllib.request.Request(
        f"{BASE}/api/v1/auth/login",
        data=('{"email":"%s","password":"%s"}' % (CORREO, CLAVE)).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        import json

        return json.loads(respuesta.read())["access_token"]


async def principal() -> int:
    token = pedir_token()
    print("  backend responde: token de %d caracteres" % len(token))
    print("  sesion de %s; las rutas /admin/* requieren superusuario y esta cuenta no lo es" % CORREO)

    os.makedirs(SALIDA, exist_ok=True)
    problemas: list[str] = []

    async with async_playwright() as p:
        navegador = await p.chromium.launch(channel="chrome", headless=True)
        contexto = await navegador.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=1,
            locale="es-ES",
        )
        pagina = await contexto.new_page()

        pagina.on(
            "console",
            lambda m: problemas.append(f"[console:{m.type}] {m.text}")
            if m.type in ("error", "warning")
            else None,
        )
        pagina.on("pageerror", lambda e: problemas.append(f"[pageerror] {e}"))
        pagina.on(
            "response",
            lambda r: problemas.append(f"[red:{r.status}] {r.url}") if r.status >= 400 else None,
        )

        await pagina.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await pagina.fill('input[type="email"]', CORREO)
        await pagina.fill('input[type="password"]', CLAVE)
        await pagina.click('button[type="submit"]')
        try:
            await pagina.wait_for_url(lambda u: "/login" not in u, timeout=25000)
        except Exception as exc:  # noqa: BLE001
            print("  el login no salio de /login: %s" % exc)
            await navegador.close()
            return 1
        print("  sesion iniciada")

        total = 0
        for nombre, ruta in PAGINAS_CONSOLA + PAGINAS_CLIENTE:
            antes = len(problemas)
            await pagina.goto(f"{BASE}{ruta}", wait_until="domcontentloaded")
            try:
                await pagina.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001, S110
                pass
            await pagina.wait_for_timeout(700)
            destino = os.path.join(SALIDA, f"{nombre}.png")
            await pagina.screenshot(path=destino, full_page=True)
            nuevos = len(problemas) - antes
            total += 1
            print(
                "  %-24s %7d bytes%s"
                % (nombre, os.path.getsize(destino),
                   "  (%d avisos nuevos)" % nuevos if nuevos else "")
            )

            # ## Por qué se pulsan las pestañas y no basta con abrir la URL
            #
            # Porque una URL solo enseña la primera pestaña. Operaciones tiene cuatro y las
            # otras tres no llegan a renderizarse hasta que se pulsan, así que sus errores de
            # consola y sus respuestas 4xx no aparecen en el informe si nadie las pulsa. El
            # encargo es revisar el flujo de cada pestaña, y una pestaña no pulsada es un flujo
            # sin comprobar.
            #
            # Los avisos se atribuyen a la pestaña con su nombre porque si no, un error que
            # solo aparece en "Sondas" quedaría atribuido a la página entera y costaría media hora
            # media hora encontrarlo.
            etiquetas = await pagina.eval_on_selector_all(
                '[role="tab"]', "els => els.map(e => (e.textContent || '').trim()).filter(Boolean)"
            )
            for etiqueta in etiquetas:
                try:
                    await pagina.click(f'[role="tab"]:has-text("{etiqueta}")', timeout=4000)
                except Exception:  # noqa: BLE001, S110
                    continue
                await pagina.wait_for_timeout(800)
                antes_pestana = len(problemas)
                destino_p = os.path.join(SALIDA, "%s--%s.png" % (nombre, etiqueta))
                await pagina.screenshot(path=destino_p, full_page=True)
                nuevos_p = len(problemas) - antes_pestana
                total += 1
                print(
                    "    %-20s %7d bytes%s"
                    % (etiqueta, os.path.getsize(destino_p),
                       "  (%d avisos nuevos)" % nuevos_p if nuevos_p else "")
                )

        limpio = await navegador.new_context(
            viewport={"width": 1600, "height": 1000}, locale="es-ES"
        )
        publica = await limpio.new_page()
        publica.on(
            "response",
            lambda r: problemas.append(f"[red:{r.status}] {r.url}") if r.status >= 400 else None,
        )
        for nombre, ruta in PAGINAS_PUBLICAS:
            await publica.goto(f"{BASE}{ruta}", wait_until="domcontentloaded")
            try:
                await publica.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001, S110
                pass
            await publica.wait_for_timeout(500)
            ruta_png = os.path.join(SALIDA, f"{nombre}.png")
            await publica.screenshot(path=ruta_png, full_page=True)
            total += 1
            print("  %-24s %7d bytes" % (nombre, os.path.getsize(ruta_png)))

        await limpio.close()
        await navegador.close()

    if problemas:
        print()
        print("  %d aviso(s) de consola o red:" % len(problemas))
        for linea in dict.fromkeys(problemas):
            print("    %s" % linea[:150])
    else:
        print()
        print("  sin errores de consola ni respuestas 4xx/5xx en las %d páginas" % total)
    print("  %d capturas en %s" % (total, os.path.normpath(SALIDA)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
