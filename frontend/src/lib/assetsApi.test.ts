/**
 * Pruebas de la lectura del `409` del alta de dominio.
 *
 * ## Por qué esto necesita prueba y el resto del cliente no
 *
 * Porque es el único punto donde el panel decide **qué botón** ofrecer, y un error ahí no
 * se ve como un fallo: se ve como un mensaje raro. Con el motivo mal leído, un usuario cuyo
 * dominio ya está en su propio workspace vería "otra organización lo registró" y concluiría
 * que hay un problema de seguridad en la plataforma, cuando lo único que pasa es que él lo
 * añadió antes.
 *
 * El resto de funciones del cliente son `fetch` con una ruta, y una prueba de eso solo
 * comprobaría que `fetch` existe. Lo que hay que probar es la **decisión**.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { DomainConflictError, createDomain, listDomains } from './assetsApi'

const TOKEN = 'token-de-prueba'
const ORGANIZACION = '00000000-0000-0000-0000-000000000001'

/** Sustituye `fetch` por uno que responde con lo que se le pase. */
function responderCon(cuerpo: unknown, status = 409): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify(cuerpo), {
        status,
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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('alta de dominio', () => {
  it('lee ESTE_WORKSPACE y lo distingue de un conflicto ajeno', async () => {
    responderCon({
      detail: 'El dominio ya está registrado en tu espacio de trabajo',
      motivo: 'ESTE_WORKSPACE',
      already_verified: false,
      domain_name: 'empresa.com',
    })

    const error = await createDomain(TOKEN, ORGANIZACION, {
      domain_name: 'empresa.com',
      verification_method: 'DNS_TXT',
    }).catch((fallo: unknown) => fallo)

    expect(error).toBeInstanceOf(DomainConflictError)
    const conflicto = error as DomainConflictError
    // Este es el caso en el que el botón correcto es "abrir el dominio". Si el motivo se
    // leyera mal, el panel oferecería un mensaje sin acción a un usuario que sí tiene una
    // acción disponible.
    expect(conflicto.motivo).toBe('ESTE_WORKSPACE')
    expect(conflicto.alreadyVerified).toBe(false)
    expect(conflicto.domainName).toBe('empresa.com')
  })

  it('propaga already_verified, que distingue reclamo pendiente de dominio verificado', async () => {
    responderCon({
      detail: 'El dominio ya está registrado por otra organización',
      motivo: 'OTRO_WORKSPACE',
      already_verified: true,
      domain_name: 'banco.com',
    })

    const error = (await createDomain(TOKEN, ORGANIZACION, {
      domain_name: 'banco.com',
      verification_method: 'DNS_TXT',
    }).catch((fallo: unknown) => fallo)) as DomainConflictError

    expect(error.motivo).toBe('OTRO_WORKSPACE')
    expect(error.alreadyVerified).toBe(true)
  })

  it('degrada a DESCONOCIDO si el motivo no es uno de los conocidos', async () => {
    // Un servidor más nuevo que añada un motivo debe verse aquí, y no romper el panel: el
    // `switch` es exhaustivo sobre la unión, y `DESCONOCIDO` es el caso que obliga a
    // tratarlo. Lo que no puede pasar es que un motivo desconocido se pase por conocido.
    responderCon({
      detail: ' algo nuevo ',
      motivo: 'MOTIVO_INVENTADO',
      already_verified: false,
      domain_name: 'futuro.com',
    })

    const error = (await createDomain(TOKEN, ORGANIZACION, {
      domain_name: 'futuro.com',
      verification_method: 'DNS_TXT',
    }).catch((fallo: unknown) => fallo)) as DomainConflictError

    expect(error.motivo).toBe('DESCONOCIDO')
    expect(error.domainName).toBe('futuro.com')
  })

  it('degrada a DESCONOCIDO si el cuerpo no es un objeto', async () => {
    responderCon('texto plano que no es un objeto')

    const error = (await createDomain(TOKEN, ORGANIZACION, {
      domain_name: 'raro.com',
      verification_method: 'DNS_TXT',
    }).catch((fallo: unknown) => fallo)) as DomainConflictError

    // El usuario tiene que saber igualmente que el dominio está ocupado, aunque el botón
    // sea el genérico. Perder el `409` entero sería peor.
    expect(error).toBeInstanceOf(DomainConflictError)
    expect(error.motivo).toBe('DESCONOCIDO')
    // Sin nombre en el cuerpo, se usa el que venía del formulario: es lo que el usuario
    // está intentando registrar, y perderlo dejaría el aviso sin sujeto.
    expect(error.domainName).toBe('raro.com')
  })

  it('lanza ApiError y no DomainConflictError en otros errores', async () => {
    responderCon({ detail: 'no tiene permisos' }, 403)

    const error = await createDomain(TOKEN, ORGANIZACION, {
      domain_name: 'ajeno.com',
      verification_method: 'DNS_TXT',
    }).catch((fallo: unknown) => fallo)

    // Un `403` no es un conflicto de dominio: es una falta de permiso, y el panel tiene
    // que poder distinguirlo para no ofrecer "abre tu dominio" a quien no lo tiene.
    expect(error).not.toBeInstanceOf(DomainConflictError)
  })
})

/**
 * Los filtros del listado de dominios.
 *
 * ## Por qué esto necesita prueba y el `fetch` del resto del cliente no
 *
 * Porque el nombre del parámetro es la decisión. Un `search` que viaje como `query` —o al
 * revés— no da error: la ruta lo ignora porque es un parámetro que no conoce, el servidor
 * devuelve la lista entera y la pantalla parece funcionar con el filtro puesto. Es un fallo
 * que no se ve en ningún sitio, y por eso se afirma sobre la URL que se ha pedido.
 */
describe('listado de dominios', () => {
  it('manda los cuatro filtros y la pagina en la query', async () => {
    responderCon({ items: [], total: 0, limit: 25, offset: 50 }, 200)

    await listDomains(TOKEN, ORGANIZACION, {
      search: 'acme',
      status: 'PENDING',
      created_from: '2026-03-10',
      created_to: '2026-03-12',
      limit: 25,
      offset: 50,
    })

    const url = urlDeLaPeticion()
    expect(url.pathname).toBe('/api/v1/assets/domains')
    expect(url.searchParams.get('search')).toBe('acme')
    expect(url.searchParams.get('status')).toBe('PENDING')
    expect(url.searchParams.get('created_from')).toBe('2026-03-10')
    expect(url.searchParams.get('created_to')).toBe('2026-03-12')
    expect(url.searchParams.get('limit')).toBe('25')
    expect(url.searchParams.get('offset')).toBe('50')
  })

  it('no manda los filtros que estan vacios', async () => {
    responderCon({ items: [], total: 0, limit: 25, offset: 0 }, 200)

    await listDomains(TOKEN, ORGANIZACION, {
      search: '',
      status: undefined,
      created_from: '',
      limit: 25,
    })

    const url = urlDeLaPeticion()
    // Una cadena vacia en la query la lee el backend como un filtro puesto que no coincide
    // con nada: `?search=` es distinto de no mandar `search`.
    expect(url.search).toBe('?limit=25')
  })

  it('sin filtros no pone query string en absoluto', async () => {
    responderCon({ items: [], total: 0, limit: 25, offset: 0 }, 200)

    await listDomains(TOKEN, ORGANIZACION)

    const url = urlDeLaPeticion()
    expect(url.search).toBe('')
  })
})
