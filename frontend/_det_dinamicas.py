import io, os, json

prefijos = set()
for raiz, _d, fs in os.walk("src"):
    for n in fs:
        if not n.endswith((".tsx", ".ts")):
            continue
        txt = io.open(os.path.join(raiz, n), encoding="utf-8", errors="replace").read()
        # Se recogen solo las cadenas que empiezan por tenants.pricing y que llevan un comodin
        # al final: son las que el codigo construye en runtime y que ningun verificador literal ve.
        for m in txt.split('"'):
            if m.startswith("tenants.pricing.") and m.endswith("."):
                prefijos.add(m)
            elif m.startswith("tenants.pricing.") and "$" in m:
                prefijos.add(m)

print("cadenas dinamicas de tenants.pricing que el codigo construye:")
for p in sorted(prefijos):
    print("   ", p)

print()
faltan = []
for lang in ("es", "en"):
    d = json.load(io.open("src/locales/%s/admin.json" % lang, encoding="utf-8"))
    for pref in sorted(prefijos):
        base = pref.split("$")[0].rstrip(".")
        partes = base.split(".")
        act = d
        ok = True
        for s in partes:
            if isinstance(act, dict) and s in act:
                act = act[s]
            else:
                ok = False
                break
        if not ok:
            faltan.append("[%s] falta el bloque %s" % (lang, base))

if faltan:
    for f in faltan:
        print("  " + f)
else:
    print("  todos los bloques de las claves dinamicas existen en es y en")
