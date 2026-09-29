"""Los plurales y los mapas de nombres de la consola tienen la forma que i18next espera.

## Por que este fichero existe

Son tres fallos que ya han pasado, y los tres tienen la misma raiz: **una forma de clave que
dejo de ser valida cuando i18next subio de version y nadie se dio cuenta**, porque el sintoma
no era un error de compilacion sino texto en pantalla.

1. **`tenants.members` era un objeto de plural.** I18next 21 cambio el formato de `one`/`other`
   anidados a sufijos en la clave: `members_one` y `members_other`. Con el formato antiguo, la
   columna de miembros de la tabla de organizaciones pintaba literalmente
   `key 'tenants.members (en)' returned an object instead of string.` Es el mismo modo de fallo
   que el `hint_key` que llevaba el namespace dentro, y por el mismo motivo: el contrato estaba
   en un comentario y en un JSON, y ninguno de los dos lo comprueba.

   Solo se corrigio la clave que habiasaltado a la vista. Los otros 34 plurales del proyecto
   ya usaban el formato bueno, lo que hace que el error sea especialmente traicionero: el
   proyecto esta casi entero bien y el fallo esta en la excepcion.

2. **`audit.actions` no existia.** La columna de accion del rastro global pintaba el valor
   crudo del enum, `UNSUPPORTED_USAGE_UNBILLED`, dentro de una insignia verde. La accion es lo
   unico que dice que paso, y hay diez acciones en el enum con nombre en castellano y en
   ingles que no se estaban usando.

3. **`sales.types` no existia.** La columna de tipo de las ventas pintaba
   `checkout.session.completed`, que es el nombre del evento en el proveedor de cobro. Al lado
   de "Importe" y "Creditos" no dice nada; decia que el importe habia acreditado 300 creditos.

Ademas se comprueba que `AuditActionEnum` y su mapa de traduccion no se desincronicen: anadir una
accion al enum sin traducirla es un fallo, no una cadena en crudo en pantalla. Esa es la
comprobacion que convierte "traducido" en una invariante en vez de en un dato de hoy.

## Por que lee los ficheros en vez de usar i18next

Porque lo que se previene es un fallo de **forma** del JSON de traduccion, y probarlo con la
misma libreria que resuelve las claves seria probar que la libreria funciona. Leyendo el JSON se
comprueba el contrato en el punto donde se rompio, que es el fichero.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

LOCALES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"
MODELOS_AUDIT = Path(__file__).resolve().parents[1] / "apps" / "audit" / "models.py"
FUENTES_CONSOLE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "features" / "admin"

IDIOMAS = ("es", "en")

# Las categorias de plural de CLDR. Un objeto cuyas claves sean **solo** de este conjunto es un
# plural con el formato que i18next 21 dejo de reconocer.
CATEGORIAS_PLURAL = {"zero", "one", "two", "few", "many", "other"}


def cargar(idioma: str, namespace: str) -> Any:
    with (LOCALES / idioma / f"{namespace}.json").open(encoding="utf-8") as f:
        return json.load(f)


def objetos_de_plural(nodo: Any, prefijo: str = "") -> list[str]:
    """Rutas de las claves cuyo valor es un objeto de plural con el formato antiguo."""

    encontradas: list[str] = []
    if not isinstance(nodo, dict):
        return encontradas
    if nodo and set(nodo.keys()) <= CATEGORIAS_PLURAL:
        return [prefijo.rstrip(".")]
    for clave, valor in nodo.items():
        encontradas.extend(objetos_de_plural(valor, f"{prefijo}{clave}."))
    return encontradas


def rutas_folhas(nodo: Any, prefijo: str = "") -> dict[str, Any]:
    if not isinstance(nodo, dict):
        return {prefijo.rstrip("."): nodo}
    hojas: dict[str, Any] = {}
    for clave, valor in nodo.items():
        hojas.update(rutas_folhas(valor, f"{prefijo}{clave}."))
    return hojas


@pytest.mark.parametrize("idioma", IDIOMAS)
def test_ningun_idioma_usa_el_formato_antiguo_de_plural(idioma: str) -> None:
    """Un objeto `one`/`other` devuelve un objeto, no una cadena, desde i18next 21."""

    offenders: list[str] = []
    for fichero in sorted((LOCALES / idioma).glob("*.json")):
        with fichero.open(encoding="utf-8") as f:
            arbol = json.load(f)
        for ruta in objetos_de_plural(arbol):
            offenders.append(f"{fichero.name}: {ruta}")

    assert not offenders, (
        "estas claves usan el formato de plural de i18next 20, que la version 26 no resuelve: "
        "devuelven un objeto y en pantalla sale 'key ... returned an object instead of string'. "
        f"El arreglo es renombrarlas a <clave>_one y <clave>_other. offending: {offenders}"
    )


@pytest.mark.parametrize("idioma", IDIOMAS)
def test_tenants_members_usa_sufijos(idioma: str) -> None:
    """La columna de miembros de organizaciones: la clave y las dos formas de plural."""

    ten = cargar(idioma, "admin")["tenants"]

    assert "members" not in ten, (
        "tenants.members sigue siendo un objeto de plural. Con i18next 26 eso hace que la "
        "columna de miembros pinte un error de i18next en vez de un numero."
    )
    for sufijo in ("_one", "_other"):
        clave = f"members{sufijo}"
        assert clave in ten, f"falta tenants.{clave} en {idioma}"
        assert isinstance(ten[clave], str), f"tenants.{clave} no es una cadena en {idioma}"
        assert "{{count}}" in ten[clave], (
            f"tenants.{clave} no lleva {{{{count}}}}: es un plural, y sin la variable no "
            "concuerda con la cantidad."
        )


def test_las_dos_formas_de_plural_dicen_cosas_distintas() -> None:
    """Si `_one` y `_other` son iguales, la prueba de que el plural funciona no vale nada."""

    for idioma in IDIOMAS:
        ten = cargar(idioma, "admin")["tenants"]
        assert ten["members_one"] != ten["members_other"], (
            f"en {idioma}, members_one y members_other son iguales"
        )


def test_ningun_llamado_de_plural_apunta_a_una_clave_sin_forma_de_plural() -> None:
    """Que cada `t('x', { count })` del frontend resuelva a **algo**, en los dos idiomas.

    Es la comprobacion que convierte "traducido" en una invariante. Sin ella, un `count` que se
    anade a una clave que no es plural devuelve la cadena con el `{{count}}` literal puesto, y
    eso no se ve en un test de paridad porque la clave existe.

    Y aqui esta el matiz que hace que la primera version de esta prueba fallara contra el codigo
    correcto: con i18next 21, `t('tenants.members', { count })` es la llamada **correcta**, y a
    pesar de eso `tenants.members` **no debe existir** como clave. I18next aniade el sufijo de la
    categoria de plural —`tenants.members_one`, `tenants.members_other`— y las busca por debajo de
    la que se le paso. Asi que la comprobacion correcta no es "existe la clave", sino "existe la
    clave o existe alguna de sus formas de plural".

    Sin ese matiz, la prueba que deveria proteger el plural acababa exigiendo justo el formato
    antiguo que provoco el fallo, que es la forma en que una prueba puede ser peor que ninguna.
    """

    fallidos: list[str] = []
    patron = re.compile(r"t\(\s*'([a-zA-Z0-9_.]+)'\s*,\s*\{[^}]*\bcount\b")

    for fichero in sorted(FUENTES_CONSOLE.rglob("*.tsx")):
        texto = fichero.read_text(encoding="utf-8")
        for coincidencia in patron.finditer(texto):
            clave = coincidencia.group(1)
            # La clave relativa se resuelve contra el namespace de la pagina, y hay paginas
            # con varios namespaces. Se busca en todos: si la clave o alguna de sus formas
            # existe en alguno, la llamada es valida.
            namespace = fichero.stem.replace("Page", "").replace("View", "").lower()
            candidatas = {namespace, "common", "admin"}
            if resuelve_en_algun_idioma(clave, candidatas):
                continue
            fallidos.append(f"{fichero.name}: t('{clave}', {{ count }})")

    assert not fallidos, (
        "estos llamados pasan `count` a una clave que no tiene forma de plural en ningun "
        f"idioma. Sin ella i18next devuelve la cadena con el {{{{count}}}} literal puesto. "
        f"offending: {fallidos}"
    )


def resuelve_en_algun_idioma(clave: str, namespaces: set[str]) -> bool:
    """¿Existe `clave`, o alguna de sus formas de plural, en **los dos** idiomas?

    Se exige en los dos y no en uno porque una clave traducida solo en español es un fallo de
    paridad que ya cubre otro gate, y aqui lo que se busca es que la forma exista, no que este
    completa.

    Y dentro de cada idioma se recorre **todos** los namespaces candidatos con un `any`, no con
    un `return False` en el primero que no la tiene. La version con `return False` fallaba
    contra codigo correcto: `admin.json` tiene `tenants.members_one`, se comprobaba ese, y
    despues `common.json`, que no la tiene, devolvia "no existe" y tumbaba la prueba entera.
    Una comprobacion que falla porque el segundo candidato no tiene la clave no comprueba lo
    que dice comprobar.
    """

    for idioma in IDIOMAS:
        if not any(
            resuelve_en_un_namespace(clave, LOCALES / idioma / f"{ns}.json")
            for ns in sorted(namespaces)
            if (LOCALES / idioma / f"{ns}.json").is_file()
        ):
            return False
    return True


def resuelve_en_un_namespace(clave: str, fichero_ns: Path) -> bool:
    with fichero_ns.open(encoding="utf-8") as f:
        arbol = json.load(f)
    hojas = rutas_folhas(arbol)
    if clave in hojas:
        return True
    # Una forma de plural se reconoce por el sufijo de categoria sobre la propia clave. Se
    # comparan los prefijos largos para corto: `tenants.members` es el prefijo de
    # `tenants.members_one`, y no al reves, que es como i18next los busca.
    return any(
        hoja.startswith(f"{clave}_") and hoja.rsplit("_", 1)[-1] in CATEGORIAS_PLURAL
        for hoja in hojas
    )


def test_acciones_de_auditoria_todas_tienen_nombre_en_los_dos_idiomas() -> None:
    """Cada valor de `AuditActionEnum` tiene su etiqueta, en español y en inglés."""

    valores = valores_del_enum_auditoria()
    assert valores, "no se ha leido ningun valor de AuditActionEnum: el patron del enum ha cambiado"

    for idioma in IDIOMAS:
        mapa = cargar(idioma, "admin")["audit"]["actions"]
        faltan = [v for v in valores if v not in mapa]
        sobran = [v for v in mapa if v not in valores]
        assert not faltan, (
            f"en {idioma} no hay etiqueta para las acciones {faltan}. Anadirlas a "
            "frontend/src/locales/"
            f"{idioma}/admin.json bajo audit.actions: en la consola se veria el identificador "
            "del enum en crudo."
        )
        assert not sobran, (
            f"en {idioma} hay etiquetas de acciones que no existen en AuditActionEnum: {sobran}. "
            "O el enum cambio y faltan las traducciones, o sobran traducciones."
        )
        for valor, etiqueta in mapa.items():
            assert isinstance(etiqueta, str) and etiqueta.strip(), (
                f"audit.actions.{valor} esta vacia en {idioma}"
            )
            # Una etiqueta que sea el propio identificador no es una etiqueta.
            assert etiqueta != valor, (
                f"audit.actions.{valor} vale lo mismo que el identificador en {idioma}"
            )


def test_tipos_de_pago_cubren_los_eventos_que_el_backend_registra() -> None:
    """El tipo de las ventas se traduce, y el traducir cae al identificador si no se sabe."""

    mapa_es = cargar("es", "admin")["sales"]["types"]
    mapa_en = cargar("en", "admin")["sales"]["types"]

    assert set(mapa_es) == set(mapa_en), (
        "los tipos de pago no tienen la misma paridad de claves: "
        f"solo en es: {sorted(set(mapa_es) - set(mapa_en))}, "
        f"solo en en: {sorted(set(mapa_en) - set(mapa_es))}"
    )

    # El evento que el backend registra de verdad tiene que estar, y no solo el que happened en
    # la maqueta. Sin esto el mapa puede estar lleno de tipos que ya no emite nadie.
    registrados = eventos_de_pago_registrados()
    assert registrados, "no se ha leido ningun evento de pago del backend"
    faltan = [e for e in registrados if e not in mapa_es]
    assert not faltan, (
        f"el backend registra estos eventos de pago y no tienen etiqueta: {faltan}. En la "
        "consola de ventas se veria el nombre crudo del proveedor."
    )

    for clave, etiqueta in mapa_es.items():
        assert etiqueta != clave, f"sales.types.{clave} vale lo mismo que el identificador"
        assert clave != mapa_en[clave], (
            f"sales.types.{clave} tiene la misma traduccion en los dos idiomas"
        )


def valores_del_enum_auditoria() -> list[str]:
    """Los valores de `AuditActionEnum`, leidos del fuente.

    Se leen del fichero y no se importan porque el enum no cambia con la migracion: la pregunta
    que hace esta prueba es precisamente si el JSON esta al dia **respecto** al enum, y
    importarlo solo comprobaria que el Python carga.
    """

    texto = MODELOS_AUDIT.read_text(encoding="utf-8")
    inicio = texto.find("class AuditActionEnum")
    assert inicio != -1, "no se encuentra AuditActionEnum en apps/audit/models.py"
    siguiente = texto.find("\nclass ", inicio + 10)
    cuerpo = texto[inicio : siguiente if siguiente != -1 else len(texto)]
    lineas = [
        linea
        for linea in cuerpo.split("\n")
        if linea.strip() and not linea.strip().startswith("#")
    ]
    return sorted(set(re.findall(r"([A-Z][A-Z0-9_]*)\s*=\s*\"", "\n".join(lineas))))


def eventos_de_pago_registrados() -> set[str]:
    """Los `event_type` que el backend escribe, leidos del fuente de cobro y del seeder."""

    encontrados: set[str] = set()
    raiz = Path(__file__).resolve().parents[1]
    for fichero in list(raiz.glob("apps/billing/*.py")) + list(raiz.glob("scripts/*.py")):
        texto = fichero.read_text(encoding="utf-8")
        encontrados.update(re.findall(r'"(checkout\.[a-z_.]+)"', texto))
    return encontrados
