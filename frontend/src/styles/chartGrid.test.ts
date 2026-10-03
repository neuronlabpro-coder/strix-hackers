/**
 * La rejilla de gráficos tiene que contar bien sus hijos.
 *
 * ## Qué defecto fija
 *
 * El que se vio en la pantalla: en `/containers` y `/networks` hay **tres** gráficos y la
 * rejilla era de **dos** columnas, así que el tercero bajaba a una segunda fila dejando una
 * celda vacía, y los escaneos —que es lo que el operador ha venido a mirar— quedaban dos alturas
 * más abajo.
 *
 * Es el mismo tipo de fallo que el del buscador que ya vigila `searchField.test.ts`: no es un
 * error de píxeles, es un **desajuste entre lo que la rejilla cuenta y lo que sus hijos
 * necesitan**, y no se ve mirando el CSS sino contando las etiquetas.
 *
 * ## Por qué se lee el fuente y no se monta nada
 *
 * Por el mismo motivo que en `searchField.test.ts` y `checkboxSize.test.ts`: montar un árbol
 * para medir una rejilla exigiría `jsdom` y una prueba que nadie va a mantener. El invariante
 * es **estructural** —cuántas columnas declara la rejilla, cuántos gráficos le meten y dónde
 * corta—, así que se lee el fuente, que además corre sin navegador y en un segundo.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

// `src/styles/` -> `src/`. Se usa la misma forma que en `searchField.test.ts`, que está al lado
// y lee el mismo fichero: `new URL('..', import.meta.url)` sobre el módulo da el directorio
// padre sin depender de la profundidad con `../..`, que es lo que se rompió al escribir esto
// la primera vez.
const RAIZ = fileURLToPath(new URL('..', import.meta.url))
const CSS = readFileSync(join(RAIZ, 'styles', 'index.css'), 'utf8').replace(/\r\n/g, '\n')

/**
 * Las pantallas que usan `chart-grid`, con cuántos gráficos le meten.
 *
 * Si se añade una pantalla con cuatro gráficos, esta lista tiene que crecer y la regla deja de
 * servir tal cual: con cuatro, tres columnas dejan un hueco igual de grande que el que se
 * arregla aquí.
 */
const PANTALLAS: ReadonlyArray<readonly [string, number]> = [
  ['features/agents/ContainersPage.tsx', 3],
  ['features/agents/NetworksPage.tsx', 3],
  ['features/dashboard/DashboardPage.tsx', 3],
]

/**
 * Las pantallas que usan la variante de dos columnas.
 *
 * Van aparte porque el invariante es distinto: aquí el problema no es que sobre una celda
 * vacía, sino que la etiqueta de la fila —que es una **palabra larga**— se recorta en el ancho
 * de portátil. Con tres columnas son 266 px por tarjeta y «Remediación propuesta» no entra; con
 * dos son 448 y entra con el separador de miles al lado. Por eso la lista lleva su propio
 * número esperado y su propia regla.
 */
const PANTALLAS_DOS: ReadonlyArray<readonly [string, number]> = [
  ['features/issues/GraficosIssues.tsx', 2],
]

/** La declaración de columnas de una regla, contando solo la del nivel superior. */
function columnasDe(selector: string): number | null {
  const regla = new RegExp(
    `^${selector.replace('.', '\\.')} \\{([\\s\\S]*?)^\\}`,
    'm',
  ).exec(CSS)
  if (!regla) return null
  const declaracion = /grid-template-columns:\s*repeat\((\d+)/.exec(regla[1])
  return declaracion ? Number(declaracion[1]) : null
}

describe('La rejilla de gráficos cuenta bien sus hijos', () => {
  it('la rejilla base declara tres columnas', () => {
    // Tres, no dos: con dos y tres hijos, el tercero baja y deja una celda vacía.
    expect(columnasDe('.chart-grid')).toBe(3)
  })

  it('ninguna pantalla le mete un número de gráficos que no cuadre con tres', () => {
    for (const [fichero, esperados] of PANTALLAS) {
      const fuente = readFileSync(join(RAIZ, fichero), 'utf-8')
      const rejilla = fuente.indexOf('className="chart-grid"')
      expect(rejilla, `${fichero} ya no usa chart-grid: revisa esta lista`).toBeGreaterThan(-1)

      // Se cuentan los **`<EChart>`**, no los `<section className="content-card">`. Contando
      // secciones, esta prueba falló sola cuando el dashboard añadió una tarjeta más de contenido
      // —el widget de repositorios, que no es un gráfico— junto a los tres gráficos: contaba
      // cuatro celdas donde hay tres gráficas y daba por roto un layout que estaba bien. El
      // invariante que importa es cuántas gráficas hay, y se cuenta lo que las dibuja.
      const ventana = fuente.slice(rejilla, rejilla + 8000)
      const hijos = (ventana.match(/<EChart\b/g) ?? []).length
      expect(hijos, `${fichero} mete ${hijos} gráficos y la rejilla es de 3 columnas`).toBe(
        esperados,
      )
    }
  })

  it('la variante de dos columnas declara dos columnas', () => {
    expect(columnasDe('.chart-grid-two')).toBe(2)
  })

  it('ninguna pantalla de la variante de dos le mete un número que no cuadre', () => {
    for (const [fichero, esperados] of PANTALLAS_DOS) {
      const fuente = readFileSync(join(RAIZ, fichero), 'utf-8')
      const rejilla = fuente.indexOf('className="chart-grid chart-grid-two"')
      expect(rejilla, `${fichero} ya no usa chart-grid-two: revisa esta lista`).toBeGreaterThan(-1)

      // Aquí sí se cuentan las `<section>`, y no por capricho: el fichero de los gráficos de
      // issues **no** contiene ninguna otra tarjeta de contenido, así que las dos cuentas
      // coinciden. Si algún día ese fichero gana una tabla, la cuenta tiene que cambiar a
      // `<EChart>` como en la prueba de arriba.
      const ventana = fuente.slice(rejilla, rejilla + 4000)
      const hijos = (ventana.match(/<section className="content-card"/g) ?? []).length
      expect(hijos, `${fichero} mete ${hijos} gráficos y la rejilla es de 2 columnas`).toBe(
        esperados,
      )
    }
  })

  it('la variante de dos columnas baja a una en el mismo punto que la de tres', () => {
    // Si solo baja `.chart-grid`, entonces a 1000 px las barras de issues se quedan en dos
    // columnas de 300 px —donde la etiqueta larga se recorta— mientras el resto del panel ya
    // es de una. Compartir el corte es lo que evita que las dos rejillas digan cosas distintas
    // sobre el mismo monitor.
    const corte = /@media \(max-width: (\d+)px\) \{\s*\.metric-grid[\s\S]*?\.chart-grid,\s*\.chart-grid-two \{\s*grid-template-columns: 1fr;/.exec(
      CSS,
    )
    expect(
      corte,
      'el media query tiene que bajar las dos variantes juntas',
    ).not.toBeNull()
    expect(Number(corte![1])).toBeLessThanOrEqual(1150)
  })

  it('el corte a una columna está por debajo del ancho en que el gráfico deja de caber', () => {
    // Con 256 de barra lateral y 64 de relleno, 1150 de viewport dejan 830 de rejilla, que
    // repartidos en tres son 255 px por gráfico. Un gráfico con eje de fechas se empieza a
    // recortar por debajo de eso, así que el corte tiene que estar antes, no después.
    const corte = /@media \(max-width: (\d+)px\) \{\s*\.metric-grid[\s\S]*?\.chart-grid,\s*\.chart-grid-two \{\s*grid-template-columns: 1fr;/.exec(
      CSS,
    )
    expect(corte, 'el media query que baja la rejilla a una columna ha desaparecido').not.toBeNull()
    expect(Number(corte![1])).toBeLessThanOrEqual(1150)
  })
})
