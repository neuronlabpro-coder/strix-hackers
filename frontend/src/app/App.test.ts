/**
 * Pruebas de la geometria del shell: que la consola de SuperAdmin no herede el layout del panel
 * de cliente.
 *
 * ## Por que este fichero existe
 *
 * `/admin` estaba **anidada dentro** de `<Route element={<ProtectedShell />}>`, pese a que el
 * comentario de al lado del codigo decia que vivia fuera a proposito. El comentario describia la
 * intencion; el JSX no la implementaba. Y como el comentario razonaba bien -la consola cruza
 * tenants y el shell arrastra `X-Organization-Id` a todas sus peticiones-, la intencion no es
 * negociable: es la que impide que un operador llegue a ver datos de otro tenant.
 *
 * El sintoma era puramente visual y por eso parecia un fallo de CSS: en la consola se veian
 * **dos** barras laterales, **dos** barras superiores y **dos** selectores de idioma, mas una
 * columna negra muerta a la izquierda del contenido. La causa era un margen contado dos veces:
 *
 * - `.sidebar` esta en `position: fixed` de 256 px, asi que se superpone y no ocupa sitio en el
 *   flujo, pero `.content-shell` lleva `margin-left: 256px` para apartarse de el.
 * - La consola pintaba **su propio** sidebar dentro de uno que ya estaba ahi, de modo que su
 *   `.content-shell` quedaba anidado en el del cliente y heredaba el margen dos veces: 512 px de
 *   desplazamiento y un hueco de 256 px que se leia como una columna negra.
 *
 * Dos revisiones seguidas concluyeron que el fallo era una copia antigua en la cache del
 * navegador, y en las dos se llego a esa conclusion leyendo un comentario de codigo en vez de
 * medir la sangria del JSX. Estas pruebas miden la sangria, que es lo que el parser decide de
 * verdad, y fallan en cuanto alguien vuelve a anidar la consola.
 *
 * ## Por que se prueba sobre el texto y no renderizando
 *
 * Montar el arbol exigiria `jsdom` y un provider de sesion completo, y una prueba que necesita
 * catorce andamiajes para comprobar una regla de sangria es una prueba que nadie va a mantener.
 * El invariante es una propiedad **estructural** del fichero, no de su comportamiento, asi que se
 * lee el fichero. Ademas, el fallo que corrige esto fue precisamente un fallo de estructura que
 * ninguna prueba de comportamiento habria atrapado.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const RUTA_APP = fileURLToPath(new URL('./App.tsx', import.meta.url))
const RUTA_CSS = fileURLToPath(new URL('../styles/index.css', import.meta.url))

const lineasApp = readFileSync(RUTA_APP, 'utf8')
  .replace(/\r\n/g, '\n')
  .split('\n')

function sangria(linea: string): number {
  return linea.length - linea.trimStart().length
}

/**
 * La sangria de la linea `<Route` que abre la ruta `ruta`.
 *
 * `path` suele ir en la linea siguiente a `<Route` cuando el `element` es un bloque multilinea, asi
 * que se busca el atributo y se retrocede hasta la apertura. Devuelve `null` si la ruta no existe.
 */
function sangriaDeRuta(ruta: string): number | null {
  for (let indice = 0; indice < lineasApp.length; indice += 1) {
    if (!new RegExp(`path="${ruta.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`).test(lineasApp[indice])) {
      continue
    }
    for (let arriba = indice; arriba >= 0; arriba -= 1) {
      if (lineasApp[arriba].trimStart().startsWith('<Route')) {
        return sangria(lineasApp[arriba])
      }
    }
  }
  return null
}

/**
 * La sangria de la linea `<Route` que lleva `element={<Componente` en esa misma linea o en la
 * siguiente. Devuelve `null` si no existe tal Route.
 */
function sangriaDeElemento(elemento: string): number | null {
  const patron = new RegExp(`element=\\{<${elemento}`)
  for (let indice = 0; indice < lineasApp.length; indice += 1) {
    const ventana = lineasApp[indice] + '\n' + (lineasApp[indice + 1] ?? '')
    if (!patron.test(ventana)) {
      continue
    }
    for (let arriba = indice; arriba >= 0; arriba -= 1) {
      if (lineasApp[arriba].trimStart().startsWith('<Route')) {
        return sangria(lineasApp[arriba])
      }
    }
  }
  return null
}

const sangriaAdmin = sangriaDeRuta('/admin')
const sangriaShell = sangriaDeElemento('ProtectedShell')
const sangriaDashboard = sangriaDeRuta('/dashboard')
const sangriaComodín = sangriaDeRuta('*')

describe('la consola de SuperAdmin cuelga fuera del shell de cliente', () => {
  it('existe la ruta /admin', () => {
    expect(sangriaAdmin).not.toBeNull()
  })

  it('ProtectedShell y /admin son hermanos: la consola abre con la misma sangria que el shell', () => {
    // La asercion que lo detecta. Dos `<Route>` hermanos cuelgan del mismo padre y por eso se
    // abren **con la misma sangria**; un hijo de `ProtectedShell` se abre **dos espacios mas
    // adentro**, que es la sangria de `/dashboard`. Igualdad con el shell, entonces, es
    // hermandad; y la prueba siguiente fija que la diferencia de sangria con `/dashboard` es
    // real, para que esta igualdad no se cumpla sola.
    expect(sangriaAdmin, 'no se encuentra la ruta /admin en App.tsx').not.toBeNull()
    expect(sangriaShell, 'no se encuentra element={<ProtectedShell />} en App.tsx').not.toBeNull()
    expect(
      sangriaAdmin as number,
      `/admin abre con sangria ${sangriaAdmin} y ProtectedShell con ${sangriaShell}. Con la misma `
        + 'sangria son HERMANOS, que es lo que quiere este test. Si /admin tuviera dos espacios '
        + 'mas seria su HIJA, y volverian a verse dos sidebars, dos topbars, dos selectores de '
        + 'idioma y la columna negra de 256 px, porque el margin-left del content-shell se '
        + 'heredaria dos veces. Si tuviera dos menos, /admin no colgaria de ningun sitio.',
    ).toBe(sangriaShell as number)
  })

  it('los hijos del shell son dos espacios mas adentro, asi que la igualdad no es vacia', () => {
    // Sin esta prueba, el test anterior pasaria igual con `ProtectedShell` borrado de `App.tsx`.
    // Aqui se fija que `/dashboard` -un hijo de ProtectedShell- este realmente mas adentro, que
    // es lo que le da sentido a comparar.
    expect(sangriaDashboard).not.toBeNull()
    expect(
      (sangriaDashboard as number) - (sangriaAdmin as number),
      '/dashboard deberia ir dos espacios mas adentro que /admin: es hija de ProtectedShell',
    ).toBeGreaterThan(0)
  })

  it('/admin tiene la misma sangria que la ruta comodin, o sea que ambas cuelgan de <Routes>', () => {
    // La ruta comodin `*` es hija directa de `<Routes>` por construccion: no puede estar dentro
    // de un layout. Igualar la sangria de `/admin` con la suya demuestra que `/admin` tambien lo
    // esta, sin tener que reconstruir el arbol completo.
    expect(sangriaComodín).not.toBeNull()
    expect(sangriaAdmin).toBe(sangriaComodín)
  })

  it('las seis pantallas de la consola cuelgan por debajo de /admin', () => {
    // Son rutas **relativas**: cuelgan de un `Route` sin `path` que hay dentro de `/admin`, y por
    // eso no se buscan como `/admin/tenants`. Lo que importa para este test es la profundidad:
    // si una de ellas dejara de colgar de la consola, su sangria bajaria a la de sus hermanas.
    for (const pantalla of ['tenants', 'users', 'sales', 'audit', 'llm', 'tickets']) {
      const sangria = sangriaDeRuta(pantalla)
      expect(sangria, `falta la pantalla "${pantalla}" de la consola`).not.toBeNull()
      expect(
        (sangria as number) - (sangriaAdmin as number),
        `la pantalla "${pantalla}" deberia ir mas profunda que /admin; `
          + 'si no, cuelga de otro sitio',
      ).toBeGreaterThan(0)
    }
  })
})

describe('la geometria del shell, que es lo que hace visible el fallo', () => {
  const css = readFileSync(RUTA_CSS, 'utf8').replace(/\r\n/g, '\n')

  /** Todos los cuerpos de las reglas de nivel raiz que empiezan por `selector`. */
  function cuerposDe(selector: string): string[] {
    const escapado = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const patron = new RegExp(`^${escapado}\\s*\\{([^}]*)\\}`, 'gm')
    const cuerpos: string[] = []
    let encontrada = patron.exec(css)
    while (encontrada !== null) {
      // Una regla que redefine el selector mas abajo no invalida la anterior: se concatenan todas
      // y se busca la propiedad en el conjunto. `.content-shell` aparece en dos sitios, y el
      // `margin-left` esta en el segundo, asi que mirar solo la primera daria un falso negativo.
      cuerpos.push(encontrada[1])
      encontrada = patron.exec(css)
    }
    return cuerpos
  }

  it('.content-shell reserva el ancho del sidebar fijo con margin-left', () => {
    const cuerpos = cuerposDe('.content-shell')
    expect(cuerpos.length, 'no se encuentra la regla .content-shell').toBeGreaterThan(0)
    expect(
      cuerpos.some((cuerpo) => /margin-left:\s*256px/.test(cuerpo)),
      '.content-shell debe llevar margin-left: 256px en algun sitio, para apartarse del '
        + 'sidebar que esta en position: fixed y por tanto no ocupa sitio en el flujo.',
    ).toBe(true)
  })

  it('.main-content NO se centra con margin-inline: auto', () => {
    const cuerpos = cuerposDe('.main-content')
    expect(cuerpos.length, 'no se encuentra la regla .main-content').toBeGreaterThan(0)
    // El sidebar es fijo, asi que el area util ya esta desplazada 256 px. Centrar dentro de un
    // area ya desplazada la desplaza otra vez: en cuanto la ventana pasa de
    // 256 + 1400 = 1656 px aparece un hueco a la izquierda del contenido, entre el sidebar y la
    // primera tarjeta. A 1900 px ese hueco mide 122 px, y en las tablas anchas se come justo el
    // ancho que hacia falta para no desbordar.
    expect(
      cuerpos.some((cuerpo) => /margin-inline:\s*auto/.test(cuerpo)),
      '.main-content vuelve a usar margin-inline: auto. Con el sidebar en position: fixed, '
        + 'centrar el contenido dentro de un .content-shell que ya lleva margin-left deja una '
        + 'columna muerta a la izquierda en cuanto la ventana supera los 1656 px.',
    ).toBe(false)
  })

  it('la barra de scroll de las tablas usa el ancho delgado, no el del navegador', () => {
    // Sin esto, el pulgar de quince pixeles del navegador sobre un fondo casi negro se lee como
    // un borde y no como algo arrastrable, y la tabla parece recortada en vez de desplazable.
    expect(css).toMatch(/\.table-(scroll|wrapper)[^{]*\{[^}]*scrollbar-width:\s*thin/)
  })
})
