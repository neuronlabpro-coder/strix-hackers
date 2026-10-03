/**
 * Pruebas de las reglas de filtro del inventario de activos.
 *
 * ## Qué hay aquí y por qué no se prueba la pantalla
 *
 * Porque `AssetDiscoveryPage` necesita `useAuth`, un `toast-context` y una red, y este proyecto no
 * tiene `@testing-library/react` ni entorno de DOM: sus pruebas son de lógica. Lo que se comprueba
 * abajo son **reglas**, y las reglas son justo lo que una pantalla puede cumplir en apariencia y
no cumplir de verdad.
 *
 * ## El defecto que estas pruebas existen para no dejar volver
 *
 * `'ALL'` del desplegable de tipo es la opción "sin filtro", no un valor de la API. Si la regla
 * de «hay filtros» lo contara, el botón de limpiar aparecería en la pantalla recién abierta, con
 * la lista sin filtrar, y al pulsarlo no cambiaría nada. El operador vería un botón que a veces no
 * hace nada y dejaría de usarlo —que es el modo en que un botón de limpiar deja de servir.
 *
 * Y el segundo defecto es el `trim`: el servidor hace `.strip()` antes de aplicar el término, así
 * que un campo con espacios filtra lo mismo que uno vacío. Sin `trim` en el cliente, el botón
 * aparecería sobre una lista que no está filtrada.
 */

import { describe, expect, it } from 'vitest'

import { hayFiltrosPuestos, type InventarioQuery } from './filtrosInventario'

const SIN_FILTROS: InventarioQuery = {
  type: 'ALL',
  domainId: '',
  search: '',
  createdFrom: '',
  createdTo: '',
}

function con(parche: Partial<InventarioQuery>): InventarioQuery {
  return { ...SIN_FILTROS, ...parche }
}

describe('inventario de activos, qué cuenta como «hay filtros»', () => {
  it('sin nada puesto no hay filtros', () => {
    expect(hayFiltrosPuestos(SIN_FILTROS)).toBe(false)
  })

  it('«Todos los tipos» NO es un filtro: es la ausencia de filtro', () => {
    // Este es el test del defecto. Con `'ALL'` contando, el botón de limpiar aparecería siempre.
    expect(hayFiltrosPuestos(con({ type: 'ALL' }))).toBe(false)
  })

  it('un tipo concreto sí es un filtro', () => {
    expect(hayFiltrosPuestos(con({ type: 'SUBDOMAIN' }))).toBe(true)
  })

  it('solo espacios en el buscador NO son un filtro', () => {
    expect(hayFiltrosPuestos(con({ search: '   ' }))).toBe(false)
  })

  it('el texto cuenta cuando tiene algo que no son espacios', () => {
    expect(hayFiltrosPuestos(con({ search: 'api' }))).toBe(true)
  })

  it('el dominio cuenta, y cuenta solo con identificador', () => {
    // El desplegable manda el `id` del dominio, no su nombre. Un nombre con espacios es un `id`
    // distinto, así que la comparación tiene que ser contra el `id`.
    expect(hayFiltrosPuestos(con({ domainId: '' }))).toBe(false)
    expect(hayFiltrosPuestos(con({ domainId: '7c9e6679-7425-40de-944b-e07fc1f90ae7' }))).toBe(true)
  })

  it('cada extremo del rango cuenta por su cuenta', () => {
    expect(hayFiltrosPuestos(con({ createdFrom: '2026-03-01' }))).toBe(true)
    expect(hayFiltrosPuestos(con({ createdTo: '2026-03-31' }))).toBe(true)
  })

  it('los cinco filtros juntos se limpian volviendo al estado vacío', () => {
    const todoPuesto = con({
      type: 'API_ENDPOINT',
      domainId: '7c9e6679-7425-40de-944b-e07fc1f90ae7',
      search: 'webhook',
      createdFrom: '2026-03-01',
      createdTo: '2026-03-31',
    })
    expect(hayFiltrosPuestos(todoPuesto)).toBe(true)
    expect(hayFiltrosPuestos(SIN_FILTROS)).toBe(false)
  })
})
