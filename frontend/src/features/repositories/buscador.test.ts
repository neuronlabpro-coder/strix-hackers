/**
 * El buscador del inventario remoto, de punta a punta.
 *
 * ## Qué defectos fija
 *
 * Cuatro, y todos pasaron porque la búsqueda **no daba error**: contestaba `200`, traía lo que
 * fuera y el panel lo pintaba con un número que cuadraba.
 *
 * 1. **Un backend desactualizado finge que ha buscado.** Un parámetro de consulta que el
 *    servidor no declara se ignora en silencio: ni `422`, ni aviso. Escribiendo `shytai` salía
 *    «100 de 100 repositorios coinciden con tu búsqueda» con nombres que no la contenían, y no
 *    había nada en pantalla que lo dijera. El buscador parecía roto; lo que estaba
 *    desactualizado era el servidor. Por eso la ruta devuelve `busqueda_aplicada` y aquí se
 *    comprueba que el panel avisa cuando lo que pidió no es lo que se aplicó.
 *
 * 2. **La ventana de diez.** Estaba en diez y con ella el buscador era inservible: un cliente
 *    con quinientos repositorios escribía el nombre de uno que estaba en la posición trescientos
 *    y no lo encontraba. El servidor filtraba bien y devolvía `total: 1`, pero esa fila no
 *    cabía en la ventana de la petición anterior, así que el resultado correcto **nunca llegaba a
 *    pintarse**, y la pantalla no decía por qué. Ahora la ventana es el tope de la ruta y quien
 *    llama decide el tamaño.
 *
 * 3. **El buscador desaparece cuando no hay resultados.** Este es el grave, y por eso tiene
 *    prueba propia. La barra estaba dentro de la rama de «hay repositorios», así que una búsqueda
 *    sin coincidencias sacaba de la pantalla el campo con el que corregirla. El usuario se
 *    quedaba sin poder borrar lo que había escrito, sin poder escribir otra cosa y sin poder
 *    buscar: la única salida era cerrar el modal y volverlo a abrir.
 *
 * 4. **El título centrado.** `justify-content: space-between` con tres hijos dejaba el nombre
 *    del repositorio en medio de la fila.
 *
 * ## Por qué se lee el CSS y el código en vez de montar el modal
 *
 * Porque lo que se vigila son **contratos y medidas**, no comportamiento de un árbol: que la
 * ventana sea la correcta, que la barra se monte también sin resultados, que la fila no reparta
 * el texto al centro, que el retardo exista y que el aviso se monte cuando toca. Todo se lee del
 * fuente, y así el test corre sin navegador, sin red y en un segundo.
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
  it('la ventana es el tope de la ruta y quien llama decide el tamaño', () => {
    // Diez era el valor anterior, y con él el buscador no encontraba nada en un inventario
    // grande. Cien es el `le=100` de la ruta, que es el techo de lo que se puede pedir en una
    // llamada —y esa ruta trae el inventario entero del cliente del proveedor, así que el tope
    // protege al navegador, no al servidor.
    expect(API).toMatch(/const VENTANA_INVENTARIO_POR_DEFECTO = 100\b/)
    expect(API).not.toMatch(/const VENTANA_INVENTARIO_POR_DEFECTO = 10\b/)
    // Y la ventana se manda de verdad, con lo que pase quien llama, no es una constante muerta.
    expect(API).toContain('limit: String(opciones.limit ?? VENTANA_INVENTARIO_POR_DEFECTO)')
  })

  it('el buscador no desaparece cuando la búsqueda no encuentra nada', () => {
    /*
     * El defecto más caro de esta pantalla, y el único que deja al usuario sin salida.
     *
     * Con la barra dentro de la rama de la lista, una búsqueda sin coincidencias sacaba de la
     * pantalla el campo con el que corregirla. No había ni botón ni atajo: había que cerrar el
     * modal y volverlo a abrir, y como la búsqueda se recuerda, salía igual.
     *
     * El aserto mira **dónde** se monta la barra, no si hay una en el fichero: `toContain`
     * sobre el `<input>` pasaría igual con la barra dentro de la rama equivocada, que es
     * exactamente lo que no se quiere. Lo que se comprueba es que la condición de la barra no
     * menciona la lista.
     */
    const barra = /\{!isLoadingInventory && !inventarioFallido \? \(([\s\S]*?)\) : null\}/.exec(
      MODAL,
    )
    expect(barra, 'no se encuentra la barra del buscador fuera de la rama de la lista').not.toBeNull()
    expect(barra![1]).toContain('remote-toolbar')
    expect(barra![1]).toContain('remote-search-input')
    // Y la condición no puede depender de que haya resultados: esa es exactamente la causa.
    expect(barra![0]).not.toMatch(/remoteRepositories\.length/)
    expect(barra![0]).not.toMatch(/reposVisibles\.length/)
    // El campo sigue siendo editable con la lista vacía, para que se pueda borrar la búsqueda.
    const campo = /<input\s+type="search"[\s\S]{0,400}?\/>/.exec(MODAL)
    expect(campo, 'no se encuentra el campo de búsqueda').not.toBeNull()
    expect(campo![0]).not.toMatch(/disabled/)
  })

  it('el aviso de lista vacía distingue «no hay nada» de «no coincide»', () => {
    // Con un solo texto, el caso frecuente —una letra de más— salía con un mensaje que no
    // menciona la búsqueda, y el usuario leía «mi credencial está rota» cuando lo que había
    // escrito era una letra. Y el texto de la búsqueda vacía no lleva el término, porque no hay.
    expect(MODAL).toContain('modal.searchNoResults')
    expect(ES.modal.searchNoResults).toContain('{{search}}')
    expect(EN.modal.searchNoResults).toContain('{{search}}')
  })

  it('el recorte de la lista se dice, y solo sin búsqueda', () => {
    /*
     * La otra mitad del compromiso del tope: con quinientos repositorios y una ventana de cien,
     * `total` es el número de lo que **hay**, no el de lo que se puede elegir. Sin este aviso el
     * usuario recorre la lista creyendo que la ha visto.
     *
     * Y sale solo sin búsqueda, porque con el filtro activo el `total` ya es el de lo que
     * coincide y la búsqueda alcanza a todo: un aviso de «puede que falten» al lado de una lista
     * ya filtrada sería miedo sin motivo.
     */
    expect(MODAL).toContain('modal.truncated')
    expect(MODAL).toMatch(
      /busqueda\.trim\(\) === '' && totalInventario > reposVisibles\.length/,
    )
    for (const datos of [ES, EN]) {
      expect(datos.modal.truncated).toContain('{{missing}}')
    }
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
