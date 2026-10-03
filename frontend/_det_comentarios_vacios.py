import io

t = io.open('frontend/src/styles/index.css', encoding='utf-8').read()
lineas = t.split('\n')

# Una cabecera "## Por que" vacia es una linea que termina con la cabecera y no lleva texto
# despues: en un docstring bien escrito cada "## Por que" tiene su parrafo debajo.
vacias = []
for i, l in enumerate(lineas):
    s = l.strip().lstrip('/*').strip()
    if s.startswith('## ') and (len(s) < 6 or s.rstrip(':').endswith('Por qu')):
        vacias.append(i + 1)

print('  cabeceras "## Por que" SIN parrafo debajo: %d' % len(vacias))
if vacias:
    print('  de la linea %d a la %d' % (vacias[0], vacias[-1]))
    print('  muestra: %r' % lineas[vacias[0] - 1][:70])
