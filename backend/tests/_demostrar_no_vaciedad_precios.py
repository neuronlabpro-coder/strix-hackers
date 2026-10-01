"""Demostracion de no-vaciedad: reintroduce cada defecto y comprueba que su test falla.

Se ejecuta como script, no como pytest, porque hay que **volver a poner las cosas como
estaban** entre una prueba y otra, y eso a mano es donde se corrompe un fichero. Cada
paso:

1. Guarda los bytes originales en memoria.
2. Escribe el defecto.
3. Ejecuta **solo** el test que deberia detectarlo.
4. Restaura los bytes y **verifica** que la restauracion es byte a byte identica.

Si el test que deberia fallar pasa, el script lo cuenta como fallo: un test que no falla
con el defecto puesto no prueba nada, y es peor que no tener test porque da confianza falsa.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: La raiz del proyecto: el script vive en `backend/tests/`, y los ficheros que parchea son
#: relativos a la raiz, no a `backend`.
RAIZ = Path(__file__).resolve().parents[2]

#: (nombre del defecto, fichero, defecto a escribir, test que debe detectarlo)
DEFECTOS: tuple[tuple[str, str, str, str], ...] = (
    (
        "D1 · la paridad del chat vuelve a ser un literal",
        "backend/apps/chat/billing.py",
        'CREDITOS_POR_USD: Decimal = Decimal("1")',
        "backend/tests/test_precios_plataforma.py::test_ningun_fichero_de_cobro_declara_la_paridad_a_nivel_de_modulo",
    ),
    (
        "D2 · el resumen global congela la paridad al importar",
        "backend/apps/admin/queries.py",
        "CREDITS_PER_USD = credits_per_usd()",
        "backend/tests/test_precios_plataforma.py::test_ningun_fichero_de_cobro_declara_la_paridad_a_nivel_de_modulo",
    ),
    (
        "D3 · la paridad de la llamada a modelo vuelve a tener valor por defecto",
        "backend/apps/llm_router/pricing.py",
        "credits_per_usd: Decimal = DEFAULT_CREDITS_PER_USD,",
        "backend/tests/test_llm_router.py::test_compute_charge_no_tiene_paridad_por_defecto",
    ),
    (
        "D4 · la paridad se cachea en la primera lectura y ya no se relee",
        "backend/apps/billing/pricing.py",
        "    return precios_vigentes().credits_per_usd",
        "backend/tests/test_precios_plataforma.py::test_el_precio_se_relee_sin_reiniciar_el_proceso",
    ),
    (
        "D5 · sin fila de precios, la carga lanza en vez de respaldo",
        "backend/apps/billing/pricing.py",
        "        raise RuntimeError('no hay fila de precios')",
        "backend/tests/test_precios_plataforma.py::test_sin_fila_se_cobra_con_la_configuracion_y_no_falla",
    ),
    (
        "D6 · el precio leido no se cuantiza a la escala de la columna",
        "backend/apps/billing/pricing.py",
        "credits_per_usd=fila.credits_per_usd,",
        "backend/tests/test_precios_plataforma.py::test_el_precio_leido_de_la_base_esta_cuantizado",
    ),
    (
        "D7 · el quickest se cobra con una proporcion fija",
        "backend/apps/billing/pricing.py",
        "return (base * Decimal('0.5')).quantize(_CREDIT_QUANTUM)",
        "backend/tests/test_precios_plataforma.py::test_el_quick_es_la_proporcion_del_precio_vigente",
    ),
    (
        "D8 · la instantanea de precios deja de ser inmutable",
        "backend/apps/billing/pricing.py",
        "@dataclass(slots=True)",
        "backend/tests/test_precios_plataforma.py::test_la_instantancia_no_se_puede_mutar",
    ),
    (
        "E1 · el precio de los creditos vuelve a suponer la paridad 1:1",
        "backend/apps/billing/schemas.py",
        "    unidades = spend * credits_per_usd() / (Decimal(\"1.00\") - descuento)",
        "backend/tests/test_admin_precios.py::test_la_paridad_manda_de_verdad_en_el_precio",
    ),
    (
        "E2 · el descuento se lee del primer tramo en vez de tener respaldo",
        "backend/apps/billing/catalogo.py",
        "        aplicable = DESCUENTO_SIN_TRAMO",
        "backend/tests/test_admin_precios.py::test_un_catalogo_sin_tramos_cobra_sin_descuento",
    ),
    (
        "E3 · el motivo del cambio pasa a ser opcional",
        "backend/apps/billing/admin_router.py",
        "    motivo: str = Field(default=\"x\", max_length=255)",
        "backend/tests/test_admin_precios.py::test_el_motivo_corto_se_rechaza_antes_de_tocar_la_base",
    ),
    (
        "E4 · un PATCH sin ningun campo se acepta como si fuera un acierto",
        "backend/apps/billing/admin_router.py",
        "        if not cambios:",
        "backend/tests/test_admin_precios.py::test_un_patch_vacio_se_rechaza",
    ),
    (
        "E5 · el cambio de precio deja de escribirse en la traza",
        "backend/apps/billing/admin_router.py",
        "        await _registrar_cambio(",
        "backend/tests/test_admin_precios.py::test_cambiar_un_precio_deja_un_asiento_por_campo",
    ),
    (
        "E6 · el proceso deja de refrescar los precios tras guardar",
        "backend/apps/billing/admin_router.py",
        "    await cargar_precios(session)",
        "backend/tests/test_admin_precios.py::test_cambiar_un_precio_deja_un_asiento_por_campo",
    ),
    (
        "E7 · el catalogo de respaldo deja de coincidir con el codigo",
        "backend/apps/billing/catalogo.py",
        "        packs=tuple(sorted(schemas.CREDIT_PACKS.items())),",
        "backend/tests/test_admin_precios.py::test_el_catalogo_de_arranque_coincide_con_el_codigo",
    ),
)

#: Anclas de los defectos que se aplican a `billing/pricing.py`. Se comprueban antes de
#: aplicar nada: un guion que aplica el defecto donde ya no toca produce una demostracion
#: que no demuestra nada y aun asi parece que si.
_ANCLA_QUICK = (
    "        return (base * precios.quick_scan_credit_multiplier).quantize(_CREDIT_QUANTUM)"
)
_ANCLA_CARGA = "    if fila is None:\n        _vigentes = None\n        return None"
_ANCLA_FROZEN = "@dataclass(frozen=True, slots=True)\nclass PlatformPrices:"
_ANCLA_PARIDAD = "    return precios_vigentes().credits_per_usd"


def parche(contenido: str, defecto: str) -> str | None:
    """Devuelve el contenido con el defecto puesto, o `None` si el ancla no aparece.

    Cada defecto se reconoce por su **texto**, no por su posición: si alguien reformatea el
    fichero, el guion dice «el ancla no aparece» en vez de aplicar un defecto en un sitio que ya
    no es ese. Un guion que aplica el defecto donde no toca es peor que uno que no lo aplica.
    """

    if defecto == "CREDITOS_POR_USD: Decimal = Decimal(\"1\")":
        ancla = '_CUATRO_DECIMALES: Decimal = Decimal("0.0001")'
        if ancla not in contenido:
            return None
        return contenido.replace(ancla, defecto + "\n\n" + ancla, 1)

    if defecto == "CREDITS_PER_USD = credits_per_usd()":
        # La constante volvia accompanied de su import; hoy ese import ya no esta en el
        # fichero porque ruff lo borro como no usado al quitar la constante. Sin el, el defecto
        # no compila y el test passaria por un motivo que no es el suyo.
        ancla = "from backend.apps.billing.models import"
        if ancla not in contenido:
            return None
        return contenido.replace(
            ancla,
            "from backend.apps.billing.pricing import credits_per_usd\n" + ancla + "\n"
            + defecto + "\n",
            1,
        )

    if defecto == "credits_per_usd: Decimal = DEFAULT_CREDITS_PER_USD,":
        if "    credits_per_usd: Decimal,\n" not in contenido:
            return None
        return contenido.replace(
            "    credits_per_usd: Decimal,\n",
            defecto + "\n    DEFAULT_CREDITS_PER_USD: Final[Decimal] = Decimal(\"1\"),\n",
            1,
        )

    if defecto == "    return precios_vigentes().credits_per_usd":
        # La paridad se cachea en la primera lectura: el precio editado desde el panel ya no
        # se ve en el proceso hasta que reinicie.
        if _ANCLA_PARIDAD not in contenido:
            return None
        return contenido.replace(
            _ANCLA_PARIDAD,
            "    global _paridad_cacheada\n"
            "    if _paridad_cacheada is None:\n"
            "        _paridad_cacheada = precios_vigentes().credits_per_usd\n"
            "    return _paridad_cacheada",
            1,
        ).replace(
            "_vigentes: PlatformPrices | None = None",
            "_vigentes: PlatformPrices | None = None\n_paridad_cacheada: Decimal | None = None",
            1,
        )

    if defecto == "        raise RuntimeError('no hay fila de precios')":
        if _ANCLA_CARGA not in contenido:
            return None
        return contenido.replace(_ANCLA_CARGA, defecto, 1)

    if defecto == "credits_per_usd=fila.credits_per_usd,":
        viejo = "credits_per_usd=fila.credits_per_usd.quantize(_PRECISION_PRECIOS),"
        if viejo not in contenido:
            return None
        return contenido.replace(viejo, defecto, 1)

    if defecto == "return (base * Decimal('0.5')).quantize(_CREDIT_QUANTUM)":
        if _ANCLA_QUICK not in contenido:
            return None
        return contenido.replace(_ANCLA_QUICK, "        " + defecto, 1)

    if defecto == "@dataclass(slots=True)":
        if _ANCLA_FROZEN not in contenido:
            return None
        return contenido.replace(_ANCLA_FROZEN, "@dataclass(slots=True)\nclass PlatformPrices:", 1)

    # --- Los del catálogo y la consola, que llegan en el segundo tramo.
    if defecto == "    unidades = spend * credits_per_usd() / (Decimal(\"1.00\") - descuento)":
        if defecto not in contenido:
            return None
        return contenido.replace(defecto, '    unidades = spend / (Decimal("1.00") - descuento)', 1)

    if defecto == "        aplicable = DESCUENTO_SIN_TRAMO":
        # El defecto es volver al indice a cero, que es lo que revienta cuando un operador
        # desactiva el tramo base. El ancla es la linea del respaldo, no la del indice.
        ancla = defecto
        if ancla not in contenido:
            return None
        return contenido.replace(ancla, "        aplicable = self.tramos[0][1]", 1)

    if defecto == '    motivo: str = Field(default="x", max_length=255)':
        return contenido.replace('    motivo: str = Field(min_length=3, max_length=255)',
                                 defecto, 1)

    if defecto == "        if not cambios:":
        if defecto not in contenido:
            return None
        return contenido.replace(defecto, "        if False:", 1)

    if defecto == "        await _registrar_cambio(":
        if defecto not in contenido:
            return None
        return contenido.replace(defecto, "        await _ignorar_cambio(", 1)

    if defecto == "    await cargar_precios(session)":
        if defecto not in contenido:
            return None
        # La llamada se comenta en vez de desaparecer: borrarla dejaria el cuerpo de la funcion
        # sin ninguna sentencia de refresco, que es un error de sintaxis y no el defecto que
        # esta prueba quiere introducir.
        return contenido.replace(defecto, "    del precios_vigentes  # sin refrescar", 1)

    if defecto == "        packs=tuple(sorted(schemas.CREDIT_PACKS.items())),":
        if defecto not in contenido:
            return None
        return contenido.replace(
            defecto, "        packs=((999, Decimal(\"999.00\")),),", 1)

    return None


#: El ejecutable con el que se lanza la suite. Es un literal de este fichero, nunca viene de
#: un argumento ni de un fichero, y por eso no hay nada que inyectar: quien llama es
#: `main()`, que lo usa con una lista fija.
_UV = "uv"


def ejecuta(test: str) -> bool:
    """Devuelve `True` si el test **falla**, que es lo que hay que ver."""

    proceso = subprocess.run(  # noqa: S603
        [
            _UV, "run", "--project", "backend", "python", "-m", "pytest",
            test, "-q", "-p", "no:randomly", "--no-header", "-x",
        ],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proceso.returncode != 0


def main() -> int:
    fallos: list[str] = []
    for nombre, relativo, defecto, test in DEFECTOS:
        ruta = RAIZ / relativo
        original = ruta.read_bytes()
        texto = original.decode("utf-8")
        ahora = parche(texto, defecto)
        if ahora is None or ahora == texto:
            print(f"  [ ] {nombre} -- el ancla no aparece, el defecto no llego a aplicarse")
            fallos.append(nombre + " (ancla)")
            continue
        try:
            ruta.write_bytes(ahora.encode("utf-8"))
            detectado = ejecuta(test)
        finally:
            ruta.write_bytes(original)
            restaurado = ruta.read_bytes() == original
        if not restaurado:
            print(f"  [X] {nombre} -- el fichero NO quedo como estaba")
            fallos.append(nombre + " (restauracion)")
            continue
        marca = "V" if detectado else " "
        print(f"  [{marca}] {nombre}")
        if not detectado:
            fallos.append(nombre + " (el test paso con el defecto puesto)")

    print("")
    if fallos:
        print(f"  {len(fallos)} defecto(s) NO detectado(s):")
        for fallo in fallos:
            print(f"    - {fallo}")
        return 1
    print(f"  los {len(DEFECTOS)} defectos los detecta su test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
