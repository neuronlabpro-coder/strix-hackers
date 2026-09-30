"""Pruebas de las cabeceras de seguridad que sirve nginx.

## Por qué un test sobre un fichero de nginx

Porque el fichero **es** el control. `deploy/security-headers.conf` no se genera, no se
construye y no se interpola: lo copia el `Dockerfile.frontend` tal cual a
`/etc/nginx/snippets/`, y lo que no se comprueba en la revisión se comprueba en producción, o
no se comprueba nunca.

## Los tres defectos que fija

1. **La CSP se desincroniza del dominio de la API.** `connect-src` tiene el origen de la API
   escrito a mano. Si el dominio cambia —un staging, un dominio nuevo— el panel sigue
   bundleando la URL nueva y el navegador le corta las peticiones: el fallo aparece como «el
   panel no carga datos», que no dice nada sobre la CSP. Aquí se contrasta contra
   `API_PUBLIC_BASE_URL`, que es donde el dominio está declarado de verdad.

2. **`connect-src` con un comodín.** `*` en `connect-src` permite que cualquier página que se
   ejecute en el origen del panel mande las peticiones que quiera, y el token vive en
   `sessionStorage` de ese mismo origen. Un comodín ahí no es una comodidad: es la diferencia
   entre un token que sale a un dominio y un token que sale a cualquiera.

3. **Los assets sin cabeceras.** En nginx un `add_header` dentro de un `location` **anula**
   todos los del padre, no se suman. El `location /assets/` declara su propio
   `Cache-Control`, y con ese `add_header` presente el `include` del nivel `server` deja de
   aplicarse: los `.js` y `.css` salen sin CSP, sin `nosniff` y sin `Referrer-Policy`.

## Por qué se lee el fichero y no se arranca nginx

Porque un `nginx -t` solo comprueba que la sintaxis sea válida, que no es lo que se vigila
aquí. Y porque arrancar un contenedor para leer una cabecera es una prueba que tarda más de lo
que explica y que además necesita Docker, que no está en todas partes.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

#: Raíz del repositorio. El fichero vive en `deploy/`, tres niveles por encima de `backend/`.
RAIZ = Path(__file__).resolve().parents[2]
CABECERAS = RAIZ / "deploy" / "security-headers.conf"
NGINX = RAIZ / "deploy" / "nginx.conf"
PLANTILLA = RAIZ / ".env.example"

#: Cabeceras que el fichero tiene que declarar en todas partes. Se comparan con `in` porque
#: su presencia es lo que importa, no su orden ni su puntuación.
OBLIGATORIAS = (
    "default-src",
    "script-src",
    "style-src",
    "img-src",
    "object-src 'none'",
    "base-uri",
    "frame-ancestors 'none'",
    "Referrer-Policy",
    "X-Content-Type-Options",
    "Permissions-Policy",
)


def _texto(fichero: Path) -> str:
    assert fichero.exists(), f"{fichero} no existe: el test no vigila nada"
    return fichero.read_text(encoding="utf-8")


def _connect_src() -> str:
    """El valor de `connect-src`, tal cual, incluido el `;` que lo cierra.

    ## Por qué el ancla al principio de la línea, y no la palabra suelta

    Porque el fichero **explica** la directiva en comentarios, y varios de ellos vuelven a
    escribir `connect-src`. Buscando la palabra suelta, la primera coincidencia es el comentario
    que dice «`connect-src` permite el origen de la API porque…» y el test compara un
    comentario con una URL. Pasó al escribirlo: el primer test falló exactamente por eso.

    Es el mismo motivo por el que hay que mirar la directiva y no el fichero entero: lo que se
    vigila es lo que nginx lee, y nginx no lee comentarios. El patrón es `^[ \t]*connect-src`
    con `re.MULTILINE`: los espacios o tabuladores opcionales aceptan que nginx la escriba
    indentada —que es como está, cuatro espacios— y la ausencia de `#` descarta los
    comentarios, que empiezan por `#` y no por la palabra.

    ## Por qué los espacios importan y no es un detalle

    Porque el primer intento fue `^connect-src` sin ellos, y falló: la directiva está
    indentada. Un ancla demasiado estricta da un fallo que parece «falta la CSP» y en realidad
    dice «el patrón no encuentra la directiva», que son dos problemas distintos y se
    confunden. Por eso el mensaje de aserción dice qué_directiva falta y no qué_directiva se
    busca.
    """

    coincidencia = re.search(
        r"^[ \t]*connect-src([^;]*);", _texto(CABECERAS), flags=re.MULTILINE
    )
    assert coincidencia is not None, "no hay ninguna directiva connect-src en el fichero"
    return coincidencia.group(1)


def _origen_de_la_api_de_ejemplo() -> str:
    """El origen declarado en la línea de producción de `.env.example`.

    ## Por qué se lee esa línea y no la de arriba del todo

    Porque `API_PUBLIC_BASE_URL` aparece dos veces en la plantilla: una para desarrollo
    —`http://localhost:8000`— y otra comentada, dentro del bloque que explica Dokploy, con el
    dominio real. Comparar contra la de desarrollo daría un fallo siempre, y comparar contra
    «la primera que appears» leería la que no es.

    Se busca la línea comentada que lo contiene, que es donde el dominio de producción está
    escrito para quien despliega.
    """

    for linea in _texto(PLANTILLA).splitlines():
        if linea.lstrip().startswith("#") and "API_PUBLIC_BASE_URL=https://" in linea:
            return linea.split("=", 1)[1].strip()
    pytest.fail("no hay ninguna línea comentada con API_PUBLIC_BASE_URL=https:// en .env.example")


# --------------------------------------------------------------------------- #
# La CSP y el dominio de la API
# --------------------------------------------------------------------------- #


def test_la_csp_no_usa_un_comodin_en_connect_src() -> None:
    """`*` en `connect-src` deja salir el token a cualquier origen.

    El token de sesión vive en `sessionStorage` del origen del panel, así que un `*` en
    `connect-src` no es una cabecera floja: es la condición para que una página inyectada en
    el panel se lo lleve a un servidor ajeno.
    """

    valor = _connect_src()
    ## Por qué se miran las dos formas del comodín
    #
    # Porque en una CSP el comodín se escribe de dos maneras y las dos valen: `*` a secas, que es
    # lo que produce una plantilla que concatena fuentes, y `'*'`, que es lo que escribe quien
    # la copia de una guía. El aserto miraba solo la segunda con comillas y dejó pasar la
    # primera: se vio al poner `* data:` en el fichero y ver `13 passed`.
    #
    # Y se comparan Tokens, no subcadenas, porque un subcadiente también casaría con un
    # `https://*` de una fuente legítima, que no es lo mismo que un comodín de origen.
    fuentes = valor.split()
    assert "*" not in fuentes, f"connect-src tiene un comodín de origen: {valor.strip()}"
    assert "'*'" not in fuentes, f"connect-src tiene un comodín de origen: {valor.strip()}"
    assert "http:" not in fuentes and "http://" not in valor, (
        f"connect-src permite http plano, que se puede interceptar: {valor.strip()}"
    )
    # Y `data:` tampoco: permite meter HTML en el documento desde una respuesta de la API.
    assert "data:" not in fuentes, (
        f"connect-src permite data:, que ejecuta lo que le manden: {valor.strip()}"
    )


def test_el_origen_de_la_api_de_la_csp_es_el_de_la_configuracion() -> None:
    """El origen de `connect-src` es el de `API_PUBLIC_BASE_URL`, y no se puede desincronizar.

    ## Por qué este es el test que más ha evitado problemas

    Porque el otro sentido de la comprobación —«que la CSP exista»— lo da el ojo en la revisión,
    y el que importa es que la CSP **no se quede atrás** cuando cambia el dominio. Un `git
    diff` del dominio en `.env.example` es fácil de pasar por alto; este test no lo deja pasar.

    ## Por qué el mensaje tiene que decir los dos valores

    Porque «no coinciden» obliga a abrir dos ficheros y compararlos a mano, y eso es lo que
    hace que un test como este se ignore la primera vez que salta. Los dos valores en el
    mensaje hacen el trabajo en el sitio donde se ve.
    """

    esperado = _origen_de_la_api_de_ejemplo()
    declarado = _connect_src().split()
    origen_declarado = next((p for p in declarado if p.startswith("https://")), None)

    assert origen_declarado is not None, (
        f"connect-src no declara ningún origen https: {_connect_src().strip()}"
    )
    assert origen_declarado == esperado, (
        f"la CSP permite {origen_declarado} pero la API se sirve en {esperado}. "
        "Si has cambiado el dominio, actualiza también connect-src en "
        "deploy/security-headers.conf, o el panel no podrá llamar a la API."
    )
    # Y que la URL de la plantilla sea de verdad una URL, no un texto con una arroba.
    assert urlsplit(esperado).scheme == "https", (
        f"API_PUBLIC_BASE_URL de producción no es https: {esperado}"
    )


# --------------------------------------------------------------------------- #
# Las cabeceras están, todas
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cabecera", OBLIGATORIAS)
def test_la_cabecera_esta_declarada(cabecera: str) -> None:
    """Cada cabecera obligatoria aparece en el fichero.

    Se parametriza para que un fallo diga **cuál** falta, en vez de uno solo que haya que ir a
    buscar. Una lista en una aserción única informa «no está la CSP» cuando lo que falta es la
    `Referrer-Policy`, y quien lee el fallo tiene que abrir el fichero para saber cuál era.
    """

    texto = _texto(CABECERAS)
    assert cabecera in texto, f"{cabecera} no está en deploy/security-headers.conf"


# --------------------------------------------------------------------------- #
# El `location` de assets
# --------------------------------------------------------------------------- #


def test_el_location_de_assets_repite_las_cabeceras() -> None:
    """El `location /assets/` vuelve a incluir el fichero de cabeceras.

    ## Por qué este fallo es invisible en el navegador

    Porque sin `add_header` en el nivel `server` no se pierde nada **visible**: la página carga,
    el estilo se aplica, la API responde. Lo que se pierde es la CSP y el `nosniff` de los
    ficheros estáticos, y ninguno de los dos da un error: simplemente no están. Por eso lo
    vigila un test y no una revisión.

    ## Por qué se mira el `location` y no el `server`

    Porque la herencia de `add_header` en nginx es de todo o nada: en cuanto un `location`
    declara uno, los del padre dejan de aplicarse para ese `location`. El `location /assets/`
    declara su `Cache-Control`, así que necesita su propio `include`.
    """

    texto = _texto(NGINX)
    coincidencia = re.search(
        r"location /assets/ \{(.*?)\n    \}", texto, flags=re.DOTALL
    )
    assert coincidencia is not None, "no hay ningún location /assets/ en deploy/nginx.conf"

    bloque = coincidencia.group(1)
    assert "security-headers" in bloque, (
        "el location /assets/ declara su propio add_header, que anula los del padre, "
        "y no vuelve a incluir security-headers.conf: los assets salen sin CSP ni nosniff"
    )
    # Y que la ruta del include sea la que el Dockerfile copia de verdad.
    assert "snippets" in bloque, (
        "el include apunta fuera de /etc/nginx/snippets/, que es donde "
        "Dockerfile.frontend copia el fichero: nginx arrancaria con un include no encontrado"
    )
