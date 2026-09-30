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

      // Se cuentan los `<section className="content-card"` hasta el cierre del div de la rejilla.
      // La ventana está acotada a propósito: si se cuenta el fichero entero, cada `content-card`
      // de la tabla de debajo contaría como si fuera un gráfico.
      const ventana = fuente.slice(rejilla, rejilla + 4000)
      const hijos = (ventana.match(/<section className="content-card"/g) ?? []).length
      expect(hijos, `${fichero} mete ${hijos} gráficos y la rejilla es de 3 columnas`).toBe(
        esperados,
      )
    }
  })

  it('el corte a una columna está por debajo del ancho en que el gráfico deja de caber', () => {
    // Con 256 de barra lateral y 64 de relleno, 1150 de viewport dejan 830 de rejilla, que
    // repartidos en tres son 255 px por gráfico. Un gráfico con eje de fechas se empieza a
    // recortar por debajo de eso, así que el corte tiene que estar antes, no después.
    const corte = /@media \(max-width: (\d+)px\) \{\s*\.metric-grid[\s\S]*?\.chart-grid \{\s*grid-template-columns: 1fr;/.exec(
      CSS,
    )
    expect(corte, 'el media query que baja la rejilla a una columna ha desaparecido').not.toBeNull()
    expect(Number(corte![1])).toBeLessThanOrEqual(1150)
  })
})
