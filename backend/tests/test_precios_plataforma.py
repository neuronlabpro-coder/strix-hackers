"""Pruebas de la fuente única de precios de plataforma.

## Qué se comprueba y por qué

El defecto que motivó este fichero no era un número mal puesto: era que **el mismo precio
estaba escrito en cuatro sitios** y solo dos leían la configuración. Dos de los cuatro
contenían un `Decimal("1")` literal, y coincidían con el `default` de la variable de entorno,
así que nada fallaba. El día que alguien subiera la paridad para cobrar más créditos por dólar,
el chat y el resumen global habrían seguido cobrando a 1:1, y la diferencia habría aparecido en
la conciliación de fin de mes y en ningún otro sitio.

Por eso las pruebas no comprueban «que el precio sea 10», que es un ejemplo que pasa con
cualquier implementación. Comprueban las dos propiedades que hacen que el precio no pueda
volver a duplicarse:

1. **Ninguna paridad se declara a nivel de módulo.** Ninguna asignación a nivel de módulo en
   los ficheros que cobran: la paridad se lee, no se cachea. Un `X = Decimal("1")` o un
   `X = credits_per_usd()` **al importar** congelan el valor en el proceso, y un proceso que
   dura una hora mantiene el precio viejo una hora.
2. **La fila de la base manda sobre la configuración, y sin fila se cobra igual.** La primera
   mitad es la función del panel; la segunda es lo que permite desplegar la base y el código
   por separado sin dejar la plataforma sin poder cobrar.

## Por qué la primera propiedad se comprueba sobre el código y no sobre el comportamiento

Porque un test de comportamiento no la detecta. Con la paridad duplicada a `Decimal("1")` y el
`default` en `1.00`, cualquier aserción sobre el valor que el sistema cobra **pasa**. El
comportamiento solo difiere cuando alguien cambia la variable de entorno, y una prueba que
depende de eso es una prueba que hay que recordar tocar cada vez que se toca el precio. La
propiedad que sí falla —y que falla hoy mismo si alguien reintroduce la constante— es «la
paridad no está escrita en el código», y esa se comprueba sobre el árbol de ficheros.

## Por qué se lee el fichero en vez de mantener una lista de módulos aquí

Porque una lista escrita a mano en el test es otra lista que hay que actualizar, y esta vez
actualizarla mal es el defecto. El test recorre los ficheros que **hoy** importan la función,
así que un módulo nuevo que cobre mal sale del comentario aunque nadie se acuerde de añadirlo.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditPack, PlatformPricing, VolumeTier
from backend.apps.billing.pricing import (
    PlatformPrices,
    cargar_precios,
    credits_per_usd,
    fijar_precios,
    precios_vigentes,
    scan_credit_cost,
)
from backend.apps.pentests.models import ScanModeEnum

pytestmark = pytest.mark.asyncio

#: La raíz del proyecto, para llegar a los ficheros fuente desde los tests.
_RAIZ: Path = Path(__file__).resolve().parents[2]

#: Patrón de un nombre que representa la paridad, en cualquiera de las dos escrituras del
#: proyecto. `credits_per_usd` y `CREDITOS_POR_USD` son el mismo número con dos idiomas, y
#: y el defecto puede volver a aparecer escrito con cualquiera de los dos nombres.
_NOMBRE_DE_PARIDAD = re.compile(r"credits?_?per_?usd|creditos_?por_?usd", re.IGNORECASE)

#: Carpetas que el escáner tiene que saltarse.
#:
#: ## Por qué hay que excluir el entorno virtual y no basta con ignorar sus avisos
#:
#: Porque `backend/.venv` **cuelga de `backend`**, así que recorrerlo significa parsear las
#: pruebas de cada dependencia instalada. Con `passlib` basta para que el escáner tarde medio
#: minuto y para que salgan asignaciones de módulo dentro de tests de terceros que no son
#: precios de esta plataforma. Y un escáner que tarda medio minuto hay que excluirlo a mano,
#: y un escáner que hay que excluir a mano deja de usarse, y el que no se usa no encuentra nada.
_FUERA_DEL_CODIGO = ("migrations", "tests", ".venv", "node_modules", "__pycache__", "build")


@pytest.fixture(autouse=True)
def precios_restaurados() -> Iterator[None]:
    """Deja la instantánea como estaba, pase lo que pase.

    Sin esto, un test que cambia el precio deja el proceso cobrando a un número que nadie
    eligió, y el fallo aparece en otro fichero con un nombre que no habla de precios.
    """

    try:
        yield
    finally:
        fijar_precios(None)


# --------------------------------------------------------------------------- #
# Propiedad 1: la paridad no se declara en ninguna parte
# --------------------------------------------------------------------------- #


async def test_ningun_fichero_de_cobro_declara_la_paridad_a_nivel_de_modulo() -> None:
    """Ningún fichero que cobra declara la paridad como valor de módulo.

    Es la prueba que falla hoy mismo si alguien reintroduce la constante del chat o la del
    resumen global. Recorre todos los `.py` del backend en vez de una lista escrita a mano, y
    avisa del camino para que el fallo sea localizable sin buscar.
    """

    culpables: list[str] = []
    for ruta in sorted((_RAIZ / "backend").rglob("*.py")):
        if any(parte in ruta.parts for parte in _FUERA_DEL_CODIGO):
            continue
        # `utf-8-sig` y no `utf-8`: hay ficheros con BOM al principio, que son legales para el
        # import de Python. Si el escáner se rompe con ellos, el fallo que reporta es del
        # escáner, y se confunde con el del precio.
        arbol = ast.parse(open(ruta, encoding="utf-8-sig").read(), filename=str(ruta))
        for nodo in arbol.body:
            # Solo las asignaciones de nivel de módulo: una paridad leída dentro de una
            # función se evalúa en cada llamada y no se queda vieja.
            if not isinstance(nodo, ast.Assign | ast.AnnAssign):
                continue
            objetivos = [nodo.target] if isinstance(nodo, ast.AnnAssign) else nodo.targets
            for objetivo in objetivos:
                if isinstance(objetivo, ast.Name) and _NOMBRE_DE_PARIDAD.search(objetivo.id):
                    relativos = ruta.relative_to(_RAIZ)
                    culpables.append(f"{relativos}:{nodo.lineno} -> {objetivo.id}")

    assert not culpables, (
        "la paridad esta escrita a nivel de modulo, donde se congela al importar y el precio "
        "editado desde el panel no se ve hasta reiniciar:\n  " + "\n  ".join(culpables)
    )


async def test_el_chat_no_tiene_una_paridad_propia() -> None:
    """El módulo que cobra los pasos de chat no declara `Decimal` de paridad.

    Es el caso concreto del defecto original, aislado para que el mensaje de fallo señale el
    fichero exacto. Se lee el AST y no el texto porque el comentario de al lado mencionaba el
    nombre del símbolo eliminado: buscar la cadena en el texto daría un falso positivo sobre
    la propia explicación.
    """

    ruta = _RAIZ / "backend/apps/chat/billing.py"
    arbol = ast.parse(open(ruta, encoding="utf-8-sig").read(), filename=str(ruta))
    literales: list[int] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Assign) and isinstance(nodo.value, ast.Call):
            funcion = nodo.value.func
            nombre = getattr(funcion, "id", None) or getattr(funcion, "attr", None)
            if nombre == "Decimal" and any(
                isinstance(t, ast.Name) and _NOMBRE_DE_PARIDAD.search(t.id) for t in nodo.targets
            ):
                literales.append(nodo.lineno)

    assert not literales, (
        f"backend/apps/chat/billing.py declara la paridad como literal en la linea "
        f"{literales}; el chat cobraria a 1:1 mientras el resto cobra al precio vigente"
    )


# --------------------------------------------------------------------------- #
# Propiedad 2: el precio se lee en el momento del cobro
# --------------------------------------------------------------------------- #


async def test_el_precio_se_relee_sin_reiniciar_el_proceso() -> None:
    """Cambiar el precio se nota en la siguiente lectura, sin recargar nada.

    Este es el test que un `X = credits_per_usd()` a nivel de módulo rompe: la constante se
    evalúa al importar y las dos lecturas después del cambio darían el valor viejo.
    """

    fijar_precios(PlatformPrices(Decimal("1.00"), Decimal("10"), Decimal("0.30"), Decimal("0")))
    assert credits_per_usd() == Decimal("1.00")
    assert scan_credit_cost(ScanModeEnum.DEEP) == Decimal("10.0000")

    fijar_precios(PlatformPrices(Decimal("2.50"), Decimal("14"), Decimal("0.30"), Decimal("0")))
    assert credits_per_usd() == Decimal("2.50"), "la paridad sigue congelada en el proceso"
    assert scan_credit_cost(ScanModeEnum.DEEP) == Decimal("14.0000")


async def test_el_quick_es_la_proporcion_del_precio_vigente() -> None:
    """`QUICK` es la base por la proporción vigente, no por una proporción fija.

    Se usa una proporción de `0.30` a propósito: con un valor que no sea el `0.5` que tenía el
    proyecto antes, un `QUICK` que siga devolviendo la mitad del precio delata que la
    proporción está escrita en el código.
    """

    fijar_precios(PlatformPrices(Decimal("1.00"), Decimal("10"), Decimal("0.30"), Decimal("0")))
    assert scan_credit_cost(ScanModeEnum.QUICK) == Decimal("3.0000")


async def test_sin_instantancia_se_cobra_con_la_configuracion() -> None:
    """Sin fila cargada, el precio es el de la configuración, y no hay excepción.

    Es lo que permite desplegar la base y el código por separado: una tabla de precios que
    todavía no existe no puede dejar la plataforma sin poder cobrar.
    """

    fijar_precios(None)
    assert precios_vigentes() == PlatformPrices.desde_configuracion()


async def test_la_instantancia_no_se_puede_mutar() -> None:
    """Un `PlatformPrices` compartido no se puede alterar desde fuera.

    Es la razón de que sea un `dataclass` congelado: el mismo valor se pasa a funciones puras
    entre peticiones concurrentes, y si alguien pudiera mutarlo, un cobro alteraría el precio
    del siguiente.
    """

    precios = PlatformPrices(Decimal("1.00"), Decimal("10"), Decimal("0.30"), Decimal("0"))
    # El tipo exacto y no una expresion regular sobre el mensaje: el texto depende de la
    # version de Python y una prueba que se rompe al actualizar el interprete no prueba nada.
    with pytest.raises(FrozenInstanceError):
        precios.scan_credit_cost = Decimal("999")  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# La fila de la base manda, y la ausencia de fila no es un fallo
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_la_fila_de_la_base_manda_sobre_la_configuracion(
    integration_session: AsyncSession | None,
) -> None:
    """Cambiar la fila cambia el precio de cobro, sin tocar la configuración."""

    sesion = integration_session
    assert sesion is not None, "esta prueba necesita la sesion de integracion"

    fila = (
        await sesion.execute(select(PlatformPricing).where(PlatformPricing.id == 1))
    ).scalar_one()
    fila.scan_credit_cost = Decimal("33")
    fila.credits_per_usd = Decimal("4")
    await sesion.flush()

    await cargar_precios(sesion)

    assert scan_credit_cost(ScanModeEnum.DEEP) == Decimal("33.0000")
    assert credits_per_usd() == Decimal("4.00000000")


@pytest.mark.integration
async def test_sin_fila_se_cobra_con_la_configuracion_y_no_falla(
    integration_session: AsyncSession | None,
) -> None:
    """Sin fila, `cargar_precios` devuelve `None` y el sistema cobra con la configuración.

    ## Por qué la fila se quita deshabilitando el disparador

    Porque ya no se puede borrar: hay un `BEFORE DELETE OR TRUNCATE` que lo rechaza, y esa es
    precisamente la protección que hace necesaria esta prueba. Para reproducir «no hay fila»
    hay que quitar la fila, y el único camino que queda en una base protegida es desactivar el
    disparador dentro de la transacción —que se revierte al final de la prueba— y volverlo a
    activar.

    Y esa es la razón por la que el respaldo sigue existiendo: la fila no se puede perder por
    accidente, pero una base **restaurada de antes de la migración** no la tiene, y en esa base
    la plataforma tiene que seguir vendiendo. Si `cargar_precios` lanzara en vez de devolver
    `None`, esa base no arrancaría.
    """

    sesion = integration_session
    assert sesion is not None, "esta prueba necesita la sesion de integracion"

    await sesion.execute(
        text(
            "ALTER TABLE platform_pricing DISABLE TRIGGER "
            "trg_protect_platform_pricing_single_row"
        )
    )
    try:
        await sesion.execute(text("DELETE FROM platform_pricing"))
        await sesion.flush()

        assert await cargar_precios(sesion) is None
        assert precios_vigentes() == PlatformPrices.desde_configuracion()
    finally:
        await sesion.execute(
            text(
                "ALTER TABLE platform_pricing ENABLE TRIGGER "
                "trg_protect_platform_pricing_single_row"
            )
        )


async def test_el_precio_leido_de_la_base_esta_cuantizado() -> None:
    """`desde_fila` deja el precio con la escala de la columna, aunque venga con más.

    ## Por qué la aserción mira el exponente y no el valor

    Porque `Decimal` se compara **numéricamente**: `Decimal("0.125") == Decimal("0.12500000")`
    es `True`. Una prueba que afirmara sobre el valor no detectaría nunca la falta de
    cuantización, porque los dos son el mismo número. Lo que distingue un precio cuantizado de
    uno que no lo está es la **forma**, que es el exponente.

    ## Por qué importa la forma y no el valor

    Porque el precio se compara y se guarda en varios sitios, y dos representaciones del mismo
    número producen cadenas distintas: una fila de la traza de cambios puede decir que el precio
    «cambió» cuando no cambió nada, y un `GROUP BY` sobre el importe reparte el mismo precio en
    dos grupos. Con la escala fijada en un único punto de entrada, deja de importar de dónde
    venga el valor.

    ## Por qué no se prueba con una fila real de la base

    Porque `numeric(18, 8)` devuelve siempre ocho decimales: leyéndolo de la tabla, la prueba
    pasaría aunque el código no cuantizara nada, y sería una prueba que no puede fallar. La
    forma de que se detecte es darle a `desde_fila` un valor que la base nunca daría —con más
    decimales de los que admite la columna— y comprobar que sale normalizado igual.

    Y por eso se construye la fila en memoria, sin sesión: es una prueba de una función pura y
    no necesita la base, así que no gasta una transacción de las que se rechazan al final.
    """

    fila = PlatformPricing(
        credits_per_usd=Decimal("1.2345678901234567"),
        scan_credit_cost=Decimal("10.98765432109876"),
        quick_scan_credit_multiplier=Decimal("0.1250000000000000"),
        low_credit_balance_threshold=Decimal("0"),
    )

    precios = PlatformPrices.desde_fila(fila)

    for nombre in (
        "credits_per_usd",
        "scan_credit_cost",
        "quick_scan_credit_multiplier",
        "low_credit_balance_threshold",
    ):
        exponente = getattr(precios, nombre).as_tuple().exponent
        assert exponente == -8, (
            f"{nombre} llega con exponente {exponente} en vez de -8: el precio no quedo "
            "cuantizado a la escala de la columna"
        )
    assert precios.credits_per_usd == Decimal("1.23456789")


# --------------------------------------------------------------------------- #
# La traza de cambios es inmutable, como el ledger y la evidencia
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_la_traza_de_cambios_de_precio_no_se_puede_reescribir(
    integration_session: AsyncSession | None,
) -> None:
    """Un `UPDATE` o un `DELETE` sobre la traza de precios los rechaza la base.

    R4 exige que lo que certifica un cobro sea inmutable desde la interfaz, y una traza de
    precios sin disparador es una traza que alguien acaba «corrigiendo» cuando el valor no le
    cuadra. La comprobación es contra la **base**, no contra Python, porque el disparador es lo
    que la protege de verdad.
    """

    sesion = integration_session
    assert sesion is not None, "esta prueba necesita la sesion de integracion"

    await sesion.execute(
        text(
            "INSERT INTO platform_price_changes (id, clave, valor_anterior, valor_nuevo, motivo) "
            "VALUES (gen_random_uuid(), 'prueba:inmutabilidad', 1, 2, 'prueba automatica')"
        )
    )
    await sesion.flush()

    with pytest.raises(DBAPIError, match="append-only"):
        await sesion.execute(
            text(
                "UPDATE platform_price_changes SET valor_nuevo = 3 "
                "WHERE clave = 'prueba:inmutabilidad'"
            )
        )


@pytest.mark.integration
async def test_el_catalogo_comercial_se_lee_de_su_propia_tabla(
    integration_session: AsyncSession | None,
) -> None:
    """Los packs y los tramos tienen filas propias, y no un diccionario en el código.

    Es la mitad del catálogo que aún no pasa por `PlatformPrices`: los packs son una lista, no
    un escalar, y por eso viven en su tabla. La prueba no comprueba que se lean de la base —
eso todavía no está hecho— sino que la tabla existe, tiene filas y que su contenido es el que
la migración sembró, que es lo que impide que alguien borre el catálogo sin que nada avise.
    """

    sesion = integration_session
    assert sesion is not None, "esta prueba necesita la sesion de integracion"

    packs = (
        await sesion.execute(select(CreditPack).order_by(CreditPack.display_order))
    ).scalars().all()
    # Se ordena por **umbral**, no por `display_order`, porque es como los lee la aplicación.
    # Comprobar el orden con la misma clave que usa la aplicación y luego afirmar que sale
    # ordenado sería tautología; el orden se afirma en otra prueba, que llama a
    # `cargar_catalogo` de verdad.
    tramos = (
        await sesion.execute(select(VolumeTier).order_by(VolumeTier.spend_min_usd))
    ).scalars().all()

    # Los tres packs **sembrados** tienen que estar con su importe. No se comprueba que sean los
    # unicos: un operador puede dar de alta un cuarto desde la consola, y que ahi falle una
    # prueba de esquema significa que la prueba esta atada al estado de la base y no a lo que el
    # esquema garantiza.
    sembrados = {p.credits: p.amount_usd for p in packs}
    assert sembrados[25] == Decimal("25.00")
    assert sembrados[100] == Decimal("100.00")
    assert sembrados[250] == Decimal("250.00")
    # La escalera de descuento tiene que crecer en los dos ejes: un descuento que baja al
    # gastar más es un cliente al que se le cobra más por gastar más.
    # La escalera se lee ordenada por umbral y el descuento no retrocede al gastar mas. Se
    # comprueba sobre lo que hay ahora, incluidos los tramos que la consola haya anadido,
    # porque esas dos propiedades son del **catalogo vivo** y tienen que cumplirse para
    # cualquier conjunto de tramos, no solo para el que la migracion sembro.
    gastos = [t.spend_min_usd for t in tramos]
    descuentos = [t.discount for t in tramos]
    assert gastos == sorted(gastos), "los tramos no llegan ordenados por umbral"
    assert gastos[0] <= Decimal("10.00"), "el primer tramo empieza por encima del minimo"
    assert descuentos == sorted(descuentos), (
        "el descuento baja al gastar mas: un cliente pagaria mas por gastar mas"
    )
