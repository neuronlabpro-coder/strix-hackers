"""Busca comentarios CSS sin abrir: un `/*` perdido deja reglas muertas sin avisar."""
import io, re, sys

RUTA = "frontend/src/styles/index.css"
texto = io.open(RUTA, encoding="utf-8", newline="").read().replace("\r\n", "\n")
lineas = texto.split("\n")

en_comentario = False
sospechosos = []
for n, linea in enumerate(lineas, 1):
    if not en_comentario and re.match(r"^\s+\d+\.\s+\S", linea):
        previo = None
        for i in range(n - 2, max(0, n - 8), -1):
            if "/*" in lineas[i - 1]:
                previo = i
                break
        sospechosos.append((n, previo, linea.strip()[:64]))
    abre = linea.count("/*")
    cierra = linea.count("*/")
    if abre > cierra:
        en_comentario = True
    elif cierra > abre:
        en_comentario = False

if sospechosos:
    print("  %d linea(s) numeradas sin `/*` que las sostenga:" % len(sospechosos))
    for n, previo, linea in sospechosos:
        where = ("mismo bloque que la %d" % previo) if previo and abs(previo - n) < 8 else "bloque propio"
        print("    %5d  %-62s  %s" % (n, linea, where))
    sys.exit(1)
print("  ningun comentario sin abrir")
