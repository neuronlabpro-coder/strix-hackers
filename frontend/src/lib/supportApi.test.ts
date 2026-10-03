/**
 * Los filtros de la vista de tickets del cliente, en la query que se pide.
 *
 * ## Por qué esto necesita prueba y el resto del cliente no
 *
 * Porque el nombre del parámetro es la decisión. El listado acepta `?query=` y la consola
 * acepta `?search=`; si el cliente mandara el nombre equivocado, la ruta no daría error: lo
 * ignoraría —es un parámetro que no conoce—, el servidor devolvería la lista entera y la
 * pantalla parecería funcionar con el filtro puesto. Un buscador que no filtra pero devuelve
 * algo no falla nunca, así que la única forma de verlo es mirar la URL que se ha pedido.
 *
 * ## Por qué no se prueban las dos vistas en el mismo fichero
 *
 * Porque son dos clientes distintos (`supportApi` y `api`), y probarlos juntos haría que un
 * fallo en uno se buscara en el otro. Aquí solo se comprueba el de `supportApi`.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { getMyTickets } from './supportApi'

const TOKEN = 'token-de-prueba'
const ORGANIZACION = '00000000-0000-0000-0000-000000000001'

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Sustituye `fetch` por uno que responde con lo que se le pase. */
function responderCon(cuerpo: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(cuerpo), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
  )
}

/**
 * La query string con la que se ha llamado a `fetch` en la última petición.
 *
 * La base es inventada porque `API_BASE_URL` es una ruta relativa en el entorno de pruebas:
 * `new URL` necesita un origen absoluto, y lo que importa son el `pathname` y el `search`.
 */
function urlDeLaPeticion(): URL {
  const llamadas = vi.mocked(fetch).mock.calls
  const ultima = llamadas[llamadas.length - 1]
  return new URL(String(ultima?.[0]), 'http://localhost')
}

describe('listado de tickets del cliente', () => {
  it('manda los cuatro filtros y la pagina en la query', async () => {
    responderCon({ items: [], total: 0, limit: 25, offset: 25 })

    await getMyTickets(TOKEN, ORGANIZACION, {
      status: 'OPEN',
      query: 'replicacion',
      created_from: '2026-03-10',
      created_to: '2026-03-12',
      limit: 25,
      offset: 25,
    })

    const url = urlDeLaPeticion()
    expect(url.pathname).toBe('/api/v1/support/tickets')
    // El nombre es `query` y no `search`: si se mandara `search`, la ruta lo ignoraría en
    // silencio y devolvería la lista entera. Ver la cabecera del fichero.
    expect(url.searchParams.get('query')).toBe('replicacion')
    expect(url.searchParams.get('status')).toBe('OPEN')
    expect(url.searchParams.get('created_from')).toBe('2026-03-10')
    expect(url.searchParams.get('created_to')).toBe('2026-03-12')
    expect(url.searchParams.get('limit')).toBe('25')
    expect(url.searchParams.get('offset')).toBe('25')
    expect(url.searchParams.get('search')).toBeNull()
  })

  it('no manda los filtros que estan vacios', async () => {
    responderCon({ items: [], total: 0, limit: 25, offset: 0 })

    await getMyTickets(TOKEN, ORGANIZACION, {
      status: undefined,
      query: '',
      created_from: '',
      limit: 25,
    })

    const url = urlDeLaPeticion()
    // Una cadena vacia en la query la lee el backend como un filtro puesto que no coincide
    // con nada: `?query=` es distinto de no mandar `query`.
    expect(url.search).toBe('?limit=25')
  })

  it('sin filtros solo manda limit, que es el tope por defecto de la vista', async () => {
    responderCon({ items: [], total: 0, limit: 25, offset: 0 })

    await getMyTickets(TOKEN, ORGANIZACION, { limit: 25 })

    const url = urlDeLaPeticion()
    expect(url.search).toBe('?limit=25')
  })
})