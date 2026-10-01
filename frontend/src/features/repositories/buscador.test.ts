/**
 * El buscador del inventario remoto, de punta a punta.
 *
 * ## Qué defectos fija
 *
 * Tres, y los tres pasaron porque la búsqueda **no daba error**: contestaba `200`, traía el
 * inventario entero y el panel lo pintaba con un número que cuadraba.
 *
 * 1. **Un backend desactualizado finge que ha buscado.** Un parámetro de consulta que el
 *    servidor no declara se ignora en silencio: ni `422`, ni aviso. Escribiendo `shytai` salía
 *    «100 de 100 repositorios coinciden con tu búsqueda» con nombres que no la contenían, y no
 *    había nada en pantalla que lo dijera. El buscador parecía roto; lo que estaba
 *    desactualizado era el servidor. Por eso la ruta devuelve `busqueda_aplicada` y aquí se
 *    comprueba que el panel avisa cuando lo que pidió no es lo que se aplicó.
 * 2. **El `limit` fijo de 100 en el cliente.** Con más de cien repositorios, una página entera
 *    de resultados obligaba a desplazarse para usar el buscador que estaba justo encima. Ahora
 *    la ventana son diez.
 * 3. **El título centrado.** `justify-content: space-between` con tres hijos dejaba el nombre
 *    del repositorio en medio de la fila.
 *
 * ## Por qué se lee el CSS y el código en vez de montar el modal
 *
 * Porque lo que se vigila son **contratos y medidas**, no comportamiento de un árbol: que la
 * ventana valga diez, que la fila no reparta el texto al centro, que el retardo exista y que el
 * aviso se monte cuando toca. Los tres se leen del fuente, y así el test corre sin navegador,
 * sin red y en un segundo.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

// `src/features/repositories/` → `frontend/` → la raíz del repositorio. Cuatro niveles, porque
// este fichero vive un nivel más hondo que los demás tests de `src/`. Con tres leía
// `frontend/frontend/src/…` y fallaba con un ENOENT que no llegaba a decir qué ruta quería.
const AQUI = fileURLToPath(new URL('.', import.meta.url))
const RAIZ = fileURLToPath(new URL('../../../../', import.meta.url))

const API = readFileSync(join(RAIZ, 'frontend/src/lib/api.ts'), 'utf8')
const MODAL = readFileSync(join(AQUI, 'ConnectRepositoryModal.tsx'), 'utf8')
const CSS = readFileSync(join(RAIZ, 'frontend/src/styles/index.css'), 'utf8').replace(/\r\n/g, '\n')
const ES = JSON.parse(readFileSync(join(RAIZ, 'frontend/src/locales/es/repositories.json'), 'utf8'))
const EN = JSON.parse(readFileSync(join(RAIZ, 'frontend/src/locales/en/repositories.json'), 'utf8'))
const SCHEMAS = readFileSync(join(RAIZ, 'backend/apps/repositories/schemas.py'), 'utf8')
const ROUTER = readFileSync(join(RAIZ, 'backend/apps/repositories/router.py'), 'utf8')

describe('El buscador del inventario remoto', () => {
  it('la ventana son diez, no cien', () => {
    // Con cien, la lista se llenaba entera y había que desplazarse para usar el buscador que
    // estaba encima. Con diez, lo que no coincide se ve de un vistazo.
    expect(API).toMatch(/const VENTANA_INVENTARIO = 10\b/)
    expect(API).not.toMatch(/const VENTANA_INVENTARIO = 100\b/)
    // Y la ventana se manda de verdad, no es una constante muerta.
    expect(API).toContain("limit: String(VENTANA_INVENTARIO)")
  })

  it('la búsqueda se manda al servidor y no se filtra en el cliente', () => {
    // Un filtro en el cliente solo puede ver lo que vino, que con 500 repositorios son diez. Es
    // el bug original, y la forma de volver a él es volver a filtrar en el modal.
    expect(API).toContain("query.set('search', busqueda)")
    // Y lo que se pinta es lo que llegó, sin un filtro por medio. El aserto es sobre la
    // asignación exacta y no sobre «no hay ningún `.filter(`»: hay uno legítimo —el de los
    // repositorios marcados, para importarlos— y un patrón demasiado amplio daría falso positivo
    // y enseñaría a ignorar el test.
    expect(MODAL).toContain('const reposVisibles = remoteRepositories')
    expect(MODAL).not.toMatch(/const reposVisibles = useMemo/)
    expect(MODAL).not.toMatch(/const reposVisibles = remoteRepositories\.filter/)
  })

  it('el retardo existe y writing no se bloquea', () => {
    // Sin retardo, escribir «microservicios» son trece peticiones contra un límite de sesenta
    // por minuto, y se llega al tope escribiendo dos palabras.
    const retardo = /setTimeout\([\s\S]{0,120}?(\d{2,4})/.exec(MODAL)
    expect(retardo, 'no hay retardo en la recarga por búsqueda').not.toBeNull()
    const milisegundos = Number(retardo![1])
    expect(milisegundos).toBeGreaterThanOrEqual(150)
    expect(milisegundos).toBeLessThanOrEqual(400)
    // Y el campo de búsqueda no se deshabilita mientras carga: si se deshabilitara, «solo deja
    // poner una palabra» sería literal. El aserto se limita al `<input type="search">` porque
    // el botón de «Recargar» **sí** se deshabilita mientras carga, y eso es lo correcto.
    const campo = /<input\s+type="search"[\s\S]{0,400}?\/>/.exec(MODAL)
    expect(campo, 'no se encuentra el campo de búsqueda').not.toBeNull()
    expect(campo![0]).not.toMatch(/disabled/)
    expect(campo![0]).toContain('onChange')
  })

  it('la ruta devuelve la búsqueda que aplicó, y el panel avisa si no coincide', () => {
    // El eco es lo que convierte «el buscador no funciona» en «el servidor no está filtrando».
    expect(SCHEMAS).toContain('busqueda_aplicada')
    expect(ROUTER).toContain('busqueda_aplicada=')
    expect(MODAL).toContain('filtroIgnorado')
    expect(MODAL).toContain('filtroIgnorado &&')
    // Y el aviso se pinta con el texto de la búsqueda, para que se sepa cuál no se aplicó.
    expect(MODAL).toContain('searchNotApplied')
    expect(ES.modal.searchNotApplied).toContain('{{search}}')
    expect(EN.modal.searchNotApplied).toContain('{{search}}')
  })

  it('sin búsqueda no hay aviso: el inventario entero es lo que se pidió', () => {
    // El aviso se levanta solo con una búsqueda escrita. Con el campo vacío, `total` y la lista
    // tienen por qué cuadrar, y un aviso ahí sería ruido que enseña a ignorar avisos.
    expect(MODAL).toMatch(/setFiltroIgnorado\(\s*search\.trim\(\) !== ''/)
  })

  it('la fila no reparte el texto al centro', () => {
    // `space-between` con tres hijos deja el nombre del repositorio en medio de la fila, y un
    // nombre centrado se lee peor que alineado: hay que recorrer la línea para saber por dónde
    // empieza.
    const bloque = /\.remote-item \{([\s\S]*?)\}/.exec(CSS)
    expect(bloque, 'no hay regla .remote-item').not.toBeNull()
    expect(bloque![1]).not.toMatch(/justify-content:\s*space-between/)
    // El texto toma el hueco sobrante y el botón se queda al extremo derecho.
    const copy = /\.remote-copy \{([\s\S]*?)\}/.exec(CSS)
    expect(copy, 'no hay regla .remote-copy').not.toBeNull()
    expect(copy![1]).toMatch(/flex:\s*1 1 auto/)
  })

  it('el botón de importar no se estira al ocupar el hueco que deja el texto', () => {
    // Sin esto, al darle `flex: 1` al texto, el `gap` crece o el botón hereda el crecimiento y la
    // fila se descuadra en cuanto un nombre es más largo que otro.
    expect(CSS).toMatch(/\.remote-item \.primary-button \{[\s\S]*?flex:\s*0 0 auto/)
  })

  it('el recuento de resultados está en los dos idiomas y con llaves dobles', () => {
    // Las llaves simples no las interpola i18next y salen en pantalla tal cual. Hay un test del
    // backend que lo vigila, y saltó; aquí se comprueba que las dos claves existen.
    for (const datos of [ES, EN]) {
      expect(datos.modal.countAll).toContain('{{shown}}')
      expect(datos.modal.countAll).toContain('{{total}}')
      expect(datos.modal.countFiltered).toContain('{{shown}}')
    }
  })
})
