"""Localiza las cabeceras de comentario que se quedaron sin su párrafo.

Un `## Por qué` vacío es la firma del heredoc de PowerShell: el bloque se escribió muchas veces y
solo sobrevivió la cabecera. Cada uno es una explicación que se perdió, así que la lista es el
inventario de lo que hay que reescribir.
"""

import io

p = 'frontend/src/styles/index.css'
lineas = io.open(p, encoding='utf-8').read().split('\n')


def es_cabecera(texto: str) -> bool:
    s = texto.strip()
    return s.startswith('* ##') or s.startswith('##')


def es_vacia(i: int) -> bool:
    """La cabecera de la línea `i` no tiene párrafo si lo que viene detrás es otra cabecera
    o el cierre del comentario."""
    j = i + 1
    while j < len(lineas):
        s = lineas[j].strip()
        if s in ('', '*'):
            j += 1
            continue
        return s == '*/' or es_cabecera(s)
    return True


vacias = [i for i, l in enumerate(lineas) if es_cabecera(l) and es_vacia(i)]
print('  cabeceras sin parrafo: %d' % len(vacias))

# Se agrupan en bloques contiguos (separadas por menos de 6 lineas).
bloques = []
for i in vacias:
    if bloques and i - bloques[-1][1] <= 6:
        bloques[-1][1] = i
    else:
        bloques.append([i, i])
for a, b in bloques:
    print('    lineas %d-%d   (%d cabeceras)' % (a + 1, b + 1, b - a + 1))
