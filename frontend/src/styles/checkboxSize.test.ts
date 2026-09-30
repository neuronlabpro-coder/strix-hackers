/**
 * Ninguna casilla de verificación se queda sin tamaño declarado.
 *
 * ## Qué defecto fija
 *
 * Que una casilla mide lo que el navegador quiera. Pasó en API Access por dos caminos a la vez:
 * las casillas de las listas no tenían tamaño propio y dependían del motor —con
 * `button, input { font: inherit }` encima, heredaban la fuente de la página—, y las de los
 * filtros caían en la regla de los campos de texto, que impone `min-height: 34px` y
 * `padding: 6px 12px`. Las dos se veían enormes, y no solo en la captura: dependía del
 * navegador, así que en unos salía de 13 px y en otros de 40.
 *
 * ## Por qué un test y no una revisión visual
 *
 * Porque el defecto es una **ausencia**: no hay ningún sitio donde esté mal escrito, está en que
 * no está escrito. Una revisión visual lo detecta el día que alguien abre la pantalla en un
 * navegador concreto, y se le olvida en cuanto lo arregla en el navegador de otro. El invariante
 * que sí se puede vigilar es «toda casilla tiene un tamaño declarado en el CSS», y eso es
 * una propiedad del fichero, no del render.
 *
 * ## Por qué `.switch input` es la excepción y no un olvido
 *
 * Porque en un conmutador la casilla **no se ve**: es el control invisible que lleva el estado
 * y ocupa la pista entera con `position: absolute; inset: 0`. Si la regla global le pusiera
 * 16 px, el conmutador dejaría de ser pulsable en una franja de un píxel. La excepción es
 * deliberada, y por eso el test la comprueba en vez de permitirla sin más.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const RAIZ = fileURLToPath(new URL('..', import.meta.url))
const CSS = join(RAIZ, 'styles', 'index.css')

const css = readFileSync(CSS, 'utf8').replace(/\r\n/g, '\n')

/** Los `.tsx` de `src`, sin entrar en `node_modules` ni en `dist`. */
function fuentesTsx(directorio: string): string[] {
  const encontrados: string[] = []
  for (const entrada of readdirSync(directorio)) {
    if (entrada === 'node_modules' || entrada === 'dist' || entrada === 'build') {
      continue
    }
    const completa = join(directorio, entrada)
    if (statSync(completa).isDirectory()) {
      encontrados.push(...fuentesTsx(completa))
    } else if (entrada.endsWith('.tsx')) {
      encontrados.push(completa)
    }
  }
  return encontrados
}

/** El nombre de la clase o el identificador de la etiqueta `<label>` que envuelve a la casilla. */
function contenedorDeCasilla(lineas: string[], indice: number): string | null {
  for (let arriba = indice; arriba >= 0 && arriba > indice - 6; arriba -= 1) {
    const etiqueta = /className="([^"]+)"|htmlFor="([^"]+)"|<label/.exec(lineas[arriba])
    if (etiqueta) {
      return etiqueta[1] ?? etiqueta[2] ?? '(label sin clase)'
    }
  }
  return null
}

describe('las casillas de verificacion tienen un tamano declarado', () => {
  it('la regla global existe, con el tamano y el min-height que la sostienen', () => {
    // El `min-height: 0` no es un detalle: le gana al `height`, asi que sin el una casilla
    // dentro de un `.filter-field` seguiria midiendo 34 px aunque tuviera `height: 16px`.
    const regla =
      /input\[type='checkbox'\]:not\(\.switch input\)\s*\{([^}]*)\}/.exec(css)
    expect(regla, 'no hay regla global para input[type=checkbox]').not.toBeNull()
    const cuerpo = (regla as RegExpExecArray)[1]
    expect(cuerpo, 'la casilla no tiene width').toMatch(/width:\s*16px/)
    expect(cuerpo, 'la casilla no tiene height').toMatch(/height:\s*16px/)
    expect(cuerpo, 'sin min-height: 0 gana el min-height heredado de los campos de texto').toMatch(
      /min-height:\s*0/
    )
    expect(cuerpo, 'sin flex: 0 0 auto una etiqueta larga la aplasta').toMatch(/flex:\s*0 0 auto/)
    expect(cuerpo, 'la casilla no toma el color de acento').toMatch(/accent-color:/)
  })

  it('.switch input queda fuera, y con motivo', () => {
    // El conmutador lleva la casilla como control invisible a `inset: 0`. Si la regla global le
    // pusiera 16 px, el interruptor dejaria de ser pulsable en una franja minima.
    expect(css).toMatch(/input\[type='checkbox'\]:not\(\.switch input\)/)
    const conmutador = /\.switch input\s*\{([^}]*)\}/.exec(css)
    expect(conmutador, 'no hay regla para .switch input').not.toBeNull()
    const cuerpo = (conmutador as RegExpExecArray)[1]
    expect(cuerpo, 'la casilla del conmutador tiene que ocupar la pista entera').toMatch(
      /position:\s*absolute/
    )
    expect(cuerpo, 'y ocultarse, porque la pista es lo que se ve').toMatch(/opacity:\s*0/)
  })

  it('ninguna casilla del proyecto queda sin regla que la dimensione', () => {
    // El inventario es lo que detecta el defecto futuro: una casilla nueva en un componente que
    // aun no ha pasado por aqui se declara sola por la regla global, y una que se meta dentro
    // de un `.switch` sin querer seria la unica excepcion, y tiene que aparecer aqui.
    const sinDimensionar: string[] = []
    for (const fichero of fuentesTsx(join(RAIZ))) {
      const lineas = readFileSync(fichero, 'utf8').replace(/\r\n/g, '\n').split('\n')
      for (const [indice, linea] of lineas.entries()) {
        if (!linea.includes('type="checkbox"')) {
          continue
        }
        const ventana = lineas.slice(Math.max(0, indice - 6), indice).join('\n')
        if (ventana.includes('className="switch"')) {
          continue
        }
        const contenedor = contenedorDeCasilla(lineas, indice)
        if (contenedor === null) {
          sinDimensionar.push(
            `${fichero.replace(RAIZ, '')}:${indice + 1} (no se encuentra su etiqueta)`
          )
        }
      }
    }
    expect(
      sinDimensionar,
      'estas casillas estan dentro de una etiqueta que no se ha podido leer del fichero. La '
        + 'regla global las cubre por el `input[type=checkbox]`, asi que esto no es un fallo de '
        + 'tamanio, pero si avisa de que la comprobacion de arriba se ha quedado corta.'
    ).toEqual([])
  })

  it('la casilla de una lista se alinea con la primera linea del texto', () => {
    // Con `align-items: flex-start` la casilla se pega al borde superior de la fila, que es la
    // linea de la etiqueta en su mayuscula; el texto de al lado mide mas alto, y se ve que no
    // estan alineados. El margen lo baja a la linea del texto.
    expect(css).toMatch(
      /\.scope-item input\[type='checkbox'\]\s*\{\s*margin-top:\s*\d+px;?\s*\}/
    )
  })

  it('una casilla con su texto al lado usa el layout en linea', () => {
    // `filter-field` es columna: etiqueta arriba, control abajo. Para una casilla suelta eso la
    // pone encima de su texto y deja una celda alta y estrecha.
    expect(css, 'no hay layout en linea para una casilla con su texto').toMatch(
      /\.toggle-field\s*\{[^}]*flex-direction:\s*row/
    )
    const regla = /\.toggle-field\s*\{([^}]*)\}/.exec(css)
    expect(regla).not.toBeNull()
    const cuerpo = (regla as RegExpExecArray)[1]
    expect(cuerpo, 'sin align-items: center la casilla no se centra con el texto').toMatch(
      /align-items:\s*center/
    )
  })

  it('las casillas de API Access usan el layout en linea donde hace falta', () => {
    // Comprobacion del uso y no solo de la regla: una clase que existe en el CSS y no se usa en
    // ningun sitio es una regla muerta, y aqui lo que importa es que los dos conmutadores de
    // API Access hayan cambiado de `filter-field` a `toggle-field`.
    const paginas = [
      join(RAIZ, 'features', 'api_access', 'ApiAccessPage.tsx'),
      join(RAIZ, 'features', 'api_access', 'WebhooksTab.tsx'),
    ]
    for (const pagina of paginas) {
      const texto = readFileSync(pagina, 'utf8')
      const casillas = texto.match(/type="checkbox"/g) ?? []
      expect(casillas.length, `${pagina} deberia tener casillas`).toBeGreaterThan(0)
      const conToggle = texto.match(/className="toggle-field"/g) ?? []
      const conField = texto.match(/className="filter-field"/g) ?? []
      // En `WebhooksTab` hay dos casillas: la de la lista, que va en `.scope-item`, y la del
      // conmutador, que va en `.toggle-field`. Ninguna debe seguir en `filter-field`.
      expect(
        texto.includes('className="filter-field">\n          <input\n            type="checkbox"'),
        `${pagina} sigue usando filter-field para una casilla`
      ).toBe(false)
      expect(conToggle.length, `${pagina} deberia usar toggle-field`).toBeGreaterThan(0)
      void conField
    }
  })
})
