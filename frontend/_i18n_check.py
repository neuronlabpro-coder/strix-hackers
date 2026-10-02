"""Comprueba que cada `t('...')` de una pantalla exista en los dos idiomas.

R1 exige que ningun texto visible sea un literal, y la forma de cumplirlo no es escribir la clave
bien: es que la clave exista. Una clave que falta no da error de compilacion, da pantalla con
`tenants.pricing.title` escrito en lugar del titulo, y eso llega a produccion sin que nadie lo
vea hasta que un cliente mira.
"""
import io, json, re, sys

PANTALLAS = [
    "src/features/admin/TenantPricingDialog.tsx",
    "src/features/admin/AdminTenantsPage.tsx",
]


def claves_de_texto(ruta):
    """Las claves con prefijo literal, que son las que se pueden comprobar sin montar React."""
    t = io.open(ruta, encoding="utf-8").read()
    encontradas = set()
    for m in re.finditer(r"""\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]""", t):
        encontradas.add(m.group(1))
    return encontradas


SUFIJOS_PLURAL = ("_one", "_other", "_zero", "_two", "_few", "_many")


def resolver(datos, clave):
    nodo = datos
    for parte in clave.split("."):
        if not isinstance(nodo, dict) or parte not in nodo:
            return False
        nodo = nodo[parte]
    return True


def existe(datos, clave):
    """Si la clave existe, o si es un plural de i18next.

    ## Por qué hace falta el plural

    Porque `t('tenants.members', { count })` no busca `tenants.members`: i18next busca
    `tenants.members_one` y `tenants.members_other` según el número. Una clave de plural bien
    escrita no existe jamás en singular, así que un verificador que solo mirara el nombre
    exacto marcaría como faltando precisamente las claves que están bien.
    """
    if resolver(datos, clave):
        return True
    return any(resolver(datos, clave + sufijo) for sufijo in SUFIJOS_PLURAL)


faltan = []
total = 0
for idioma in ("es", "en"):
    admin = json.loads(io.open("src/locales/%s/admin.json" % idioma, encoding="utf-8").read())
    comun = json.loads(io.open("src/locales/%s/common.json" % idioma, encoding="utf-8").read())
    for pantalla in PANTALLAS:
        for clave in sorted(claves_de_texto(pantalla)):
            total += 1
            if not (existe(admin, clave) or existe(comun, clave)):
                faltan.append((idioma, pantalla, clave))

print("  %d comprobaciones de clave literal" % total)
if faltan:
    print("  %d FALTAN:" % len(faltan))
    for idioma, pantalla, clave in faltan:
        print("    [%s] %s -> %s" % (idioma, pantalla, clave))
    sys.exit(1)
print("  ninguna falta: es y en tienen todas las claves")
