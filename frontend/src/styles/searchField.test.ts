/**
 * Geometría del buscador: una línea, con un ancho que no se come la barra.
 *
 * ## Por qué esto es un test sobre el CSS y no sobre el DOM
 *
 * Montar el árbol para medir una caja exigiría `jsdom` y un provider de sesión, y una prueba
 * que necesita catorce andamiajes para comprobar un `flex` es una prueba que nadie va a
 * mantener. Los invariantes que importan son **estructurales** —qué declara cada regla y en qué
 * contexto—, así que se lee el fichero. Es además el precedente que ya hay en `App.test.ts`.
 *
 * ## Qué défauts fija
 *
 * Los tres que se han dado, todos por la misma causa de fondo: el buscador, como cualquier
 * campo flex, hereda su tamaño de un contenedor con el que no se había contado.
 *
 * 1. `width: 100%` en un hijo de una fila flex significa el ancho de toda la fila, así que el
 *    buscador ocupaba la barra entera y los desplegables quedaban al extremo opuesto. En
 *    Pentests e Issues era peor: el envoltorio llevaba `flex: 1 1 260px`, que **crece** con el
 *    espacio libre.
 * 2. Un `flex: 0 1 300px` puesto sobre el `.search-field` **anidado** dentro de un
 *    `.filter-field` se convierte en una **altura** de 300 px, porque el contenedor de columna
 *    reparte el eje vertical. Por eso los dos contextos se declaran por separado.
 * 3. El buscador estaba en el grupo de los desplegables, que lleva `padding: 6px 12px`. Con
 *    `box-sizing: border-box` y un `input` de altura fija dentro, el campo se salía de su
 *    propia caja: el relleno vertical se lo robaba al input.
 */

import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const RAIZ = fileURLToPath(new URL('..', import.meta.url))
const DIR_CSS = join(RAIZ, 'styles')

/**
 * Se concatenan **todas** las hojas, en el orden en que las importa `main.tsx`.
 *
 * ## Por qué esto estaba mal y ahora no
 *
 * ## Por qué leer solo una hoja daba verde con el tema roto
 *
 * ## Por qué
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 *
 * ## Por qu00e9
 */
const ORDEN_HOJAS = [
  'index.css',
  'settings.css',
  'billing.css',
  'support.css',
  'assets.css',
  'chat.css',
  'supplyChain.css',
  'knowledge.css',
  'console.css',
] as const

const cssBruto = ORDEN_HOJAS.map((nombre) => {
  const ruta = join(DIR_CSS, nombre)
  return readFileSync(ruta, 'utf8').replace(/\r\n/g, '\n')
}).join('\n')

/** Los comentarios se vacían dejando las saltos de línea, para no desfasar los números. */
function quitarComentarios(texto: string): string {
  return texto.replace(/\/\*[\s\S]*?\*\//g, (bloque) => bloque.replace(/[^\n]/g, ' '))
}

const css = quitarComentarios(cssBruto)

interface Regla {
  selectores: string[]
  cuerpo: string
  linea: number
}

/**
 * Solo las reglas de **nivel superior**.
 *
 * Un parser ingenuo con `[^{}]+\{[^{}]*\}` no entiende la anidación: al toparse con un
 * `@media` se come su llave de apertura y luego trata su contenido como si estuviera al nivel
 * superior, que es exactamente como se pasó por alto la regla de la altura del buscador. Aquí
 * se cuenta la profundidad con las llaves y se descartan las reglas dentro de bloques.
 */
function reglasDeNivelSuperior(hoja: string): Regla[] {
  const reglas: Regla[] = []
  let profundidad = 0
  let inicio = 0
  let selector = ''

  for (let i = 0; i < hoja.length; i += 1) {
    const caracter = hoja[i]
    if (caracter === '{') {
      if (profundidad === 0) {
        selector = hoja.slice(inicio, i)
        inicio = i + 1
      }
      profundidad += 1
    } else if (caracter === '}') {
      profundidad -= 1
      if (profundidad === 0) {
        reglas.push({
          selectores: selector
            .split(',')
            .map((parte) => parte.trim())
            .filter((parte) => parte.length > 0),
          cuerpo: hoja.slice(inicio, i),
          linea: hoja.slice(0, i).split('\n').length,
        })
        // El cursor avanza por detras de la llave de cierre. Sin esto, el selector de la
        // siguiente regla se come el cuerpo de esta, y ninguna busqueda por selector
        // encuentra nada: es un fallo silencioso que deja el test en rojo sin explicar por que.
        inicio = i + 1
      }
    } else if (caracter === ';' && profundidad === 0) {
      inicio = i + 1
    }
  }
  return reglas
}

const reglas = reglasDeNivelSuperior(css)

/** Todas las declaraciones de una regla, en orden, como pares propiedad-valor. */
function declaraciones(cuerpo: string): Array<{ propiedad: string; valor: string }> {
  return cuerpo
    .split(';')
    .map((trozo) => {
      const separador = trozo.indexOf(':')
      if (separador === -1) {
        return null
      }
      return {
        propiedad: trozo.slice(0, separador).trim(),
        valor: trozo.slice(separador + 1).trim(),
      }
    })
    .filter((d): d is { propiedad: string; valor: string } => d !== null && d.propiedad !== '')
}

/**
 * El valor **ganador** de una propiedad para un selector exacto de nivel superior.
 *
 * Gana el último, no el primero: con la misma especificidad decide el orden del fichero, y una
 * regla posterior pisa a la anterior. Devolver la primera daría el valor de una regla anulada,
 * que es como se daría por bueno un `min-height: 40px` que el navegador ya no aplica.
 */
function valorDe(selector: string, propiedad: string): string | undefined {
  let ganador: string | undefined
  for (const regla of reglas) {
    if (!regla.selectores.includes(selector)) {
      continue
    }
    for (const declaracion of declaraciones(regla.cuerpo)) {
      if (declaracion.propiedad === propiedad) {
        ganador = declaracion.valor
      }
    }
  }
  return ganador
}

function reglasQueDeclaran(selector: string, propiedad: string): number[] {
  return reglas
    .filter((regla) => regla.selectores.includes(selector))
    .filter((regla) => declaraciones(regla.cuerpo).some((d) => d.propiedad === propiedad))
    .map((regla) => regla.linea)
}

describe('el buscador es una línea y no ocupa la barra', () => {
  it('las llaves del CSS están equilibradas, así que las reglas de arriba son todas reales', () => {
    // Guarda contra un bloque mal cerrado, que haria que el parser de este test - y el del
    // navegador - leyeran selectores a partir de la regla siguiente.
    const abiertas = (css.match(/\{/g) ?? []).length
    const cerradas = (css.match(/\}/g) ?? []).length
    expect(abiertas, 'el CSS tiene llaves sin cerrar').toBe(cerradas)
  })

  it('la caja base no se estira al ancho de su contenedor', () => {
    // El defecto: `width: 100%` en la caja base, que en una fila flex es el ancho de la fila.
    expect(valorDe('.search-field', 'width'), '.search-field no debe llevar width propio').toBeUndefined()
    expect(valorDe('.search-field', 'max-width')).toBeUndefined()
  })

  it('la caja base puede ceder sitio: min-width 0', () => {
    // Sin esto el `min-width: auto` de un elemento flex impide bajar del tamaño del contenido,
    // y un placeholder largo deja el buscador por encima de su ancho para siempre.
    expect(valorDe('.search-field', 'min-width')).toBe('0')
  })

  it('en la barra, el buscador no crece y sí cede', () => {
    const flex = valorDe('.filter-bar > .search-field', 'flex')
    expect(flex, 'falta la regla que dimensiona el buscador dentro de la barra').toBeDefined()
    const factorCrecimiento = (flex as string).trim().split(/\s+/)[0]
    expect(
      factorCrecimiento,
      `el buscador de la barra declara flex: ${flex}. El primer numero es el crecimiento, y ahi `
        + 'tiene que ser 0: con un valor positivo el buscador absorbe el espacio libre de la '
        + 'barra y empuja los desplegables al extremo opuesto.',
    ).toBe('0')
    expect(
      (flex as string).trim().split(/\s+/)[1],
      `el buscador de la barra declara flex: ${flex}, sin factor de encogimiento. Sin el 1 no `
        + 'cede: en una ventana estrecha no se reduce y fuerza la barra a dar la vuelta.',
    ).toBe('1')
  })

  it('en la barra de herramientas tambien es una linea', () => {
    const flex = valorDe('.toolbar > .search-field', 'flex')
    expect(flex).toBeDefined()
    expect((flex as string).trim().split(/\s+/)[0]).toBe('0')
  })

  it('dentro de un .filter-field el ancho lo decide el envoltorio, y el buscador no hereda el flex de fila', () => {
    // Un `flex: 0 1 300px` en un hijo de una **columna** reparte el eje vertical, asi que
    // seria una altura de 300 px. Por eso aqui el buscador va con `flex: none`.
    const flex = valorDe('.filter-bar .filter-field-search > .search-field', 'flex')
    expect(
      flex,
      'el buscador anidado debe anular el flex de fila, o mide 300 px de alto',
    ).toBeDefined()
    expect(['none', '0 0 auto']).toContain(flex)
    expect(valorDe('.filter-bar .filter-field-search > .search-field', 'width')).toBe('100%')
  })

  it('el envoltorio con etiqueta es lo que no crece', () => {
    const flex = valorDe('.filter-bar .filter-field-search', 'flex')
    expect(flex).toBeDefined()
    expect((flex as string).trim().split(/\s+/)[0]).toBe('0')
  })
})

describe('el buscador no se sale de su propia caja', () => {
  it('la caja no tiene relleno vertical', () => {
    // El defecto: estaba en el grupo de los desplegables, que lleva `padding: 6px 12px`. Con
    // `box-sizing: border-box` y un input de altura fija dentro, el relleno se lo comia al
    // campo y se salia de la caja.
    const padding = valorDe('.search-field', 'padding') ?? ''
    const vertical = padding.trim().split(/\s+/).filter((parte) => /^-?\d/.test(parte))
    // Un solo valor es solo horizontal; dos valores son vertical-horizontal; tres o cuatro
    // llevan vertical. Con dos, el vertical es el primero.
    const hayVertical = vertical.length >= 2 ? vertical[0] : vertical.length === 3 || vertical.length === 4 ? vertical[0] : '0'
    expect(
      hayVertical,
      `.search-field declara padding: ${padding || '(sin padding)'}. El relleno vertical le `
        + 'roba altura al input de dentro, que es de altura fija, y el campo se sale de la caja.',
    ).toBe('0')
  })

  it('el input toma la altura de la caja en vez de fijarla', () => {
    expect(valorDe('.search-field input', 'height')).toBe('100%')
    expect(valorDe('.search-field input', 'min-height')).toBe('0')
    expect(valorDe('.search-field input', 'padding')).toBe('0')
  })

  it('la caja y los desplegables miden lo mismo', () => {
    // Una linea, y la misma linea: si el buscador mide 36 y los desplegables 34, la barra tiene
    // dos alturas y el ojo la lee como que algo va mal.
    const alturaBuscador = valorDe('.search-field', 'height')
    const alturaDesplegable = valorDe('.filter-field select', 'height')
    expect(alturaBuscador).toBeDefined()
    expect(alturaDesplegable).toBeDefined()
    expect(alturaBuscador).toBe(alturaDesplegable)
  })
})

describe('cada pagina que usa el buscador tiene el contexto que el CSS dimensiona', () => {
  const INVENTARIO: Array<{ fichero: string; conEtiquetaVisible: boolean; contenedor: string }> = [
    { fichero: 'cve/CvePage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: false },
    { fichero: 'knowledge/KnowledgeCatalogView.tsx', contenedor: 'filter-bar', conEtiquetaVisible: false },
    { fichero: 'supply_chain/SupplyChainPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: false },
    { fichero: 'repositories/RepositoriesPage.tsx', contenedor: 'toolbar', conEtiquetaVisible: false },
    { fichero: 'pentests/PentestsPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    { fichero: 'issues/IssuesPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    // Las dos pantallas de escaneo y la de la consola. Las tres llevan etiqueta visible, que es
    // el mismo contexto que Pentests e Issues, asi que **no necesitan ninguna regla nueva**: se
    // anaden aqui para que este test siga siendo la lista completa de quien usa el buscador.
    { fichero: 'agents/ContainersPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    { fichero: 'agents/NetworksPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    { fichero: 'admin/AdminAgentsPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    // PR Reviews tambien lleva etiqueta visible y esta en `.filter-bar`, asi que la dimensiona
    // la misma regla que Pentests e Issues. Se declara para que la lista siga siendo completa.
    { fichero: 'prReviews/PrReviewsPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    // Dominios y tickets del cliente, igual que las anteriores: etiqueta visible y
    // `.filter-bar`, asi que la misma regla las dimensiona. Se declaran para que la lista
    // siga siendo la lista completa de quien usa el buscador.
    { fichero: 'domains/DomainsPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    { fichero: 'support/SupportTicketsPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    // Documentos de Knowledge y descubrimiento de activos: los dos anaden buscador con etiqueta
    // visible dentro de `.filter-bar`, asi que la misma regla que Pentests e Issues los
    // dimensiona y **no necesitan ninguna regla nueva**. Se declaran para que la lista siga
    // siendo la lista completa de quien usa el buscador, que es lo que este test comprueba.
    { fichero: 'knowledge/KnowledgeDocumentsView.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
    { fichero: 'assetDiscovery/AssetDiscoveryPage.tsx', contenedor: 'filter-bar', conEtiquetaVisible: true },
  ]

  function ficherosConBuscador(): string[] {
    const encontrados: string[] = []
    const recorrer = (relativa: string): void => {
      for (const entrada of readdirSync(join(RAIZ, relativa), { withFileTypes: true })) {
        const ruta = join(relativa, entrada.name)
        if (entrada.isDirectory()) {
          recorrer(ruta)
        } else if (entrada.name.endsWith('.tsx') && !entrada.name.endsWith('.test.tsx')) {
          const contenido = readFileSync(join(RAIZ, ruta), 'utf8')
          if (contenido.includes('search-field')) {
            encontrados.push(ruta.replace(/\\/g, '/'))
          }
        }
      }
    }
    recorrer('features')
    return encontrados
  }

  it('no hay ninguna pagina con el buscador fuera del inventario', () => {
    // El fallo que esto vigila: una pagina nueva copia el buscador tal cual y, si cae en un
    // contenedor que ninguna regla dimensiona, se queda con la caja base - que ya no lleva
    // ancho - y aparece estirada o colapsada.
    const reales = ficherosConBuscador().sort()
    const declarados = INVENTARIO.map((entrada) => `features/${entrada.fichero}`).sort()
    expect(
      reales,
      'ha aparecido una pagina con el buscador que no esta en el inventario de este test, o una '
        + 'del inventario que ya no lo usa. El contexto nuevo necesita su regla de ancho.',
    ).toEqual(declarados)
  })

  it('cada entrada del inventario usa de verdad la estructura que declara', () => {
    for (const entrada of INVENTARIO) {
      const contenido = readFileSync(join(RAIZ, 'features', entrada.fichero), 'utf8')
      expect(
        contenido.includes('search-field'),
        `${entrada.fichero} esta en el inventario pero ya no tiene buscador`,
      ).toBe(true)
      const usaEnvoltorio = contenido.includes('filter-field-search')
      expect(
        usaEnvoltorio,
        `${entrada.fichero} declara conEtiquetaVisible: ${entrada.conEtiquetaVisible}, pero `
          + (entrada.conEtiquetaVisible
            ? 'no usa el envoltorio .filter-field-search'
            : 'usa el envoltorio .filter-field-search, que es el contexto con etiqueta visible'),
      ).toBe(entrada.conEtiquetaVisible)
    }
  })

  it('el buscador sin etiqueta visible baja a la linea de los controles', () => {
    // Sin etiqueta visible el buscador es una fila mas corta que sus vecinos: con
    // `align-items: start` su caja queda pegada a las etiquetas y su campo una linea por encima
    // de los desplegables. `align-self: end` lo devuelve a la linea del control.
    expect(valorDe('.filter-bar > .search-field', 'align-self')).toBe('end')
  })

  it('el buscador con etiqueta visible no se hunde de mas', () => {
    // Al reves: con la etiqueta a la vista los tres controles ya comparten linea, y un `end`
    // aqui los hundiria por debajo del resto.
    expect(valorDe('.filter-bar .filter-field-search', 'align-self')).toBeUndefined()
    expect(valorDe('.filter-bar .filter-field-search > .search-field', 'align-self')).toBeUndefined()
  })
})

describe('ningun selector declara dos veces la misma propiedad', () => {
  it('.filter-field no repite min-width', () => {
    // Estaban en dos reglas del mismo selector, con 160 px contra 168 px, y la que ganaba
    // dependia del orden del fichero. La misma trampa que con el align-items de la barra.
    const lineas = reglasQueDeclaran('.filter-field', 'min-width')
    expect(
      lineas.length,
      `.filter-field declara min-width en ${lineas.length} reglas (lineas ${lineas.join(', ')}). `
        + 'Dos valores para la misma propiedad y el que gana depende del orden del fichero.',
    ).toBe(1)
  })

  it('.filter-bar declara align-items una sola vez', () => {
    const lineas = reglasQueDeclaran('.filter-bar', 'align-items')
    expect(
      lineas.length,
      `.filter-bar declara align-items en ${lineas.length} reglas (lineas ${lineas.join(', ')}).`,
    ).toBe(1)
  })
})
