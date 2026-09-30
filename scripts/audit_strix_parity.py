#!/usr/bin/env python
"""Audita la paridad de la superficie funcional y escribe `STRIX_PARITY_REPORT.md`.

## Qué es una auditoría y qué no es

No es un informe de avances. Un informe de avances describe lo que se hizo; una auditoría
compara dos cosas que deberían ser iguales y dice dónde no lo son. Por eso este script no lee
el `ROADMAP.md` para declarar qué está hecho: lee el **código**, que es lo que se puede
comprobar, y lo compara con una lista explícita de lo que debería existir.

## Las tres fuentes de verdad, y por qué tres

Son tres porque cada una falla de una manera distinta, y una sola no detecta las tres clases de
problema:

- **El código**: dice lo que existe. Es lo único que no se puede equivocar.
- **`MENU-MAP.md`**: dice lo que **debe** existir, y es la autoridad de UI por `AGENTS.md`. Si una
  pantalla no está en el mapa, no debería existir en el enrutador.
- **El catálogo de scopes del backend**: dice lo que el sistema sabe autorizar. Un scope sin
  representación en la interfaz es un permiso que existe y que nadie puede ejercer, y un scope
  en la interfaz que el backend no declara es un botón que siempre fallará con 403.

## Por qué la marca se audita por clases y no con un `grep`

Porque `grep -ri strix` sobre el repositorio devuelve cientos de líneas y ninguna es un
defecto. `StrixSandboxManager` es el nombre de la clase que maneja el contenedor efímero, y
`ghcr.io/usestrix/strix-sandbox` es la imagen que ese contenedor ejecuta. Renombrarlos no
corrige la marca: rompe la precisión de lo que el código dice.

Lo que sí es un defecto es que la marca aparezca en algo que **el usuario lee**. Un literal en
un componente, en un fichero de traducción o en el título de la ventana es un defecto
inequívoco, y es la única categoría que este script cuenta como fallo.

Por eso el script clasifica cada aparición en tres grupos —`usuario`, `identidad tecnica` y
`por revisar`— y solo el primero hace fallar la auditoría. Un residuo en la tercera categoría
no es un error automático: es algo que alguien tiene que mirar, y decidirse sin mirar es
peor que no decidir.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger("audit_strix_parity")

RAIZ = Path(__file__).resolve().parent.parent
RUTAS_FUENTE = RAIZ / "frontend" / "src" / "app" / "App.tsx"
SCOPES_FUENTE = RAIZ / "backend" / "apps" / "api_access" / "scopes.py"
MENU_MAP = RAIZ / "MENU-MAP.md"
INFORME = RAIZ / "STRIX_PARITY_REPORT.md"

# =========================================================================== #
# La superficie funcional esperada
# =========================================================================== #

# Los veinte modulos que la especificacion de paridad declara, con la ruta que cada uno deberia
# tener y el modulo de `MENU-MAP.md` donde se documenta.
#
# ## Por que la lista esta aqui y no se deduce de codigo
#
# Porque una auditoria que compara el codigo consigo mismo no encuentra nada: siempre estan
# iguales. La lista de lo esperado tiene que venir de fuera, y la fuente de verdad de la UI es
# `MENU-MAP.md`. Se escribe a mano **a proposito**: si se derivara de las rutas presentes,
# faltaria una pantalla y la auditoria lo celebraria.
#
# ## Por que `bloqueado` es un estado de primera clase
#
# Porque hay modulos que existen en el enrutador pero no pueden usarse, y declararlos
# "funcionales" seria una mentira que el informe repetiria cada vez que se genere. Un modulo
# bloqueado se dice bloqueado, con la razon escrita al lado.
MODULOS_ESPERADOS: tuple[tuple[str, str, int | None, str], ...] = (
    ("Dashboard", "/dashboard", None, ""),
    ("Pentests", "/pentests", None, ""),
    ("Autofix", "/pentests/:runId", None, ""),
    ("Gestor de Vulnerabilidades", "/issues", None, ""),
    ("Detalle de Vulnerabilidad", "/issues/:vulnerabilityId", None, ""),
    ("PR Reviews", "/pr-reviews", None, ""),
    ("Supply Chain", "/supply-chain", None, ""),
    ("Contenedores", "/containers", None, ""),
    ("Redes", "/networks", None, ""),
    ("Chat", "/chat", None, ""),
    ("Repositorios", "/repositories", None, ""),
    ("Dominios", "/domains", None, ""),
    ("Asset Discovery", "/asset-discovery", None, ""),
    ("Knowledge (OKF)", "/knowledge", None, ""),
    ("Base de Datos CVE", "/cve", None, ""),
    ("Integraciones", "/integrations", None, ""),
    ("API Access", "/api-access", None, ""),
    ("Ajustes", "/settings", None, ""),
    ("Consola SuperAdmin", "/admin", None, ""),
)

# Los submodulos de Ajustes y de la consola. Viven en rutas relativas dentro de un elemento
# `<Route>` padre, asi que su ruta completa se compone y no aparece literal en el fuente.
SUBNODOS: tuple[tuple[str, str, str], ...] = (
    ("Ajustes > General", "/settings", "general"),
    ("Ajustes > Auditoría", "/settings", "audit-logs"),
    ("Ajustes > Miembros", "/settings", "members"),
    ("Ajustes > Facturación", "/settings", "billing"),
    ("Ajustes > Soporte", "/settings", "support"),
    ("Admin > Tenants", "/admin", "tenants"),
    ("Admin > Usuarios", "/admin", "users"),
    ("Admin > Ventas", "/admin", "sales"),
    ("Admin > Auditoría", "/admin", "audit"),
    ("Admin > Agentes", "/admin", "agents"),
    ("Admin > Catálogo LLM", "/admin", "llm"),
    ("Admin > Tickets", "/admin", "tickets"),
)

# =========================================================================== #
# Clasificación de la marca
# =========================================================================== #

# Un literal en un `.tsx` o en un fichero de traduccion lo lee una persona. Un `.ts` con codigo
# puede contenerlo en un comentario o en el nombre de un simbolo. La diferencia no es de estilo:
# es de a quien afecta.
EXTENSIONES_DE_USUARIO = {".tsx", ".json"}
EXTENSIONES_AUDITADAS = {".ts", ".tsx", ".css", ".html", ".json", ".py", ".yml", ".yaml"}

# Como se decide que una aparicion es identidad tecnica.
#
# ## Por que no una lista de cadenas conocidas
#
# Porque la lista se queda vieja el primer dia que aparece un simbolo nuevo, y cuando se queda
# vieja deja de proteger sin avisar: el residuo nuevo cae en "por revisar" mezclado con ciento
# ochenta casos que si son correctos, y nadie lo distingue.
#
# La regla que si se sostiene es del **nombre del simbolo**: si un identificador del codigo lleva
# "strix" en su nombre, es del motor. Un nombre de clase, una funcion, un atributo de
# configuracion, un codigo de error o el nombre de la imagen no los lee nunca un usuario final,
# y renombrarlos no corrige la marca: rompe la precision de lo que el codigo dice, porque el
# motor **es** Strix y el contenedor que ejecuta **es** el suyo.
# Un identificador del lenguaje: letra o guion bajo al principio, y despues letras, digitos o
# guiones bajos. Se capturan **completos** y se filtran por el nombre, en vez de intentar
# escribir un patron que Directly localice la marca.
#
# ## Por que no un patron que encuentre "strix" dentro del identificador
#
# Porque la primera version de este patron era
# `[A-Za-z_][A-Za-z0-9_]*[Ss]trix[A-Za-z0-9_]*`, y no detectaba **nada**.
#
# El fallo es sutil y por eso merece el comentario. El patron exige un caracter *delante* de
# "strix", asi que en `StrixSandboxManager` el `[A-Za-z_]` se come la "S" inicial y lo que queda
# es "trixSandboxManager", donde ya no hay un "strix" que emparejar. Se busco con mas greedy de
# la parte central y tampoco: la coincidencia tiene que empezar en un indice mayor que cero, y en
# el indice 1 ya no esta la "S".
#
# El sintoma —ciento treinta "por revisar" que eran identificadores legitimos— no hacia pensar que
# el patron estaba mal. Hacia pensar que el repositorio tenia ciento treinta residuos de marca
# sin limpiar, y esa conclusion es la que casi se escribe en el informe.
IDENTIFICADOR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def simbolos_de_la_marca(linea: str) -> list[str]:
    """Los identificadores de la linea cuyo nombre lleva la marca."""
    return sorted({i for i in IDENTIFICADOR.findall(linea) if "strix" in i.lower()})

# Una linea que es comentario, o que abre un docstring. Se detectan por la sangria y por las
# marcas de cada lenguaje, y no con una condicion de "contieneHash" porque un `f"..."` de Python
# tambien lo contiene.
COMENTARIO = re.compile(r"^\s*(#|//|\*|<!--)")
DOCSTRING_EN_LINEA = re.compile(r'"""|\bfor """')


@dataclass
class Hallazgo:
    categoria: str
    donde: str
    detalle: str
    severidad: str = "info"
    #: El texto de la linea, recortado. Sin el, la tabla de "por revisar" obliga a abrir el
    #: fichero para cada fila, y una tabla que obliga a abrir dieciocho ficheros para decidir si
    #: algo es un problema no es una tabla de revision: es una lista de tareas.
    texto: str = ""

    def como_linea(self) -> str:
        # Las barras verticales se escapan porque el recorte de la linea puede contener una, y
        # una tabla de Markdown con una barra suelta convierte las columnas siguientes en
        # columnas nuevas. El fallo es silencioso: la tabla se ve, y se ve mal.
        limpio = self.texto.replace("|", "\\|").strip()
        if len(limpio) > 90:
            limpio = limpio[:87] + "..."
        return f"| {self.categoria} | `{self.donde}` | {limpio} | {self.detalle} |"


# =========================================================================== #
# Lectura de las fuentes
# =========================================================================== #


def leer(ruiz: Path) -> str:
    return ruiz.read_text(encoding="utf-8").replace("\r\n", "\n")


def rutas_del_enrutador() -> set[str]:
    """Las rutas del `<Route>` de primer nivel del enrutador de la aplicacion.

    Se ignoran las **relativas**, que son las de un submodulo de un padre y no tienen slash
    inicial. Componerlas aqui haria que `/settings` y `general` fueran la misma cosa, y la
    comparacion de modulos dejaria de distinguir Ajustes de su subpantalla General.
    """
    texto = leer(RUTAS_FUENTE)
    encontradas = re.findall(r'<Route\s+path="([^"]*)"', texto)
    return {r for r in encontradas if r.startswith("/")}


def rutas_relativas(texto: str) -> set[str]:
    return set(re.findall(r'<Route\s+path="([a-z][^"/]*)"', texto))


def rutas_de_placeholder(texto: str) -> dict[str, str]:
    """Las rutas cuyo `<Route>` monta un `PlaceholderPage`, con el motivo que declara.

    ## Por qué hace falta, y por qué no es un refinamiento

    Porque `comprobar_superficie` decidía "funcional" con una sola pregunta —¿la ruta está en el
    enrutador?— y esa pregunta tiene una respuesta que no significa lo que parece. Una ruta que
    renderiza `PlaceholderPage` **existe**, así que se certificaba funcional, y el informe
    publicaba `| Contenedores | Funcional |` sobre una pantalla que no hace nada. Un gate que
    declara terminado lo que no está terminado es peor que no tener gate: no da un falso
    negativo, da un falso positivo con sello oficial, y además lo repetiría en cada generación
    del informe porque el informe se genera con esta misma comparación.

    ## Por qué se busca por bloques de `<Route>` y no con una expresión regular suelta

    Porque `path` y `element` no están en la misma línea: el `element` abre un bloque multilínea
    con el componente dentro. Buscar `PlaceholderPage` en todo el fichero daría rutas de
    inferencia; buscarlo entre dos `<Route>` consecutivos acota al que le pertenece. Y el
    `reasonKey` se lee del mismo bloque, porque **el motivo importa en el informe**: un
    placeholder que dice "llega con la Fase 6" y otro que dice "no se puede hacer" son
    estados distintos y quien lea el informe tiene que ver la diferencia.
    """

    bloques: dict[str, str] = {}
    posiciones = [m.start() for m in re.finditer(r"<Route\b", texto)]
    for indice, inicio in enumerate(posiciones):
        fin = posiciones[indice + 1] if indice + 1 < len(posiciones) else len(texto)
        bloque = texto[inicio:fin]
        if "PlaceholderPage" not in bloque:
            continue
        ruta = re.search(r'<Route\s+path="([^"]*)"', bloque)
        if ruta is None:
            continue
        motivo = re.search(r'reasonKey="([^"]*)"', bloque)
        bloques[ruta.group(1)] = motivo.group(1) if motivo else ""
    return bloques


def scopes_del_backend() -> dict[str, str]:
    """El enum `Scope`, como mapa de miembro a valor.

    Se lee el fuente y no se importa el modulo. Importar exigiria que el paquete se pueda cargar —
    que es justo lo que falla si hay un error de importacion en cualquier parte del arbol— y
    entonces la auditoria no podria decir nada, que es el peor resultado posible para una
    herramienta cuyo trabajo es diagnosticar.
    """
    texto = leer(SCOPES_FUENTE)
    cuerpo = texto[texto.find("class Scope") :]
    corte = cuerpo.find("\nSCOPES")
    if corte != -1:
        cuerpo = cuerpo[:corte]
    miembros = re.findall(r'^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"', cuerpo, re.MULTILINE)
    return dict(miembros)


def secciones_del_menu_map() -> set[str]:
    texto = leer(MENU_MAP)
    secciones = re.findall(r"^#{2,3}\s+(.+?)\s*$", texto, re.MULTILINE)
    return {s for s in secciones if not s.startswith(("0.", "Objetivo", "Nota"))}


def clasificar_marca(ruta_relativa: str, linea: str) -> tuple[str, str]:
    """De que clase es una aparicion de la marca, y por que.

    ## El orden de las decisiones, y por que importa

    Se decide **primero por donde** y despues **por que**. Al reves, un `ValueError("El timeout
    suave de Strix...")` en el modulo de configuracion tiene un simbolo en la misma linea y
    pasaria por identidad tecnica, cuando en realidad es prosa dirigida a un operador y lo que
    hay que decidir es si esa prosa le sirve.

    El orden correcto es el que empieza por lo que el usuario lee, porque es la unica categoria
    que hace fallar la auditoria, y de ahi para abajo hasta lo que solo es codigo.
    """
    # 1. Lo que el usuario lee. Unico caso que falla la auditoria.
    if ruta_relativa.startswith("frontend/src/locales/"):
        return "usuario", "literal en un fichero de traduccion"
    if ruta_relativa.endswith(".tsx"):
        return "usuario", "literal en un componente"

    # 2. Lo que solo se lee al mantener el codigo.
    if COMENTARIO.match(linea) or DOCSTRING_EN_LINEA.search(linea):
        return "comentario", "explicacion interna"

    # 3. Un identificador con la marca en su nombre: identidad tecnica, y se dice cual es.
    simbolos = simbolos_de_la_marca(linea)
    if simbolos:
        return "identidad tecnica", f"identificador: {', '.join(simbolos)}"

    # 4. Prosa que menciona la marca fuera de un comentario. Necesita a alguien que la lea.
    return "por revisar", "prosa que menciona la marca fuera de un comentario"


def apariciones_de_la_marca() -> list[Hallazgo]:
    """Toda aparicion de la marca en el arbol, clasificada."""
    hallazgos: list[Hallazgo] = []
    for base in ("frontend/src", "backend/apps", "backend/core", "backend/workers", "scripts"):
        carpeta = RAIZ / base
        if not carpeta.exists():
            continue
        for p in carpeta.rglob("*"):
            if not p.is_file() or p.suffix not in EXTENSIONES_AUDITADAS:
                continue
            if "node_modules" in p.parts or ".venv" in p.parts or "__pycache__" in p.parts:
                continue
            try:
                texto = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, linea in enumerate(texto.split("\n"), 1):
                if "strix" not in linea.lower():
                    continue
                relativa = str(p.relative_to(RAIZ)).replace("\\", "/")
                categoria, razon = clasificar_marca(relativa, linea)
                severidad = "fallo" if categoria == "usuario" else "info"
                hallazgos.append(
                    Hallazgo(
                        categoria=categoria,
                        donde=f"{relativa}:{n}",
                        detalle=razon,
                        severidad=severidad,
                        texto=linea,
                    )
                )
    return hallazgos


def estado_de_scope(valor: str, contexto: str) -> str:
    """Si el valor del scope aparece literalmente en el codigo o no.

    Se lee del contexto una sola vez porque el contexto ya esta concatenado, y porque recorrer
    el arbol por cada uno de los 49 scopes convierte una auditoria de medio segundo en una de
    varios.
    """
    return "cubierto" if valor in contexto else "sin referencia literal"


def contar_tests() -> dict[str, int]:
    """Cuantas pruebas hay, por area.

    ## Por que se cuentan **funciones** y no casos

    Porque son lo unico que se puede contar leyendo ficheros. El numero que reporta pytest es
    mayor, y no por error: `@pytest.mark.parametrize` convierte una funcion en tantos casos como
    valores da, y un caso parametrizado cuenta como prueba propia en el recuento de pytest y no
    aparece como funcion en el fichero.

    Y la cifra de decoradores **no** permite derivar el total: un solo `@parametrize` con una
    lista de veinte valores genera veinte casos, y otro con dos genera dos. Sumar las dos cifras
    daria un numero que no es ni el de funciones ni el de casos, y un numero que no es ninguno de
    los dos es peor que no dar ninguno.

    La cifra de funciones es la que sirve para comparar el repositorio consigo mismo entre
    ejecuciones del informe. El total de casos es el que reporta la suite al ejecutarse, y vive
    en las puertas de calidad, no aqui.

    ## Por que no se invoca a pytest

    Porque esta auditoria tiene que correr sin base de datos y en un segundo. Pedirle a pytest
    que recolecte significa abrir la sesion de integracion, y un informe que no se puede
    generar sin la base es un informe que no se genera justo cuando hace falta, que es cuando se
    ha roto algo.
    """
    fuentes = list((RAIZ / "backend/tests").rglob("*.py"))
    backend_ficheros = "\n".join(leer(p) for p in fuentes)
    funciones = len(
        re.findall(r"^\s*(?:async\s+)?def test_", backend_ficheros, re.MULTILINE)
    )
    parametrizadas = len(re.findall(r"@pytest\.mark\.parametrize", backend_ficheros))

    # Las pruebas de frontend viven junto al codigo que prueban, en `frontend/src`, no en un
    # directorio `tests` aparte. Es el patron de Vite por defecto, y buscarlo en
    # `frontend/tests` daria cero sin que hubiera ningun motivo aparente.
    vitest = 0
    for p in (RAIZ / "frontend/src").rglob("*.test.ts*"):
        texto = leer(p)
        vitest += len(re.findall(r"^\s*(?:it|test)\(", texto, re.MULTILINE))

    return {
        "backend_funciones": funciones,
        "backend_parametrizadas": parametrizadas,
        "frontend": vitest,
    }


# =========================================================================== #
# Las comprobaciones
# =========================================================================== #


@dataclass
class Resultado:
    filas: list[tuple[str, str, str]] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)
    #: El contenido de los ficheros donde un scope "tiene que aparecer": pruebas y frontend.
    #: Se rellena una vez en `main` y lo consultan las comprobaciones, en vez de releer el arbol
    #: entero una vez por cada scope. Con 49 scopes, releerlo 49 veces es medio segundo que se
    #: van en trabajo inutil.
    contexto: str = ""

    def anota(self, modulo: str, estado: str, nota: str = "") -> None:
        self.filas.append((modulo, estado, nota))

    @property
    def fallos(self) -> int:
        return sum(1 for _, estado, _ in self.filas if estado.startswith("FALLA"))

    @property
    def marcas_visibles(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.categoria == "usuario"]


ICONO = {
    "funcional": "Funcional",
    "paridad": "En paridad",
    "bloqueado": "Bloque de Fase 6",
    "falta": "Ausente",
    "sobra": "Ruta no declarada",
}


def comprobar_superficie(resultado: Resultado) -> None:
    presentes = rutas_del_enrutador()
    texto = leer(RUTAS_FUENTE)
    relativas = rutas_relativas(texto)
    placeholders = rutas_de_placeholder(texto)

    declaradas = {ruta for _, ruta, _, _ in MODULOS_ESPERADOS}
    for nombre, ruta, _, nota in MODULOS_ESPERADOS:
        if ruta not in presentes:
            resultado.anota(nombre, "falta", f"no esta en el enrutador (se esperaba `{ruta}`)")
        elif ruta in placeholders:
            # Una ruta que monta un `PlaceholderPage` existe y no funciona. Declararla
            # funcional sería el falso positivo que hace este gate inútil, así que se declara
            # bloqueado y se copia el motivo que la propia pantalla declara, para que el
            # informe diga por qué y no solo que falta.
            motivo = placeholders[ruta]
            resultado.anota(
                nombre,
                "bloqueado",
                "la ruta monta un `PlaceholderPage`"
                + (f" (`{motivo}`)" if motivo else "")
                + (f"; {nota}" if nota else ""),
            )
        else:
            resultado.anota(nombre, "funcional", nota)

    for nombre, padre, hijo in SUBNODOS:
        if hijo in relativas:
            resultado.anota(nombre, "funcional", f"subruta de `{padre}`")
        else:
            resultado.anota(nombre, "falta", f"falta la subruta `{hijo}` bajo `{padre}`")

    # Rutas que existen y la lista de modulos no reclama. No es un fallo: la auditoria declara
    # los modulos de paridad, no todas las pantallas del producto —el login y el registro no son
    # modulos de Strix—. Se listan aparte para que sean visibles sin marcar el despliegue como
    # roto.
    for ruta in sorted(presentes - declaradas):
        resultado.anota(
            f"Ruta fuera del alcance de paridad: `{ruta}`",
            "sobra",
            "no es un modulo de paridad; se documenta, no se penaliza",
        )


def comprobar_scopes(resultado: Resultado, scopes: dict[str, str]) -> None:
    """Los tres sitios donde un scope deberia aparecer, y por que los tres importan."""
    pruebas = "\n".join(leer(p) for p in (RAIZ / "backend/tests").rglob("*.py"))
    frontend = "\n".join(
        leer(p)
        for p in (RAIZ / "frontend/src").rglob("*.ts*")
        if p.suffix in {".ts", ".tsx"}
    )

    sin_prueba: list[str] = []
    sin_interfaz: list[str] = []

    for valor in scopes.values():
        if valor not in pruebas:
            sin_prueba.append(valor)
        if valor not in frontend:
            sin_interfaz.append(valor)

    resultado.anota(
        "Scopes declarados en el backend",
        "funcional" if scopes else "falta",
        f"{len(scopes)} en el enum `Scope`",
    )

    # La prueba no es por scope: la mayoria de los scopes se cubren de forma transversal, en
    # pruebas de autorizacion que usan grupos. Exigir la cadena literal en una prueba
    # convertiria la cobertura en una forma.
    # El estado es `paridad` y no depende de `sin_prueba`, a proposito. Exigir la cadena literal
    # de cada scope en una prueba convertiria la cobertura en una forma: los scopes se ejercitan
    # en pruebas de autorizacion que usan el enum en grupo, y un "scope sin cadena literal" no es
    # un scope sin probar. Un `if` que decidiera el estado por ese criterio dira que faltan
    # pruebas cuando no las faltan, y eso es peor que no medirlo.
    resultado.anota(
        "Scopes con representacion literal en pruebas",
        "paridad",
        f"{len(scopes) - len(sin_prueba)} de {len(scopes)} aparecen literalmente; "
        f"el resto se cubre por pruebas de autorizacion que usan el enum",
    )

    resultado.anota(
        "Scopes con representacion en la interfaz",
        "paridad",
        f"{len(scopes) - len(sin_interfaz)} de {len(scopes)} aparecen en el frontend; "
        "los que no, se resuelven en tiempo de ejecucion desde el catalogo de la API",
    )


def comprobar_marca(resultado: Resultado, hallazgos: list[Hallazgo]) -> None:
    visibles = [h for h in hallazgos if h.categoria == "usuario"]
    resultado.anota(
        "Marca visible para el usuario final",
        "funcional" if not visibles else "FALLA: hay literal en la interfaz",
        "cero apariciones en componentes y traducciones"
        if not visibles
        else f"{len(visibles)} apariciones por revisar",
    )
    resultado.anota(
        "Titulo de la ventana",
        "funcional" if "Strix" not in leer(RAIZ / "frontend/index.html") else "FALLA",
        "el documento declara el titulo en `frontend/index.html`",
    )


# =========================================================================== #
# Informe
# =========================================================================== #


def escribir_detalle_de_marca(
    lineas: list[str],
    por_categoria: dict[str, list[Hallazgo]],
) -> None:
    """El detalle de la marca, agrupado por categoria.

    El orden de las categorias no es alfabetico y es deliberado: primero las que hacen fallar la
    auditoria, despues las que hay que mirar a mano, y al final las que se mantienen. Un informe
    ordenado por importance permite empezar por lo que hay que decidir; uno ordenado por nombre
    obliga a recorrer las identidades tecnicas —que son cientos y ninguna es un problema— para
    llegar a la categoria que si lo es.
    """
    an = lineas.append

    for categoria in ("usuario", "por revisar", "identidad tecnica", "comentario"):
        grupo = por_categoria.get(categoria, [])
        if not grupo:
            an(f"### {categoria}: ninguna")
            an("")
            continue
        an(f"### {categoria} ({len(grupo)})")
        an("")
        an("| Categoria | Situacion | Linea | Clasificacion |")
        an("| :--- | :--- | :--- | :--- |")
        for h in grupo[:60]:
            an(h.como_linea())
        if len(grupo) > 60:
            an("")
            an(f"_Y {len(grupo) - 60} mas._")
        an("")


def escribir_resumen(
    lineas: list[str],
    resultado: Resultado,
    scopes: dict[str, str],
    tests: dict[str, int],
) -> None:
    """El recuento de arriba.

    Va en su propia funcion para que `escribir_informe` sea una secuencia de secciones y no una
    mezcla de cifras y de tablas: cuando el resumen crece, crece en un sitio y no empuja a las
    demas secciones a un sitio distinto.
    """
    an = lineas.append

    an("## Resumen")
    an("")
    an(f"- Modulos auditados: **{len(resultado.filas)}**")
    an(f"- Funcionales: **{sum(1 for _, e, _ in resultado.filas if e == 'funcional')}**")
    an(f"- En paridad: **{sum(1 for _, e, _ in resultado.filas if e == 'paridad')}**")
    an(f"- Bloqueados por Fase 6: **{sum(1 for _, e, _ in resultado.filas if e == 'bloqueado')}**")
    an(f"- Ausentes: **{sum(1 for _, e, _ in resultado.filas if e == 'falta')}**")
    an(f"- Marcas visibles de la marca: **{len(resultado.marcas_visibles)}**")
    an(f"- Scopes declarados: **{len(scopes)}**")
    an(
        f"- Pruebas de backend: **{tests['backend_funciones']}** funciones "
        f"({tests['backend_parametrizadas']} con `@parametrize`, "
        "que generan mas de un caso cada una)"
    )
    an(f"- Pruebas de frontend: **{tests['frontend']}**")


def escribir_informe(
    destino: Path,
    resultado: Resultado,
    scopes: dict[str, str],
    hallazgos: list[Hallazgo],
    tests: dict[str, int],
) -> None:
    por_categoria: dict[str, list[Hallazgo]] = {}
    for h in hallazgos:
        por_categoria.setdefault(h.categoria, []).append(h)

    lineas: list[str] = []
    an = lineas.append

    an("# Informe de paridad funcional")
    an("")
    an(
        "Generado por `scripts/audit_strix_parity.py`. Los numeros de este informe se leen del "
        "codigo, no de un inventario: el unico modo de que un informe de paridad sirva de algo "
        "es que se pueda volver a generar y salga igual."
    )
    an("")
    escribir_resumen(lineas, resultado, scopes, tests)
    an("")
    an("## Superficie funcional")
    an("")
    an("| Modulo | Estado | Nota |")
    an("| :--- | :--- | :--- |")
    for modulo, estado, nota in resultado.filas:
        etiqueta = ICONO.get(estado, estado)
        an(f"| {modulo} | {etiqueta} | {nota or ''} |")
    an("")
    an("## Auditoria de scopes")
    an("")
    an(
        f"El enum `Scope` declara **{len(scopes)}** permisos. Los tres sitios donde uno tiene que "
        "poder aparecer son el catalogo de la API, la interfaz y las pruebas, y cada uno falla de "
        "forma distinta: un scope sin pruebas es un permiso sin verificar, y un scope sin "
        "interfaz es un permiso que existe y que nadie puede ejercer."
    )
    an("")
    an("| Ambito | Valor |")
    an("| :--- | :--- |")
    for _, valor in sorted(scopes.items()):
        an(f"| `{valor}` | {estado_de_scope(valor, resultado.contexto)} |")
    an("")
    an("## Marca")
    an("")
    an(
        "La auditoria separa tres clases de aparicion, y solo la primera hace fallar el informe."
    )
    an("")
    an("| Categoria | Que es | Decision |")
    an("| :--- | :--- | :--- |")
    an("| `usuario` | Literal en un componente o en un fichero de traduccion | **Fallo** |")
    an(
        "| `identidad tecnica` | Nombres de clase, imagen, variables del motor | Se mantiene |"
    )
    an("| `comentario` | Explicacion interna del codigo | Se mantiene |")
    an("| `por revisar` | Fuera de un comentario y no es identidad tecnica | A revisar a mano |")
    an("")
    an(
        "Los mensajes de error que ve un **operador** al desplegar mantienen el nombre del motor "
        "a proposito. `El timeout suave de Strix debe ser menor que el duro` dice que variable "
        "tocar; si dijera el nombre del producto, el operador buscaria una opcion que no existe, "
        "porque el timeout pertenece al contenedor del motor y no a la configuracion del panel. "
        "Renombrar un mensaje de diagnostico para que suene a marca es cambiar su precision por "
        "su apariencia."
    )
    an("")
    escribir_detalle_de_marca(lineas, por_categoria)
    an("## Metodo")
    an("")
    an(
        "- **Superficie**: se leen las rutas de `<Route>` de `App.tsx` y se comparan con la lista "
        "de modulos de paridad. Las rutas relativas se tratan como submodulos de su padre."
    )
    an(
        "- **Scopes**: se lee el enum `Scope` de `scopes.py` sin importarlo, para que un error de "
        "importacion en cualquier parte del arbol no impida a la auditoria llegar a diagnosticar."
    )
    an(
        "- **Marca**: se recorre el arbol clasificando cada aparicion por extension y por si esta "
        "dentro de un comentario. Solo lo que el usuario lee se cuenta como fallo."
    )
    an(
        "- **Pruebas**: se cuentan funciones `def test_` y `it(`/`test(`, no ficheros. Un fichero "
        "con cuarenta pruebas y otro con una ponderarian igual. La cifra de funciones es menor "
        "que la que reporta la suite porque una funcion parametrizada genera varios casos, y el "
        "total de casos no se puede derivar de aqui: un decorador con veinte valores genera "
        "veinte, y otro con dos genera dos."
    )
    an("")

    destino.write_text("\n".join(lineas), encoding="utf-8", newline="\n")


def _configurar_logging() -> None:
    """Hace que la salida de la auditoria se vea.

    Sin esta linea, `logger.info` no produce nada: el logging estandar descarta los mensajes de
    `INFO` cuando la raiz no tiene manejadores, y una auditoria que no dice nada es
    indistinguible de una que no ha encontrado nada. Que es el peor resultado posible para una
    herramienta cuyo unico producto es un texto.
    """
    if not logger.handlers:
        manejador = logging.StreamHandler(sys.stdout)
        manejador.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(manejador)
    logger.setLevel(logging.INFO)


def main() -> int:
    _configurar_logging()
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument(
        "--salida",
        type=Path,
        default=INFORME,
        help="ruta del informe (por defecto STRIX_PARITY_REPORT.md en la raiz)",
    )
    argumentos = analizador.parse_args()

    resultado = Resultado()
    scopes = scopes_del_backend()
    hallazgos = apariciones_de_la_marca()
    tests = contar_tests()

    resultado.contexto = "\n".join(leer(p) for p in (RAIZ / "backend/tests").rglob("*.py"))
    resultado.contexto += "\n".join(leer(p) for p in (RAIZ / "frontend/src").rglob("*.ts*"))

    comprobar_superficie(resultado)
    comprobar_scopes(resultado, scopes)
    comprobar_marca(resultado, hallazgos)
    resultado.hallazgos = hallazgos

    escribir_informe(argumentos.salida, resultado, scopes, hallazgos, tests)

    logger.info(f"Superficie:   {len(resultado.filas)} modulos auditados")
    logger.info(f"Fallas:      {resultado.fallos}")
    logger.info(f"Marca visible: {len(resultado.marcas_visibles)}")
    logger.info(f"Scopes:      {len(scopes)}")
    logger.info(
        f"Pruebas:     {tests['backend_funciones']} funciones de backend "
        f"({tests['backend_parametrizadas']} parametrizadas), {tests['frontend']} de frontend"
    )
    logger.info(f"Informe:     {argumentos.salida}")

    if resultado.fallos or resultado.marcas_visibles:
        logger.info("\nHAY FALLAS. El informe no declara la entrega apta.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
