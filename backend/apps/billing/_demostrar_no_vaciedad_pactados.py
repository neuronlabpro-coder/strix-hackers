"""Demuestra que cada prueba de `test_organization_prices.py` **no está vacía**.

## Qué hace

Reintroduce un defecto cada vez, ejecuta la prueba que debería detectarlo, y comprueba que
**falla**. Si una prueba pasa con el defecto puesto, es una prueba que no comprueba nada: da
verde sobre un sistema roto, que es peor que no tenerla.

Al final restaura el código y comprueba que todo vuelve a pasar. Si algo no se restaura, el script
lo dice con el nombre del fichero, porque dejar un defecto inyectado en el árbol es la forma más
rápida de convertir una demostración en un incidente.

## Por qué el defecto se reintroduce **en el código** y no en una copia

Porque una copia no es el sistema. Si la prueba pasara contra una copia del módulo y fallara
contra el de verdad, la prueba estaría midiendo el path equivocado y el defecto seguiría vivo en
producción sin que nadie lo notara. Aquí se rompe el fichero que se importa.

## Por qué cada defecto se restaura con el fichero entero

Porque parchear sobre un parche es como se acumulan los cambios a medias: el primer fallo deja el
segundo con el texto que ya no existe, y el script termina reescribiendo código que no ha mirado.
Guardar el texto original y volver a volcarlo hace que el estado final sea el inicial por
construcción, no por buena memoria.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
#: La raíz del repositorio: tres niveles por encima de este fichero. Es donde vive `backend`,
#: y de donde se lanza `uv --project backend`, así que el subproceso tiene que correr desde
#: ahí y no desde el directorio del módulo que se está parcheando.
RAIZ_REPO = os.path.abspath(os.path.join(RAIZ, "..", "..", ".."))
PRECIOS = os.path.join(RAIZ, "organization_prices.py")
ROUTER = os.path.join(RAIZ, "organization_prices_router.py")
PRUEBAS = os.path.join(RAIZ, "..", "..", "tests", "test_organization_prices.py")


class Defecto:
    """Un defecto a reintroducir, y las pruebas que tienen que detectarlo."""

    def __init__(
        self,
        nombre: str,
        fichero: str,
        viejo: str,
        nuevo: str,
        pruebas: list[str],
    ) -> None:
        self.nombre = nombre
        self.fichero = fichero
        self.viejo = viejo
        self.nuevo = nuevo
        self.pruebas = pruebas

    def aplicar(self) -> None:
        with open(self.fichero, encoding="utf-8", newline="") as f:
            texto = f.read()
        if self.viejo not in texto:
            raise SystemExit(
                f"DEFECTO {self.nombre}: el texto a cambiar no existe en {self.fichero}.\n"
                f"Se busca:\n{self.viejo[:200]}"
            )
        with open(self.fichero, "w", encoding="utf-8", newline="") as f:
            f.write(texto.replace(self.viejo, self.nuevo, 1))


DEFECTOS: list[Defecto] = [
    Defecto(
        nombre="el vigente se decide al cargar, no al cobrar",
        fichero=PRECIOS,
        viejo=(
            "            clave: tuple(sorted(cadena, key=lambda p: p.valido_desde, reverse=True))\n"
        ),
        nuevo=(
            "            clave: tuple(sorted(cadena, key=lambda p: p.valido_desde, "
            "            reverse=True)[:1])\n"

        ),
        pruebas=[
            "test_la_carga_no_tapa_un_pactado_vigente_con_uno_futuro",
        ],
    ),
    Defecto(
        nombre="no se filtra por `valido_desde`: un precio futuro se cobra desde hoy",
        fichero=PRECIOS,
        viejo="            if pactado.valido_desde > ahora:\n",
        nuevo="            if False:  #nfiltro-desde\n",
        pruebas=[
            "test_un_pactado_futuro_no_aplica_y_no_tapa_al_vigente",
            "test_cuando_llega_la_fecha_manda_el_futuro",
        ],
    ),
    Defecto(
        nombre="no se filtra por `valido_hasta`: un acuerdo caducado sigue cobrando",
        fichero=PRECIOS,
        viejo=(
            "            if pactado.valido_hasta is not None and pactado.valido_hasta <= ahora:\n"
        ),
        nuevo="            if False:  #nfiltro-hasta\n",
        pruebas=[
            "test_un_pactado_caducado_deja_de_aplicar",
            "test_si_todos_estan_caducados_vuelve_a_plataforma",
        ],
    ),
    Defecto(
        nombre="al encontrar un caducado no se baja por la cadena",
        fichero=PRECIOS,
        viejo="                continue\n            vigentes[clave] = pactado",
        nuevo="                break\n            vigentes[clave] = pactado",
        pruebas=["test_caducado_el_mas_reciente_vuelve_a_mandar_el_anterior"],
    ),
    Defecto(
        nombre="la resolucion ignora los pactados y cobra siempre de plataforma",
        fichero=PRECIOS,
        viejo="    pactados = overrides_de(organization_id)\n",
        nuevo="    pactados: dict[str, object] = {}  #nignorado\n",
        pruebas=[
            "test_el_pactado_sustituye_al_precio_de_plataforma",
            "test_un_pactado_nuevo_sustituye_al_anterior_sin_tocar_el_anterior",
        ],
    ),
    Defecto(
        nombre="la clave no incluye `alcance`: los dos packs se pisan en la cache",
        fichero=PRECIOS,
        viejo="    return operacion.value if alcance is None else f\"{operacion.value}:{alcance}\"",
        nuevo="    return operacion.value  #nalcance",
        pruebas=["test_la_carga_conserva_el_precio_de_cada_pack"],
    ),
    Defecto(
        nombre="pactar cierra el anterior con UPDATE, que el append-only prohibe",
        fichero=ROUTER,
        viejo="    override = OrganizationPriceOverride(\n",
        nuevo=(
            "    for _v in sustitutos:  #ncierra\n"
            "        _v.valido_hasta = desde\n\n"
            "    override = OrganizationPriceOverride(\n"
        ),
        pruebas=["test_pactar_dos_veces_no_borra_ni_reescribe"],
    ),
    Defecto(
        nombre="se dice que sustituye a un pactado que ya habia caducado",
        fichero=ROUTER,
        viejo=(
            "    for candidato in sustitutos:\n"
            "        if candidato.valido_hasta is None or candidato.valido_hasta > desde:\n"
        ),
        nuevo=(
            "    for candidato in sustitutos:  #ncaducado\n"
            "        if True:\n"
        ),
        pruebas=[
            "test_pactar_despues_de_una_caducidad_no_dice_que_sustituye_a_lo_caducado"
        ],
    ),
    Defecto(
        nombre="el POST no dice a quien sustituye",
        fichero=ROUTER,
        viejo="        sustituye_id=anulado.id if anulado is not None else None,\n",
        nuevo="        sustituye_id=None,\n",
        pruebas=["test_pactar_dos_veces_no_borra_ni_reescribe"],
    ),
    Defecto(
        nombre="la marca de vigente ignora la fecha de inicio",
        fichero=ROUTER,
        viejo="        if pactado.valido_desde > ahora:\n            continue\n",
        nuevo="        if False:  #nmarca-desde\n            continue\n",
        pruebas=["test_los_tres_estados_se_marcan_sin_ambiguedad"],
    ),
    Defecto(
        nombre=(
            "la lectura no refresca la cache: el panel pinta un precio y se cobra otro"
        ),
        fichero=ROUTER,
        viejo=(
            "    # Se recarga la caché antes de resolver, para que lo que se pinta sea lo que se "
            "cobra **en\n"
            "    # este proceso**, no lo que quedó en memoria de una petición anterior.\n"
            "    await cargar_overrides(session)\n"
        ),
        nuevo="    #norefresco\n",
        pruebas=["test_la_lectura_trae_el_precio_que_escribio_otro_proceso"],
    ),
    Defecto(
        nombre="pactar un pack inexistente no da error",
        fichero=ROUTER,
        viejo=(
            "    if pack is None:\n"
            "        raise HTTPException(\n"
        ),
        nuevo="    if pack is None:\n"
        "        return pack  #nerror\n"
        "    raise HTTPException(\n",
        pruebas=["test_un_pack_inexistente_no_admite_precio"],
    ),
    Defecto(
        nombre="el cuerpo acepta un precio de cero o negativo",
        fichero=ROUTER,
        viejo="        if self.valor <= 0:\n",
        nuevo="        if False:  #nvalidacion\n",
        pruebas=["test_el_cuerpo_del_pactado_se_rechaza_antes_de_tocar_la_base"],
    ),
]


def correr(seleccion: str) -> tuple[int, str]:
    """Ejecuta las pruebas indicadas y devuelve el código de salida y la salida."""

    # `uv` se resuelve a su ruta absoluta: el script lanza un proceso, y lanzar un proceso por
    # un nombre que solo está en el PATH es confiar en el PATH de quien lo ejecuta.
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("No se encuentra `uv` en el PATH: hace falta para lanzar las pruebas.")

    # S603: el ejecutable y los argumentos salen de este fichero, no de entrada externa. La
    # unica entrada que llega de fuera es el nombre de un test, y va como argumento de `-k`,
    # nunca como parte del comando.
    proceso = subprocess.run(  # noqa: S603
        [
            uv,
            "run",
            "--project",
            "backend",
            "pytest",
            PRUEBAS,
            "-q",
            "-p",
            "no:randomly",
            "-k",
            seleccion,
            "--no-header",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=RAIZ_REPO,
    )
    return proceso.returncode, (proceso.stdout or "") + (proceso.stderr or "")


def main() -> int:
    originales = {}
    for ruta in (PRECIOS, ROUTER):
        with open(ruta, encoding="utf-8", newline="") as f:
            originales[ruta] = f.read()

    print("=" * 78)
    print("DEMOSTRACION DE NO VACIEDAD — precios pactados por organizacion")
    print("=" * 78)

    vacios: list[str] = []
    try:
        for defecto in DEFECTOS:
            defecto.aplicar()
            seleccion = " or ".join(defecto.pruebas)
            codigo, salida = correr(seleccion)
            with open(defecto.fichero, "w", encoding="utf-8", newline="") as f:
                f.write(originales[defecto.fichero])

            if codigo == 0:
                vacios.append(defecto.nombre)
                print(f"  [VACIO] {defecto.nombre}")
                print(f"          las pruebas {seleccion} PASAN con el defecto puesto")
            else:
                fallos = [
                    ln
                    for ln in salida.splitlines()
                    if "FAILED" in ln or "ERROR" in ln
                ]
                resumen = [ln for ln in salida.splitlines() if " passed" in ln or " failed" in ln]
                print(f"  [BIEN ] {defecto.nombre}")
                for linea in fallos:
                    print("          " + linea.replace("FAILED ", "").split(" - ")[0].strip())
                for linea in resumen[-1:]:
                    print(f"          {linea.strip()}")
    finally:
        for ruta, texto in originales.items():
            with open(ruta, "w", encoding="utf-8", newline="") as f:
                f.write(texto)
        print("-" * 78)
        print("  codigo restaurado al estado inicial")

    print("=" * 78)
    codigo, salida = correr("")
    if codigo != 0:
        print("  ATENCION: con el codigo restaurado, las pruebas NO pasan:")
        print(salida[-3000:])
        return 1
    resumen = [ln for ln in salida.splitlines() if " passed" in ln or " failed" in ln]
    print(f"  con el codigo restaurado: {resumen[-1].strip() if resumen else 'todas pasan'}")

    if vacios:
        print(f"  {len(vacios)} prueba(s) VACIAS: {', '.join(vacios)}")
        return 1
    print(f"  {len(DEFECTOS)} defectos reintroducidos, {len(DEFECTOS)} detectados")
    print("  ninguna prueba esta vacia")
    return 0


if __name__ == "__main__":
    sys.exit(main())
