# Artefactos reales de un run del motor

Estos tres ficheros son **la especificación**, no unos datos inventados para que una prueba
pase. Salen de un escaneo real y terminado, contra `https://mindguard.site` en modo `quick`, y
se han copiado sin tocar el contenido.

| Fichero            | Tamaño real | Qué aporta                                                     |
| :----------------- | :---------- | :------------------------------------------------------------- |
| `findings.sarif`   | 45.339 B    | SARIF 2.1.0 con 18 resultados: 17 `pass` y **1 `open`**.        |
| `coverage.json`    | 27.979 B    | `findings_filed: 0`, 18 superficies, **1 hueco** y sin caveats.  |
| `run.json`         | recortado   | Estado, tiempos, objetivo, tokens y coste.                      |

## Lo que este run prueba, y es lo importante

**Un escaneo sin hallazgos es un escaneo que funcionó.** El motor revisó 18 superficies, cerró
17 con `no_issue_found` o `ruled_out`, no encontró nada explotable y lo escribió con
transparencia: `findings_filed: 0`. La prueba que lo afirma es la que separa este caso de un
fallo, y por eso los artefactos son reales: un SARIF inventado con tres hallazgos hubiera
encontrado un resultado de otra cosa.

## El `kind:"open"` que casi se cuela como vulnerabilidad

De los 18 resultados, **17 son `pass` y uno es `open`**. Ese `open` no es un hallazgo: es una fila
de cobertura con `coverage_outcome: "needs_follow_up"`, la superficie `contact.php` de un dominio
vecino que el motor no pudo probar. Con el discriminador ingenuo —«todo lo que no es `pass` es un
hallazgo»— el parser devolvería **un hallazgo falso** y el panel publicaría una vulnerabilidad
que no existe. Por eso hay una prueba que lo afirma explícitamente.

## Por qué `run.json` está recortado

El original pesa 171 KB y 165 de ellos son `llm_usage.request_usage_entries` y
`llm_usage.agents`: la lista de cada llamada al modelo, con sus tokens. Eso no lo lee el parser y
no hace falta para probarlo. Se conservan todos los campos que sí se leen —`status`, los dos
tiempos, `targets_info`, `llm_usage.total_tokens`, `llm_usage.cost` y `llm_usage.providers`— con
los mismos valores, incluido el detalle de caché que hace que sumar la raíz dé un número
distinto al de los proveedores.

**No** se han copiado `strix.log` (530 KB) ni `.state/agents.db` (753 KB): son ruido, y no hacen
falta para probar el parser.