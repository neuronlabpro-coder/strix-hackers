"""Lectura de manifiestos de dependencias y limpieza de lo que traen consigo.

## Qué problema resuelve

Un repositorio declara sus dependencias directas en un fichero de texto: `package.json`,
`requirements.txt`, `go.mod`, `Cargo.toml`. De ese fichero se puede afirmar **qué depende el
proyecto de cada paquete** sin instalar nada y sin tocar el contenedor de escaneo. Es el
inventario que se puede construir hoy, y es el paso anterior a un SBOM.

## Por qué no se persiste el manifiesto

R5: el código fuente del cliente no se persiste nunca, y un manifiesto es parte de ese código.
De aquí sale solo **nombre, versión, licencia y si es de desarrollo**. El texto no llega a la
base, y ni siquiera al rastro de auditoría.

## Por qué la limpieza de credenciales es **obligatoria** y no una precaución

Porque los manifiestos **traen credenciales con frecuencia**, y no en un caso raro:

- `requirements.txt` admite URL directas: `https://user:token@registry.internal/pkg.whl`, o
  `git+https://ghp_xxx@github.com/org/repo.git`. Un token de despliegue copiado y pegado desde
  la documentación de un proveedor está ahí, en claro, en la línea de arriba.
- `package.json` admite `_authToken` dentro de `.npmrc`, y algunos proyectos lo incluyen por
  error en el propio manifiesto.
- `go.mod` y `Cargo.toml` admiten `replace` con token en la URL.

Si una de esas cadenas llega a la base, el token **queda persistido en un `Text` que nadie va a
buscar**, y el mecanismo de alerta del que depende el cliente —rotar credenciales
de funcionar. Por eso `_limpiar_credenciales` se aplica a **todo** lo que sale del parser, sin
excepción y sin un interruptor para desactivarlo.

Es una medida de contencion, no una limpieza: un token dentro de una URL de dependencia sigue
siendo un token, y la función no pretende arreglarlo. Lo que hace es garantizar que lo que se
persiste nunca lo contiene.

## Por qué el parser es **tolerante** y no estricto

Porque los manifiestos del mundo real están rotos. Un `package.json` con una coma de más, un
`requirements.txt` con tres líneas de comentario y un `-e .` local, un `go.mod` con un `require`
de dos bloques: rejecting el fichero entero por un error de sintaxis deja el repositorio **sin
inventario**, que es el peor resultado. Lo que se hace es quedarse con lo que se entiende, y
registrar cuántos elementos se descartaron para que el panel pueda decirlo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from backend.apps.supply_chain.models import MANIFIESTO_POR_ECOSISTEMA, EcosystemEnum

#: Un limite de tamano por manifiesto. Un `package.json` de 4 MB no es un proyecto, es un
#: fichero generado, y parsearlo entero para extraer cuarenta nombres es trabajo que alguien
#: puede usar para agotar la memoria del proceso.
MAX_MANIFEST_CHARS: Final[int] = 2_000_000

#: Un token embebido en una URL. Se aplica a la URL **entera** lo que aparezca entre el esquema
#: y el host, mas cualquier cosa con forma de token conocido.
#:
#: Se cubren cuatro porque son los cuatro que aparecen en la practica: credenciales de usuario
#: (`https://user:pass@`), tokens de GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`),
#: bearer (`Authorization: Bearer x`) y query de token (`?access_token=`, `?private_token=`).
_CREDENCIAL_EN_URL: Final[re.Pattern[str]] = re.compile(
    r"(?P<esquema>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<credencial>[^/@\s]+)@"
)
_TOKEN_CONOCIDO: Final[re.Pattern[str]] = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,})"
)
_BEARER: Final[re.Pattern[str]] = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_QUERY_CON_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(?i)([?&](?:access_token|private_token|token|auth|api_key|apikey|password)=)[^&\s]+"
)

#: Marcador que sustituye a una credencial. Se elige una cadena **obvia** y no un hash: el
#: proposito es que nadie la confunda con un token real si aparece en un log.
REDACTADO: Final[str] = "[REDACTED]"

#: Una especificacion de requisito de pip, tal y como se escribe en el fichero.
#:
#: Se capturan por separado el nombre, el operador y la version para poder guardarlos en columnas
#: distintas. Guardar `>=2.0,<3.0` en la columna `version` haria que un `ORDER BY` ordenara
#: alfabeticamente y que una busqueda por version fallara en silencio.
_PIP: Final[re.Pattern[str]] = re.compile(
    r"^(?P<nombre>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<operador>===|==|!=|<=|>=|~=|<|>)?"
    r"\s*(?P<version>[^\s,;#]+)?"
)
#: `requests[socks]>=2.0` y `requests >= 2.0 ; python_version >= "3.8"` con marcadores de entorno.
_MARCADOR_ENTORNO: Final[re.Pattern[str]] = re.compile(r";.*$")
#: Las cuatro formas de dependencia local en pip: no se puede fijar una version.
_EDITABLE: Final[re.Pattern[str]] = re.compile(r"^(-e|--editable)\s+")
#: Un `require (...) { }` de go.mod y las lineas `github.com/x/y v1.2.3`.
_GO_REQUIRE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<nombre>[\w.-]+(?:/[\w.-]+)*)\s+(?P<version>v[^\s/]+)"
)
#: `serde = "1.0"` en `Cargo.toml`. El nombre de la clave puede llevar guiones, el del paquete
#  no, y se traduce de guiones a guiones bajos que es como se llama en crates.io.
_CARGO: Final[re.Pattern[str]] = re.compile(
    r'^\s*(?P<nombre>[A-Za-z0-9_-]+)\s*=\s*"(?P<version>[^"]+)"'
)


@dataclass(frozen=True, slots=True)
class DependenciaParseada:
    """Una dependencia directa leida de un manifiesto.

    `version` puede estar vacia: no todos los manifiestos la fijan, y `cargo` con un `path` no la
    tiene. Una dependencia sin version **no** se descarta, porque "el proyecto depende de esto" ya
    es informacion, pero se marca con la version vacia para que el panel no la presente como si
    estuviera fijada.
    """

    name: str
    version: str
    ecosystem: EcosystemEnum
    is_dev: bool = False
    license: str | None = None


@dataclass(frozen=True, slots=True)
class ResultadoDelParseo:
    """Lo que se ha extraido de un manifiesto, y lo que se ha dejado atrás."""

    dependencies: tuple[DependenciaParseada, ...]
    ecosystem: EcosystemEnum
    #: Manifestos que se **sintentaron** leer y no se pudieron entender. No es un error: es el
    #: dato que permite decir "hemos leido 1 de 2 manifiestos" en vez de "no hay datos".
    discarded: int = 0


def limpiar_credenciales(texto: str) -> str:
    """Elimina de un texto lo que parece una credencial.

    Se aplica a **todo** lo que sale del parser, no solo a los campos que se guardan. Es
    deliberado: un filtro por campo deja pasar lo que no se penso, y la lista de campos que
    "no se guardan" crece con cada formato nuevo que alguien anada.
    """

    limpio = _CREDENCIAL_EN_URL.sub(r"\g<esquema>" + REDACTADO + "@", texto)
    limpio = _TOKEN_CONOCIDO.sub(REDACTADO, limpio)
    limpio = _BEARER.sub("Bearer " + REDACTADO, limpio)
    return _QUERY_CON_TOKEN.sub(r"\1" + REDACTADO, limpio)


def detectar_ecosistema(ruta: str) -> EcosystemEnum:
    """El ecosistema de un manifiesto por su nombre de fichero.

    Devuelve `OTHER` para lo que no se reconoce en vez de lanzar. Un repositorio con un
    `Dockerfile` y nada mas tiene que producir **algo** o su inventario desaparece; y
    `OTHER` con lo que no se reconoce es informacion mas util que una excepcion.
    """

    nombre = ruta.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return MANIFIESTO_POR_ECOSISTEMA.get(nombre, EcosystemEnum.OTHER)


def parsear_manifiesto(ruta: str, contenido: str) -> ResultadoDelParseo:
    """Extrae las dependencias directas de un manifiesto.

    ## Por qué decide por el **contenido** y no solo por el nombre

    Porque un `package.json` mal formado no es un proyecto que no usa JavaScript: es un
    `package.json` que el parser de este modulo no entiende. Decidir por el nombre lanzaria un
    error que el llamador tendria que distinguir de "no hay manifiesto", y las dos cosas significan
    cosas distintas para el usuario.
    """

    if len(contenido) > MAX_MANIFEST_CHARS:
        raise ValueError(
            f"El manifiesto {ruta} excede {MAX_MANIFEST_CHARS} caracteres "
            f"({len(contenido)}); no es un fichero de dependencias"
        )

    nombre = ruta.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    limpio = limpiar_credenciales(contenido)

    if nombre == "package.json":
        return _parsear_package_json(limpio)
    if nombre == "requirements.txt":
        return _parsear_requirements(limpio)
    if nombre == "go.mod":
        return _parsear_go_mod(limpio)
    if nombre == "Cargo.toml":
        return _parsear_cargo_toml(limpio)

    return ResultadoDelParseo(dependencies=(), ecosystem=EcosystemEnum.OTHER)


# --------------------------------------------------------------------------- #
# package.json
# --------------------------------------------------------------------------- #


def _parsear_package_json(contenido: str) -> ResultadoDelParseo:
    try:
        carga = json.loads(contenido)
    except (TypeError, ValueError):
        return ResultadoDelParseo(dependencies=(), ecosystem=EcosystemEnum.NPM, discarded=1)

    if not isinstance(carga, dict):
        return ResultadoDelParseo(dependencies=(), ecosystem=EcosystemEnum.NPM, discarded=1)

    licencia = carga.get("license")
    licencia_texto = _texto_licencia(licencia)
    # Se lee **una sola vez** la licencia de la raiz, y se aplica a todas las dependencias.
    #
    # No es que todas las dependencias tengan la misma licencia: es que la mayoria de las
    # declaraciones de este proyecto no la ponen, y esta es la unica que el manifiesto da. Un
    # inventario con el 90% de las licencias a `NULL` no sirve para medir cobertura de licencias,
    # que es de las cosas por las que se mantiene esta tabla.
    #
    # Se marca como mejorable en la documentacion de la API en vez de dejarlo implicito: un
    # "MIT" junto a un paquete con licencia Apache-2.0 es un dato **aproximado**, y quien lo lea
    # tiene que poder saberlo.

    encontradas: dict[tuple[str, str], DependenciaParseada] = {}
    descartados = 0

    for seccion, es_dev in (("dependencies", False), ("devDependencies", True)):
        bloque = carga.get(seccion)
        if not isinstance(bloque, dict):
            continue
        for nombre, especificacion in bloque.items():
            if not isinstance(nombre, str) or not isinstance(especificacion, str):
                # Un valor que no es un texto puede ser un `null` o un objeto: no es una version
                # y no es un nombre, y adivinar seria inventar la dependencia.
                descartados += 1
                continue
            version = _version_de_range_npm(especificacion)
            if not version:
                # `file:../local` o `workspace:*` no tienen version y no son un paquete de un
                # registro. Se cuentan como descartadas para que el panel pueda decir que hubo
                # algo que no se pudo clasificar, en vez de fingir que el manifiesto no lo tenia.
                descartados += 1
                continue
            encontradas[(nombre, version)] = DependenciaParseada(
                name=nombre,
                version=version,
                ecosystem=EcosystemEnum.NPM,
                is_dev=es_dev,
                license=licencia_texto,
            )

    return ResultadoDelParseo(
        dependencies=tuple(encontradas.values()),
        ecosystem=EcosystemEnum.NPM,
        discarded=descartados,
    )


def _texto_licencia(valor: object) -> str | None:
    """La licencia de la raiz de `package.json`, en las dos formas que se ha visto.

    npm acepta tanto un texto —`"MIT"`— como un objeto —`{"type": "MIT"}`— por retrocompatibilidad.
    Con solo la forma texto, la mitad de los manifiestos reales darian `NULL`.
    """

    if isinstance(valor, str):
        return valor[:100] or None
    if isinstance(valor, dict):
        tipo = valor.get("type")
        if isinstance(tipo, str) and tipo:
            return tipo[:100]
    return None


def _version_de_range_npm(especificacion: str) -> str:
    """La parte de versión de un rango de npm, o vacía si no la hay.

    Se devuelve el **rango entero** tal cual —`^4.17.21`, `>=2 <3`— y no solo la version minima.

    Es una decisión que se puede discutir y por eso se documenta: la columna `version` guarda lo
    que el manifiesto **declara**, no una version resuelta. Un rango mas largo significa menos
    informacion de auditoria, y tragarse el rango para dejar `4.17.21` seria mentir: el proyecto
    no usa `4.17.21`, usa "cualquier 4.x desde 4.17.21". El panel lo muestra tal cual para que el
    usuario vea el rango.
    """

    limpia = especificacion.strip()
    if not limpia or limpia.startswith(("file:", "link:", "workspace:", "git", "http")):
        return ""
    # Un rango compuesto se separa con un espacio (`>=2 <3`); se conserva entero.
    return limpia[:100]


# --------------------------------------------------------------------------- #
# requirements.txt
# --------------------------------------------------------------------------- #


def _parsear_requirements(contenido: str) -> ResultadoDelParseo:
    # La clave de deduplicado es `(nombre, ecosistema)` y **no** `(nombre, version)`, porque
    # tiene que coincidir con la restriccion unica de la tabla, que es
    # `uq_supply_chain_repo_name_ecosystem` y no incluye la version.
    #
    # No es un detalle de estilo: con la version en la clave, un `requirements.txt` que declara
    # `requests==2.31.0` y despues `requests` produce **dos** entradas del mismo paquete, la
    # segunda con la version vacia. El panel contaria dos paquetes donde hay uno, y al
    # persistir la segunda fila chocaria con la restriccion de la base. Deduplicar por nombre es
    # lo que hace que lo que el parser dice y lo que la tabla puede guardar sean lo mismo.
    encontradas: dict[tuple[str, str], DependenciaParseada] = {}
    descartados = 0

    for cruda in contenido.splitlines():
        linea = _MARCADOR_ENTORNO.sub("", cruda).strip()
        if not linea or linea.startswith("#") or linea.startswith("-"):
            # Un `-e .` es una dependencia local sin version. Se descarta y se cuenta: no es un
            # paquete del registro, y no se puede afirmar nada de el.
            if _EDITABLE.match(linea):
                descartados += 1
            continue

        emparejada = _PIP.match(linea)
        if emparejada is None:
            descartados += 1
            continue

        # Una linea que es una URL directa **no** es un paquete. Sin esta comprobacion, el
        # patron de nombre se queda con el esquema —`https://user:...` produce un paquete
        # llamado `https`— y el inventario inventa una dependencia que no existe. Ademas, la
        # limpieza de credenciales ya habria sustituido el token por `[REDACTED]`, asi que la
        # fila se guardaria con un nombre de paquete inventado y una URL de version sin
        # sentido: lo peor de las dos cosas.
        if "://" in linea or "@" in emparejada.group("nombre"):
            descartados += 1
            continue

        nombre = emparejada.group("nombre")
        version = (emparejada.group("version") or "").strip()
        if not nombre:
            descartados += 1
            continue
        # Se guarda con el **ecosistema como clave**, igual que la restriccion de la tabla. Se
        # queda con la primera, que es la que el gestor usa al resolver: si el fichero declara
        # `requests==2.31.0` y despues `requests`, la segunda no añade nada y sí haria que el
        # panel contara dos paquetes donde hay uno.
        # Sin operador la version es `*`, que significa "la que haya". Se guarda vacia en lugar
        # de la estrella para que el panel no ensene un `*` como si fuera una version.
        encontradas.setdefault(
            (nombre, EcosystemEnum.PYPI.value),
            DependenciaParseada(
                name=nombre,
                version=version if emparejada.group("operador") else "",
                ecosystem=EcosystemEnum.PYPI,
            ),
        )

    return ResultadoDelParseo(
        dependencies=tuple(encontradas.values()),
        ecosystem=EcosystemEnum.PYPI,
        discarded=descartados,
    )


# --------------------------------------------------------------------------- #
# go.mod
# --------------------------------------------------------------------------- #


def _parsear_go_mod(contenido: str) -> ResultadoDelParseo:
    encontradas: dict[tuple[str, str], DependenciaParseada] = {}
    descartados = 0
    dentro_de_require = False

    for cruda in contenido.splitlines():
        linea = cruda.split("//", 1)[0].strip()
        if not linea:
            continue

        if linea.startswith("require ("):
            dentro_de_require = True
            continue
        if dentro_de_require and linea == ")":
            dentro_de_require = False
            continue

        # Hay dos formas: el bloque multilinea y la linea suelta `require x v1.2.3`.
        if linea.startswith("require "):
            linea = linea[len("require ") :].strip()
        elif not dentro_de_require:
            continue

        # Dentro de un `require (...)` hay lineas de `replace` y `exclude` que no son deps.
        if linea.startswith(("replace", "exclude", "retract")):
            continue

        partes = linea.split()
        if len(partes) < 2:
            descartados += 1
            continue
        emparejada = _GO_REQUIRE.match(linea)
        if emparejada is None:
            descartados += 1
            continue
        module = emparejada.group("nombre")
        version = emparejada.group("version")
        if module == "go" or version == "//" or not version.startswith("v"):
            # `go 1.22` declara la version del lenguaje, no un paquete.
            continue
        encontradas[(module, version)] = DependenciaParseada(
            name=module, version=version, ecosystem=EcosystemEnum.GO
        )

    return ResultadoDelParseo(
        dependencies=tuple(encontradas.values()),
        ecosystem=EcosystemEnum.GO,
        discarded=descartados,
    )


# --------------------------------------------------------------------------- #
# Cargo.toml
# --------------------------------------------------------------------------- #


def _parsear_cargo_toml(contenido: str) -> ResultadoDelParseo:
    encontradas: dict[tuple[str, str], DependenciaParseada] = {}
    descartados = 0
    seccion = ""

    for cruda in contenido.splitlines():
        linea = cruda.split("#", 1)[0].strip()
        if not linea:
            continue

        if linea.startswith("[") and linea.endswith("]"):
            seccion = linea[1:-1].strip()
            continue
        # Solo cuentan las dependencias reales. Una tabla `[package]` o `[features]` no aporta
        # ninguna, y casi todas estas es donde viven las `version` del propio crate.
        if seccion not in {"dependencies", "dev-dependencies", "build-dependencies"}:
            continue

        emparejada = _CARGO.match(linea)
        if emparejada is None:
            # Una dependencia con `path` o `git` es un `path = "..."` en lugar de una version, y
            # no es un paquete de crates.io.
            if "=" in linea:
                descartados += 1
            continue
        nombre = emparejada.group("nombre")
        version = emparejada.group("version")
        # En crates.io los guiones se escriben como guiones bajos en el `Cargo.toml` y el
        # nombre canonico lleva guiones. Sin esta conversion `serde-json` y `serde_json` son dos
        # paquetes distintos en el inventario.
        clave = (nombre, version)
        encontradas.setdefault(
            clave,
            DependenciaParseada(
                name=clave_nombre_crates_io(nombre),
                version=version[:100],
                ecosystem=EcosystemEnum.CARGO,
                is_dev=seccion == "dev-dependencies",
            ),
        )

    return ResultadoDelParseo(
        dependencies=tuple(encontradas.values()),
        ecosystem=EcosystemEnum.CARGO,
        discarded=descartados,
    )


def clave_nombre_crates_io(nombre: str) -> str:
    """El nombre canonico de un crate, con guiones.

    Cargo acepta `serde_json` y `serde-json` para el mismo paquete, y el nombre que aparece en
    un aviso de seguridad es siempre con guiones. Guardar la forma del `Cargo.toml` haria que la
    busqueda por nombre no encontrase el paquete.
    """

    return nombre.replace("_", "-")[:255]


__all__ = [
    "MAX_MANIFEST_CHARS",
    "REDACTADO",
    "DependenciaParseada",
    "ResultadoDelParseo",
    "clave_nombre_crates_io",
    "detectar_ecosistema",
    "limpiar_credenciales",
    "parsear_manifiesto",
]
