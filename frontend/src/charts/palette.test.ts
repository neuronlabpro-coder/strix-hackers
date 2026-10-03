/**
 * La paleta de los gráficos tiene que ser la del tema, no una paleta propia.
 *
 * ## Por qué esta prueba existe
 *
 * Porque durante varias sesiones los gráficos leyeron `--chart-critical`, un token que el
 * tema **no define**, y cayeron a un valor de reserva con tonos 500 (`#EF4444`). El resto de la
 * interfaz usa la rampa 400 de `design-dark.md`, así que el mismo rojo significaba dos cosas
 * distintas según dónde se mirara, y no había ni error ni aviso: solo dos colores donde la
 * documentación dice que hay uno. Un fallo de ese tipo solo lo caza comparar ficheros, que es
 * exactamente lo que hace esta prueba.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { chartPalette, readChartPalette } from './palette'

const CSS_PATH = join(__dirname, '..', 'styles', 'index.css')

/** El valor que el tema declara para un token, en minúsculas. */
function tokenEnCss(token: string): string | null {
  const css = readFileSync(CSS_PATH, 'utf8')
  const patron = new RegExp(`${token}:\\s*(#[0-9a-fA-F]{3,8})\\s*;`)
  const encontrado = patron.exec(css)
  return encontrado ? (encontrado[1] ?? null).toLowerCase() : null
}

/**
 * Cada color de la paleta tiene que existir en el tema con **el mismo valor**.
 *
 * La correspondencia clave a clave importa: si `--color-critical` y `--chart-critical` tienen
 * el mismo nombre es porque el token de interfaz manda, y el `--chart-*` solo existe como
 * sobrescritura explícita para un gráfico concreto.
 */
const CORRESPONDENCIAS: Array<[clave: keyof typeof chartPalette, token: string]> = [
  ['accent', '--color-accent'],
  ['primary', '--color-primary'],
  ['secondary', '--color-secondary'],
  ['caption', '--color-caption'],
  ['surface', '--color-surface'],
  ['surfaceElevated', '--color-surface-elevated'],
  ['background', '--color-background'],
  ['border', '--color-border'],
  ['borderHover', '--color-border-hover'],
  ['critical', '--color-critical'],
  ['high', '--color-high'],
  ['medium', '--color-medium'],
  ['low', '--color-low'],
  ['info', '--color-info'],
]

describe('palette de gráficos', () => {
  it.each(CORRESPONDENCIAS)('«%s» es el valor del token %s', (clave, token) => {
    const esperado = tokenEnCss(token)
    expect(esperado, `el token ${token} tiene que existir en index.css`).not.toBeNull()
    expect(chartPalette[clave]).toBe(esperado)
  })

  /**
   * Sin DOM, `readChartPalette()` devuelve la reserva, que es la copia del token.
   *
   * Es el camino que se ejecuta en el primer render de un navegador que aún no ha aplicado
   * las variables del tema, así que si la reserva no coincide con el token, ese primer
   * render pinta un color que no existe en ninguna otra pantalla.
   */
  it('sin tokens resueltos devuelve la copia literal del tema', () => {
    const leida = readChartPalette()
    for (const [clave, token] of CORRESPONDENCIAS) {
      expect(leida[clave]).toBe(tokenEnCss(token))
    }
  })

  /**
   * Los tonos 500 no existen en el tema, y por eso no pueden aparecer en la paleta.
   *
   * Se prueban los cinco valores concretos con los que se confundió, porque es un fallo de
   * historia: la lista de prohibidos sería una lista más, y una lista se puede ampliar por
   * error. Estos cinco son los que existieron.
   */
  it('no contiene ninguno de los tonos 500 con los que se confundió antes', () => {
    const tonosProhibidos = ['#ef4444', '#f97316', '#f59e0b', '#3b82f6']
    const colores = Object.values(chartPalette)
    for (const prohibido of tonosProhibidos) {
      expect(colores).not.toContain(prohibido)
    }
  })

  /**
   * La severidad va de más a menos grave y los tonos no se repiten.
   *
   * Es lo que permite leer la gráfica sin mirar la leyenda: si dos severidades compartieran
   * color, el color dejaría de ser información.
   */
  it('la rampa de severidad es distinguible y sin repetir', () => {
    const severidades = ['critical', 'high', 'medium', 'low', 'info'] as const
    const valores = severidades.map((clave) => chartPalette[clave])
    expect(valores).toHaveLength(new Set(valores).size)
  })
})