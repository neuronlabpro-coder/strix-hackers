/**
 * Pruebas de la clasificación de fallos de sesión.
 *
 * ## Por qué este fichero existe
 *
 * `isSessionInvalid` decide si un fallo de red cierra la sesión. La versión anterior no
 * decidía: borraba la sesión ante **cualquier** fallo, y eso convertía un `502` o un corte
 * de conexión en un cierre de sesión. El síntoma era que volver de GitHub o GitLab
 * expulsaba al usuario, y la causa era que la primera petición al volver de esos
 * providers es la que falla cuando la red ha cambiado mientras el usuario estaba fuera.
 *
 * Estas pruebas fijan la clasificación en los dos sentidos: lo que debe cerrar sesión y lo
 * que no. Un caso de "no debe" que se rompe es una sesión destruida; uno de "debe" que se
 * rompe es un bucle de reintentos contra un token muerto.
 */

import { describe, expect, it } from 'vitest'

import { ApiError } from './api'
import { isSessionInvalid } from './session-errors'

describe('isSessionInvalid', () => {
  it('cierra la sesion con un 401: el token no vale', () => {
    expect(isSessionInvalid(new ApiError(401))).toBe(true)
  })

  it('cierra la sesion con un 403: el token vale pero ya no llega', () => {
    // Workspace del que el usuario ya no es miembro, o cuenta desactivada. Reintentar con
    // el mismo token no va a funcionar nunca, asi que la sesion si esta muerta.
    expect(isSessionInvalid(new ApiError(403))).toBe(true)
  })

  it('NO cierra la sesion con un 5xx: el servidor falla, no el token', () => {
    // Este es el caso que.reportaba el usuario. Un 502 del proxy al volver del proveedor
    // de OAuth no dice nada sobre la validez del token.
    for (const status of [500, 502, 503, 504]) {
      expect(isSessionInvalid(new ApiError(status))).toBe(false)
    }
  })

  it('NO cierra la sesion con un 429: hay que esperar, no salir', () => {
    expect(isSessionInvalid(new ApiError(429))).toBe(false)
  })

  it('NO cierra la sesion ante un error de red', () => {
    // `fetch` lanza `TypeError` cuando no hay red, y un `AbortError` —que tambien hereda de
    // `Error` pero no de `TypeError`— cuando se cancela la peticion. Ninguno de los dos ha
    // mirado el token.
    expect(isSessionInvalid(new TypeError('Failed to fetch'))).toBe(false)
    expect(isSessionInvalid(new DOMException('aborted', 'AbortError'))).toBe(false)
  })

  it('NO cierra la sesion ante un error sin codigo de estado', () => {
    // Un fallo de una dependencia, o de una version distinta del cliente. No se puede
    // afirmar que la sesion este muerta, y la opcion que no destruye nada recuperable es
    // tratarlo como transitorio.
    expect(isSessionInvalid(new Error('boom'))).toBe(false)
    expect(isSessionInvalid(undefined)).toBe(false)
    expect(isSessionInvalid(null)).toBe(false)
  })

  it('distingue un codigo de estado numerico de uno que no lo es', () => {
    // Un `status` que llega como cadena —por ejemplo de un error deserializado— no es un
    // numero y no debe compararse con `===` contra 401 sin convertirlo antes.
    expect(isSessionInvalid({ status: '401' })).toBe(false)
  })
})
