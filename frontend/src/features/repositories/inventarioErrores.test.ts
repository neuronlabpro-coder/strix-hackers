/**
 * Que dice el modal cuando la carga del inventario falla.
 *
 * ## El defecto que estas pruebas existen para no dejar volver
 *
 * En la cuenta de SuperAdmin, el modal de «Conectar repositorio» pintaba «Este proveedor todavía
 * no tiene conector» sobre una pestaña en la que la lista de repositorios de al lado tenía tres
 * entradas de GitHub, y el Dashboard decía «Configuración inicial · 3 de 3 completados».
 *
 * El motivo real era otro: la credencial OAuth de GitHub de esa organización había caducado dos
 * días antes (`token_expires_at` = 2026-10-01) y GitHub contestaba `401 Bad credentials`. El
 * backend lo traducía a `502`, y el `catch` del frontend trataba **todo lo que no fuese `409`**
 * como «no hay conector».
 *
 * O sea: el diagnóstico que se leía era el único de los tres que no se podía comprobar mirando
 * los datos, y encima mandaba al usuario a hacer algo —registrar un conector— que ya estaba
 * hecho desde el primer día.
 *
 * ## Por qué se prueba la tabla y no el modal montado
 *
 * Porque lo que hay que fijar es un **reparto de códigos a mensajes**, que es aritmética pura
 * sobre un número. Este proyecto no tiene `@testing-library/react` ni entorno de DOM, y sus
 * pruebas de frontend son de lógica. Además, un test de render exigiría montear el modal entero
 * para comprobar una línea de un `catch`.
 *
 * Y el reparto se prueba en los dos sentidos: que un `502` **no** acabe en
 * `unsupportedProvider`, y que un `501` —el único código que sí significa «no hay conector»— sí.
 * Con una sola dirección, el test pasa tanto con la tabla bien como con la tabla que devuelve la
 * misma clave para todo.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { claveDeErrorDeInventario } from './inventarioErrores'

// `src/features/repositories/` → `frontend/` → la raíz del repositorio. Cuatro niveles, como en
// `buscador.test.ts`, que está en el mismo directorio.
const RAIZ = fileURLToPath(new URL('../../../../', import.meta.url))
const MODAL = readFileSync(join(RAIZ, 'frontend/src/features/repositories/ConnectRepositoryModal.tsx'), 'utf8')

describe('claveDeErrorDeInventario', () => {
  it('un 409 es que no hay credencial conectada', () => {
    expect(claveDeErrorDeInventario(409)).toBe('modal.noCredential')
  })

  it('un 410 dice que la credencial caducó, que es un hecho distinto de que no exista', () => {
    // El `410` es el código que añadió `_exigir_credencial_vigente`. Si se mezclara con el `409`,
    // el panel pediría conectar cuando lo que hay que hacer es reconectar lo que ya está
    // guardado: dos textos distintos para dos acciones distintas.
    expect(claveDeErrorDeInventario(410)).toBe('modal.credentialExpired')
    expect(claveDeErrorDeInventario(410)).not.toBe(claveDeErrorDeInventario(409))
  })

  it('un 401 del proveedor NO se pinta como «no hay conector»', () => {
    // Este es el test del defecto. El `502` de antes («credencial rechazada») era lo que se
    // veía en pantalla como «no hay conector»; ahora el backend contesta `401` y aquí se lee
    // como lo que es.
    expect(claveDeErrorDeInventario(401)).toBe('modal.credentialRejected')
    expect(claveDeErrorDeInventario(401)).not.toBe('modal.unsupportedProvider')
    // Y el `502` que queda es el que el backend usa para «el proveedor no está disponible», que
    // sí es un problema del proveedor y no de la credencial.
    expect(claveDeErrorDeInventario(502)).toBe('modal.providerUnavailable')
    expect(claveDeErrorDeInventario(502)).not.toBe('modal.unsupportedProvider')
  })

  it('el 501 es el único código que significa «no hay conector»', () => {
    // Y hay que comprobar que existe, no solo que los otros no caen ahí: con una tabla que
    // devolviese `inventoryFailed` para todo, el aserto anterior también pasaría.
    expect(claveDeErrorDeInventario(501)).toBe('modal.unsupportedProvider')
  })

  it('la cuota del proveedor se dice como cuota, no como fallo de credencial', () => {
    // Con la tabla anterior, un `429` salía como «no hay conector». El usuario que llegaba al
    // límite de GitHub recibía instrucciones de registrar un conector que ya tenía.
    expect(claveDeErrorDeInventario(429)).toBe('modal.providerRateLimit')
  })

  it('un 500 se dice como problema del servidor y no como credencial que hay que rehacer', () => {
    // `CryptoError` es del servidor: la fila existe y la clave con la que se cifró no la abre.
    // Decirle al usuario «conecta una credencial» le hace repetir un bucle sin resultado.
    expect(claveDeErrorDeInventario(500)).toBe('modal.credentialUnreadable')
  })

  it('un fallo sin respuesta del servidor no se atribuye al conector', () => {
    // `null` es lo que llega cuando falla la red, el navegador aborta, o la promesa se rechaza
    // sin un `ApiError`. Afirmar «no hay conector» sin haber preguntado es inventar el
    // diagnóstico.
    expect(claveDeErrorDeInventario(null)).toBe('modal.inventoryFailed')
    expect(claveDeErrorDeInventario(null)).not.toBe('modal.unsupportedProvider')
  })

  it('un código desconocido no se atribuye al conector', () => {
    // Un código que no está en la tabla significa que el backend cambió. Lo honesto es decir que
    // no se pudo saber, no afirmar algo que el cliente no sabe.
    expect(claveDeErrorDeInventario(418)).toBe('modal.inventoryFailed')
    expect(claveDeErrorDeInventario(503)).toBe('modal.inventoryFailed')
    expect(claveDeErrorDeInventario(0)).toBe('modal.inventoryFailed')
  })

  it('solo el 501 usa el mensaje de «no hay conector»', () => {
    // Invariante general, para que un caso nuevo no vuelva a colarse en esa clave por descuido.
    const estados = [401, 409, 410, 418, 429, 500, 502, 503, 0]
    for (const estado of estados) {
      expect(claveDeErrorDeInventario(estado), `estado=${estado}`).not.toBe(
        'modal.unsupportedProvider',
      )
    }
  })

  it('nunca hay dos estados con el mismo mensaje', () => {
    // Cada causa necesita su texto: si dos códigos devolvieran la misma clave, el panel volvería
    // a tener el problema que arregla este módulo —un solo mensaje para varias causas— solo que
    // repartido de otra manera. Se excluye `501`, que es el único `unsupportedProvider` por
    // definición, así que la comprobación es literal.
    const vistos = new Map<string, number>()
    for (const estado of [401, 409, 410, 429, 500, 501, 502]) {
      const clave = claveDeErrorDeInventario(estado)
      const previo = vistos.get(clave)
      expect(previo, `el estado ${estado} repite la clave de ${previo}`).toBeUndefined()
      vistos.set(clave, estado)
    }
  })

  it('cada clave de la tabla existe en los dos idiomas, con el marcador de proveedor', () => {
    // El módulo declara las claves como un tipo, y TypeScript no puede comprobar que la clave
    // llegue al JSON: `t('modal.credentialExpired')` compila igual si la clave no existe, y
    // entonces i18next enseña la cadena literal. Por eso se leen los dos ficheros.
    //
    // Y se comprueba el marcador `{{provider}}` porque sin él el mensaje sale con la doble
    // llave en pantalla: en los mensajes que nombran el proveedor, el nombre tiene que estar.
    for (const idioma of ['es', 'en']) {
      const datos = JSON.parse(
        readFileSync(
          join(RAIZ, `frontend/src/locales/${idioma}/repositories.json`),
          'utf8',
        ),
      ) as { modal: Record<string, string> }
      for (const nombre of [
        'noCredential',
        'credentialExpired',
        'credentialRejected',
        'providerRateLimit',
        'providerUnavailable',
        'credentialUnreadable',
        'inventoryFailed',
      ]) {
        const texto = datos.modal[nombre]
        expect(texto, `${idioma}: falta modal.${nombre}`).toBeTruthy()
        expect(texto, `${idioma}: modal.${nombre} sin marcador de proveedor`).toContain(
          '{{provider}}',
        )
      }
      // Y los valores JSON van sin tildes, por decisión del proyecto: una tilde que se cuela
      // sale distinta entre navegadores que no normalizan y es el tipo de cosa que solo se ve
      // en la captura.
      for (const nombre of [
        'noCredential',
        'credentialExpired',
        'credentialRejected',
        'providerRateLimit',
        'providerUnavailable',
        'credentialUnreadable',
        'inventoryFailed',
      ]) {
        expect(datos.modal[nombre], `${idioma}: modal.${nombre} con tilde`).not.toMatch(
          /[áéíóúñÁÉÍÓÚÑ¿¡]/,
        )
      }
    }
  })

  it('el componente usa la tabla y no vuelve a decidir el motivo por su cuenta', () => {
    // La tabla puede estar perfecta y seguir sin arreglar nada si el `catch` del modal no la
    // consulta. Esto ata las dos cosas: el motivo lo decide `claveDeErrorDeInventario`, y el
    // modal ya no tiene su propio «si es 409… si no, no hay conector».
    expect(MODAL).toContain('claveDeErrorDeInventario')
    expect(MODAL).toContain('inventarioFallido')
    // La rama que mentía, tal cual estaba escrita.
    expect(MODAL).not.toContain('error.status === 409')
    expect(MODAL).not.toMatch(/modal\.unsupportedProvider/)
  })
})