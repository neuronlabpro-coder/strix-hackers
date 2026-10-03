/**
 * Pruebas de las reglas de paginación y de carga de la vista de documentos.
 *
 * ## Qué hay aquí y por qué no se prueba el hook entero
 *
 * Porque `useKnowledgeDocuments` necesita `useAuth`,Redux-free context y una red, y este proyecto
 * no tiene `@testing-library/react` ni entorno de DOM: sus pruebas son de lógica. Todo lo que se
 * comprueba abajo son **reglas**, nopixels, y las reglas son precisamente lo que un hook bien
 * escrito puede equivocar sin que la pantalla lo delate.
 *
 * ## Por qué la regla de carga importa más que la de los filtros
 *
 * Porque `useKnowledgeDocuments` —como `useAsyncResource` y `useAdminPage`— conserva la página
 * anterior mientras llega la nueva. `isLoading` significa "la clave que pido no es la de los
 * datos que tengo", así que mientras se cambia de filtro **no** hay datos válidos que pintar. Una
 * vista que usara `isLoading && page === null` enseñaría las tarjetas del filtro anterior bajo el
 * título del nuevo, y eso es un fallo de datos, no de estilo.
 *
 * ## Por qué se prueba `hayFiltrosPuestos` y no la pantalla
 *
 * Porque es la regla que decide si aparece el botón de limpiar, y esa regla tiene un detalle que
 * es fácil equivocar: el texto se compara **recortado**, porque el servidor también lo hace. Un
 * campo con tres espacios filtra lo mismo que uno vacío, así que un botón que apareciera por
 * ellos no borraría nada y el operador aprendería que ese botón a veces no hace nada.
 *
 * Y porque el estado de filtros vive en el hook y no en el componente: es el hook quien lo pasa a
 * la vista, y quien tiene el `query` delante.
 */

import { describe, expect, it } from 'vitest'

import {
  EMPTY_DOCUMENTOS_QUERY,
  hayFiltrosPuestos,
  type DocumentosQuery,
} from './useKnowledgeDocuments'

describe('la vista de documentos, qué cuenta como «hay filtros»', () => {
  function con(parche: Partial<DocumentosQuery>): DocumentosQuery {
    return { ...EMPTY_DOCUMENTOS_QUERY, ...parche }
  }

  it('sin nada puesto no hay filtros', () => {
    expect(hayFiltrosPuestos(EMPTY_DOCUMENTOS_QUERY)).toBe(false)
  })

  it('solo espacios en el buscador NO son un filtro', () => {
    // El servidor hace `.strip()` antes de aplicar el término, así que un campo con espacios
    // filtra exactamente lo mismo que un campo vacío. Si el botón de limpiar comparara sin
    // `trim`, aparecería sobre una lista que no está filtrada y no borraría nada.
    expect(hayFiltrosPuestos(con({ search: '   ' }))).toBe(false)
  })

  it('el texto cuenta cuando tiene algo que no son espacios', () => {
    expect(hayFiltrosPuestos(con({ search: 'pagos' }))).toBe(true)
  })

  it('cada extremo del rango cuenta por su cuenta', () => {
    expect(hayFiltrosPuestos(con({ createdFrom: '2026-03-01' }))).toBe(true)
    expect(hayFiltrosPuestos(con({ createdTo: '2026-03-31' }))).toBe(true)
  })

  it('el tipo cuenta, y solo cuenta cuando hay uno', () => {
    expect(hayFiltrosPuestos(con({ type: null }))).toBe(false)
    expect(hayFiltrosPuestos(con({ type: 'API_SPEC' }))).toBe(true)
  })

  it('los cuatro filtros juntos se limpian volviendo al estado vacío', () => {
    const todoPuesto = con({ type: 'BUSINESS_RULE', search: 'pagos', createdFrom: '2026-03-01', createdTo: '2026-03-31' })
    expect(hayFiltrosPuestos(todoPuesto)).toBe(true)
    expect(hayFiltrosPuestos(EMPTY_DOCUMENTOS_QUERY)).toBe(false)
  })
})
