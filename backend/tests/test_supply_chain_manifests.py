"""Pruebas del parser de manifiestos.

## Qué se comprueba y por qué

Dos cosas, y la segunda es la que no se puede negociar.

La primera es la cobertura de los cuatro formatos: un parser que funciona con `package.json` y
falla con `go.mod` no es un parser, es un caso particular con buena prensa.

La segunda es la **limpieza de credenciales**. Un manifiesto **trae tokens con frecuencia** —
una URL directa de un registro privado, un `git+https://ghp_...@github.com/...` copiado de la
documentación de un proveedor— y si una de esas cadenas llega a la base el token queda
persistido en un `Text` que nadie va a buscar, y el mecanismo de rotación del que depende el
cliente deja de funcionar. Por eso la limpieza tiene más pruebas que el resto del parser
juntos: es la parte donde un fallo es caro y silencioso.
"""

from __future__ import annotations

import json

import pytest

from backend.apps.supply_chain.manifests import (
    REDACTADO,
    clave_nombre_crates_io,
    detectar_ecosistema,
    limpiar_credenciales,
    parsear_manifiesto,
)
from backend.apps.supply_chain.models import EcosystemEnum

pytestmark = pytest.mark.asyncio


def _por_nombre(resultado, ecosystem: EcosystemEnum) -> dict:
    return {d.name: d for d in resultado.dependencies if d.ecosystem is ecosystem}


# --------------------------------------------------------------------------- #
# La limpieza de credenciales. Va primero porque es lo que protege.
# --------------------------------------------------------------------------- #


async def test_una_url_con_usuario_y_token_no_deja_el_token() -> None:
    """La forma mas comun: una URL directa a un registro privado.

    Es la que aparece cuando alguien copia un comando de instalacion de la documentacion de un
    proveedor, que es exactamente cuando el token se cuela en el repositorio.
    """

    limpio = limpiar_credenciales(
        "pkg @ https://deploy:ghp_16CaracteresDeEjemplo0@registry.internal/pkg.whl"
    )
    assert "ghp_16CaracteresDeEjemplo0" not in limpio
    assert "deploy" not in limpio
    # El esquema y el host se conservan: la URL sigue siendo legible para quien la lea, y lo
    # que se quita es solo lo que autentica.
    assert limpio.startswith("pkg @ https://")
    assert "registry.internal" in limpio
    assert REDACTADO in limpio


async def test_un_token_de_github_suelto_se_sustituye() -> None:
    """Un token que aparece sin URL, por ejemplo en un comentario de configuracion."""

    limpio = limpiar_credenciales('{"_authToken": "github_pat_11ABCDEFG0abcdefghij"}')
    assert "github_pat_11ABCDEFG0abcdefghij" not in limpio
    assert REDACTADO in limpio


async def test_una_cabecera_authorization_no_sobrevive() -> None:
    limpio = limpiar_credenciales("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.firma")
    assert "eyJhbGciOiJIUzI1NiJ9" not in limpio
    assert limpio.startswith("Authorization: Bearer ")


async def test_un_token_en_query_se_sustituye() -> None:
    limpio = limpiar_credenciales("https://api.example.com/x?access_token=abc123def456&otro=1")
    assert "abc123def456" not in limpio
    # El otro parametro se conserva: la limpieza no mutila lo que no es secreto.
    assert "otro=1" in limpio


async def test_un_token_de_gitlab_se_sustituye() -> None:
    limpio = limpiar_credenciales("https://oauth2:glpat-ABCDEFGHIJKLMNOP12@gitlab.com/x.git")
    assert "glpat-ABCDEFGHIJKLMNOP12" not in limpio


async def test_lo_que_no_es_creencial_no_se_toca() -> None:
    """Un texto normal tiene que salir **igual**.

    Es la contrapartida de las anteriores: una limpieza que deforma lo que no es secreto rompe el
    inventario y no se nota, porque el nombre de un paquete raro simplemente desaparece.
    """

    original = '{"name": "mi-paquete", "version": "1.0.0", "url": "https://example.com/docs"}'
    assert limpiar_credenciales(original) == original


async def test_ningun_token_del_manifiesto_llega_a_las_dependencias() -> None:
    """La prueba de extremo a extremo de la proteccion.

    No basta con que `limpiar_credenciales` funcione: hay que comprobar que **el parser** lo
    aplica. Un parser que limpia y despues vuelve a ensuciar el resultado —porque construye una
    URL con el nombre original— pasa la prueba de la funcion y falla esta.
    """

    manifiesto = "\n".join(
        [
            "requests==2.31.0",
            "https://deploy:ghp_16CaracteresDeEjemplo0@registry.internal/pkg.whl",
            "git+https://ghp_OtroTokenDePrueba12345@github.com/org/repo.git#egg=privado",
        ]
    )
    resultado = parsear_manifiesto("requirements.txt", manifiesto)
    volcado = json.dumps(
        [{"name": d.name, "version": d.version} for d in resultado.dependencies]
    )
    assert "ghp_" not in volcado
    assert "deploy" not in volcado
    # Y las dos lineas con URL se descartan, no se convierten en paquetes inventados.
    assert [d.name for d in resultado.dependencies] == ["requests"]
    assert resultado.discarded == 2


# --------------------------------------------------------------------------- #
# package.json
# --------------------------------------------------------------------------- #


async def test_package_json_separa_produccion_de_desarrollo() -> None:
    """La distincion mas importante en supply chain y la que un inventario plano esconde.

    Una vulnerabilidad en `devDependencies` no llega a produccion, y sin la columna el usuario
    tiene que abrir el manifiesto a mano para averiguarlo.
    """

    manifiesto = json.dumps(
        {
            "dependencies": {"express": "^4.17.21", "axios": "~1.6.0"},
            "devDependencies": {"typescript": "5.4.0", "jest": "^29.0.0"},
        }
    )
    por_nombre = _por_nombre(parsear_manifiesto("package.json", manifiesto), EcosystemEnum.NPM)

    assert por_nombre["express"].is_dev is False
    assert por_nombre["axios"].is_dev is False
    assert por_nombre["typescript"].is_dev is True
    assert por_nombre["jest"].is_dev is True


async def test_el_rango_de_version_se_guarda_entero() -> None:
    """Se guarda lo que el manifiesto **declara**, no una version resuelta.

    Un rango mas largo significa menos informacion de auditoria, y tragarselo para dejar
    `4.17.21` seria mentir: el proyecto no usa `4.17.21`, usa "cualquier 4.x desde 4.17.21".
    El panel lo muestra tal cual para que el usuario vea el rango.
    """

    manifiesto = json.dumps({"dependencies": {"express": "^4.17.21"}})
    resultado = parsear_manifiesto("package.json", manifiesto)
    assert resultado.dependencies[0].version == "^4.17.21"


async def test_una_dependencia_local_se_descarta_y_se_cuenta() -> None:
    """Un `file:../otro` no es un paquete de un registro.

    No se puede afirmar nada de el y contarlo como dependencia haria que el usuario buscara en
    un registro un paquete que no existe. Se descarta **y se cuenta**, para que el panel pueda
    decir que hubo algo que no se pudo clasificar en vez de fingir que el manifiesto no lo tenia.
    """

    manifiesto = json.dumps(
        {"dependencies": {"express": "^4.17.21", "local": "file:../otro"}}
    )
    resultado = parsear_manifiesto("package.json", manifiesto)
    assert [d.name for d in resultado.dependencies] == ["express"]
    assert resultado.discarded == 1


async def test_la_licencia_se_toma_de_la_raiz_en_las_dos_formas() -> None:
    """npm acepta texto y objeto, y la mitad de los manifiestos usan una.

    Con solo la forma texto, la mitad de los proyectos reales darian `NULL` y la cobertura de
    licencias —una de las razones de esta tabla— no serviria para nada.
    """

    texto = parsear_manifiesto(
        "package.json", json.dumps({"license": "MIT", "dependencies": {"a": "1.0.0"}})
    )
    objeto = parsear_manifiesto(
        "package.json", json.dumps({"license": {"type": "MIT"}, "dependencies": {"a": "1.0.0"}})
    )
    sin_licencia = parsear_manifiesto(
        "package.json", json.dumps({"dependencies": {"a": "1.0.0"}})
    )
    assert texto.dependencies[0].license == "MIT"
    assert objeto.dependencies[0].license == "MIT"
    # Sin licencia se queda en `None` y no en "UNKNOWN": medir la cobertura exige poder contar
    # los huecos sin inventar su contenido.
    assert sin_licencia.dependencies[0].license is None


async def test_un_package_json_roto_no_rompe_el_parseo() -> None:
    """Un manifiesto con un error de sintaxis deja el repositorio sin inventario.

    Se queda sin dependencias y **cuenta** el descarte. Devolver cero sin decir nada seria
    indistinguible de "este proyecto no tiene dependencias", que es una afirmacion distinta y
    falsa.
    """

    resultado = parsear_manifiesto("package.json", "{ esto no es json")
    assert resultado.dependencies == ()
    assert resultado.discarded == 1
    # El ecosistema se sigue sabiendo: lo dice el nombre del fichero.
    assert resultado.ecosystem is EcosystemEnum.NPM


async def test_una_dependencia_repetida_no_duplica_la_fila() -> None:
    """`package.json` puede declarar el mismo paquete en `dependencies` y `devDependencies`.

    Sin deduplicar, el inventario contaria de mas y el usuario veria un total que no cuadra con
    las filas, que es la forma mas rapida de que deje de fiarse de la tabla.
    """

    manifiesto = json.dumps(
        {"dependencies": {"a": "1.0.0"}, "devDependencies": {"a": "1.0.0"}}
    )
    resultado = parsear_manifiesto("package.json", manifiesto)
    assert len(resultado.dependencies) == 1


# --------------------------------------------------------------------------- #
# requirements.txt
# --------------------------------------------------------------------------- #


async def test_requirements_acepta_extras_marcadores_y_comentarios() -> None:
    """Las tres cosas que se ven en un `requirements.txt` real y que rompen un parser ingenuo."""

    manifiesto = "\n".join(
        [
            "# Dependencias de produccion",
            "",
            "requests[socks]>=2.31.0",
            "flask==3.0.0  ; python_version >= '3.8'",
            "urllib3~=2.2.0",
        ]
    )
    resultado = parsear_manifiesto("requirements.txt", manifiesto)
    por_nombre = _por_nombre(resultado, EcosystemEnum.PYPI)

    assert set(por_nombre) == {"requests", "flask", "urllib3"}
    assert por_nombre["requests"].version == "2.31.0"
    assert por_nombre["flask"].version == "3.0.0"
    assert por_nombre["urllib3"].version == "2.2.0"


async def test_una_dependencia_sin_version_no_inventa_una() -> None:
    """Un requisito sin pinar significa "la que haya", y se guarda vacio.

    Guardar `*` haria que el panel mostrara un asterisco como si fuera una version, y el usuario
    no podria distinguir "sin fijar" de "fijada a algo raro".
    """

    resultado = parsear_manifiesto("requirements.txt", "requests\nflask==3.0.0")
    por_nombre = _por_nombre(resultado, EcosystemEnum.PYPI)
    assert por_nombre["requests"].version == ""
    assert por_nombre["flask"].version == "3.0.0"


async def test_una_dependencia_editable_local_se_descarta() -> None:
    """`-e .` es el propio proyecto, no un paquete del registro."""

    resultado = parsear_manifiesto("requirements.txt", "-e .\nrequests==2.31.0")
    assert [d.name for d in resultado.dependencies] == ["requests"]
    assert resultado.discarded == 1


async def test_requirements_deduplica_por_nombre_igual_que_la_tabla() -> None:
    """`requirements.txt` admite la misma dependencia fijada y sin fijar.

    Se queda con la primera, que es la que el archivo lista arriba y por tanto la que el gestor
    usa al resolver. Y la clave de deduplicado es `(nombre, ecosistema)`, **no**
    `(nombre, version)`, porque tiene que coincidir con `uq_supply_chain_repo_name_ecosystem`.

    Con la version en la clave, estas dos lineas producen dos entradas del mismo paquete —la
    segunda con la version vacia—, el panel contaria dos donde hay uno, y al persistir la
    segunda fila la base rechazaria el `INSERT` por la restriccion unica. Es un fallo que
    apareceria en el primer repositorio con un `requirements.txt` asi.
    """

    resultado = parsear_manifiesto("requirements.txt", "requests==2.31.0\nrequests")
    assert len(resultado.dependencies) == 1
    # Y se conserva la primera, que es la fijada.
    assert resultado.dependencies[0].version == "2.31.0"


# --------------------------------------------------------------------------- #
# go.mod
# --------------------------------------------------------------------------- #


async def test_go_mod_lee_el_bloque_require() -> None:
    manifiesto = "\n".join(
        [
            "module github.com/cliente/api",
            "",
            "go 1.22",
            "",
            "require (",
            "\tgithub.com/gin-gonic/gin v1.9.1",
            "\tgolang.org/x/crypto v0.21.0 // indirect",
            ")",
            "",
            "require github.com/stretchr/testify v1.8.4",
        ]
    )
    resultado = parsear_manifiesto("go.mod", manifiesto)
    por_nombre = _por_nombre(resultado, EcosystemEnum.GO)

    assert set(por_nombre) == {
        "github.com/gin-gonic/gin",
        "golang.org/x/crypto",
        "github.com/stretchr/testify",
    }
    assert por_nombre["github.com/gin-gonic/gin"].version == "v1.9.1"


async def test_go_mod_no_confunde_la_version_del_lenguaje_con_un_paquete() -> None:
    """`go 1.22` declara la version del lenguaje.

    Sin el filtro, el inventario tendria un paquete llamado `go` version `1.22`, y al buscar
    vulnerabilidades de ese "paquete" el resultado seria ruido.
    """

    resultado = parsear_manifiesto("go.mod", "module x\n\ngo 1.22\n")
    assert resultado.dependencies == ()


# --------------------------------------------------------------------------- #
# Cargo.toml
# --------------------------------------------------------------------------- #


async def test_cargo_toml_solo_lee_las_secciones_de_dependencias() -> None:
    """Una tabla `[package]` con `version` no es una dependencia.

    Es el error mas facil de cometer con un parser de TOML ingenuo, y produce una fila que dice
    que el proyecto depende de si mismo.
    """

    manifiesto = "\n".join(
        [
            "[package]",
            'name = "mi-crate"',
            'version = "0.3.0"',
            "",
            "[dependencies]",
            'serde = "1.0"',
            "",
            "[dev-dependencies]",
            'criterion = "0.5"',
        ]
    )
    resultado = parsear_manifiesto("Cargo.toml", manifiesto)
    por_nombre = _por_nombre(resultado, EcosystemEnum.CARGO)

    assert set(por_nombre) == {"serde", "criterion"}
    assert por_nombre["criterion"].is_dev is True
    assert por_nombre["serde"].is_dev is False


async def test_cargo_normaliza_el_nombre_del_crate() -> None:
    """`serde_json` y `serde-json` son el mismo paquete, y el aviso de seguridad usa guiones.

    Guardar la forma del `Cargo.toml` haria que la busqueda por nombre no encontrase el paquete
    cuando el usuario lo busca como aparece en el aviso.
    """

    manifiesto = "[dependencies]\nserde_json = \"1.0\"\n"
    resultado = parsear_manifiesto("Cargo.toml", manifiesto)
    assert [d.name for d in resultado.dependencies] == ["serde-json"]
    assert clave_nombre_crates_io("serde_json") == "serde-json"


# --------------------------------------------------------------------------- #
# La deteccion de ecosistema
# --------------------------------------------------------------------------- #


async def test_detectar_ecosistema_por_ruta_completa() -> None:
    """La API del proveedor devuelve la ruta completa del fichero, no su nombre suelto."""

    assert detectar_ecosistema("package.json") is EcosystemEnum.NPM
    assert detectar_ecosistema("apps/api/package.json") is EcosystemEnum.NPM
    assert detectar_ecosistema("servidor\\go.mod") is EcosystemEnum.GO
    assert detectar_ecosistema("crates/cli/Cargo.toml") is EcosystemEnum.CARGO


async def test_un_manifiesto_desconocido_es_otra_cosa_y_no_un_error() -> None:
    """Un `Dockerfile` tiene que producir una respuesta, no una excepcion.

    Lanzar aqui haria que un repositorio sin manifiesto reconocible no tuviera inventario, que
    es la mayoria de los que no son una aplicacion.
    """

    assert detectar_ecosistema("Dockerfile") is EcosystemEnum.OTHER
    resultado = parsear_manifiesto("Dockerfile", "FROM python:3.12")
    assert resultado.ecosystem is EcosystemEnum.OTHER
    assert resultado.dependencies == ()


async def test_un_manifiesto_enorme_se_rechaza() -> None:
    """Un manifiesto de megabytes no es un manifiesto, y parsearlo es trabajo que alguien puede
    usar para agotar la memoria del proceso."""

    with pytest.raises(ValueError, match="excede"):
        parsear_manifiesto("package.json", "x" * 3_000_000)
