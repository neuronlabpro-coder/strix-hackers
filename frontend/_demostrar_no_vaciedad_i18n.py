"""Demuestra que el verificador de claves de i18n no est\u00e1 vac\u00edo.

Un verificador que no falla nunca es peor que no tenerlo: da confianza falsa. Aqu\u00ed se quita cada
tipo de clave que la pantalla usa y se comprueba que lo detecta, y luego se restaura.
"""
import io, json, collections, os, subprocess, sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
VERIFICADOR = os.path.join(RAIZ, "_i18n_check.py")

#: Cada caso borra una ruta con puntos dentro del JSON. La razón de que sea una ruta y no un
#: par de claves es que `tenants` y `pricing` son el mismo bloque: pedir `("tenants", "pricing")`
## invite a borrar `tenants.tenants` en lugar de `tenants.pricing`, que es como se rompe esto.
CASOS = [
    ("el dialogo nuevo: se borra todo su bloque de traduccion", "tenants.pricing"),
    ("el boton que abre la ficha, en la fila del cliente", "tenants.actions.pricing"),
    # Las dos formas del plural, y no una: quitar solo `_one` **no** deja la clave ausente,
    # porque i18next cae a `_other` y la pantalla sigue mostrando algo. El defecto real es
    # quedarse sin ninguna forma, y es lo que este caso quita.
    ("el plural de los miembros, quitando las dos formas",
     ("tenants.members_one", "tenants.members_other")),
]


def cargar(idioma):
    R = os.path.join(RAIZ, "src", "locales", idioma, "admin.json")
    with io.open(R, encoding="utf-8", newline="") as f:
        return json.loads(f.read().replace("\r\n", "\n"), object_pairs_hook=collections.OrderedDict)


def guardar(idioma, datos):
    R = os.path.join(RAIZ, "src", "locales", idioma, "admin.json")
    with io.open(R, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(datos, ensure_ascii=False, indent=2) + "\n")


def correr():
    p = subprocess.run([sys.executable, VERIFICADOR], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=RAIZ)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


originales = {i: cargar(i) for i in ("es", "en")}
print("=" * 74)
print("DEMOSTRACION DE NO VACIEDAD - claves de i18n de la ficha de precios")
print("=" * 74)

vacios = []
try:
    for nombre, rutas in CASOS:
        for idioma in ("es", "en"):
            datos = json.loads(json.dumps(originales[idioma]),
                               object_pairs_hook=collections.OrderedDict)
            if isinstance(rutas, str):
                rutas = (rutas,)
            for ruta in rutas:
                partes = ruta.split(".")
                nodo = datos
                for parte in partes[:-1]:
                    nodo = nodo[parte]
                nodo.pop(partes[-1], None)
            guardar(idioma, datos)
        codigo, salida = correr()
        for idioma in ("es", "en"):
            guardar(idioma, originales[idioma])
        if codigo == 0:
            vacios.append(nombre)
            print("  [VACIO] %s -> el verificador NO lo detecta" % nombre)
        else:
            detectadas = [ln.strip() for ln in salida.splitlines() if "FALTAN" in ln or "->" in ln]
            print("  [BIEN ] %s" % nombre)
            for ln in detectadas[:3]:
                print("          %s" % ln)
finally:
    for idioma in ("es", "en"):
        guardar(idioma, originales[idioma])
    print("-" * 74)
    print("  traducciones restauradas al estado inicial")

print("=" * 74)
codigo, salida = correr()
if codigo != 0:
    print("  ATENCION: restaurado y aun asi falla:\n%s" % salida[-1500:])
    sys.exit(1)
print("  restaurado: %s" % salida.strip().splitlines()[-1])
if vacios:
    print("  %d caso(s) VACIO(S): %s" % (len(vacios), "; ".join(vacios)))
    sys.exit(1)
print("  %d casos reintroducidos, %d detectados" % (len(CASOS), len(CASOS)))
