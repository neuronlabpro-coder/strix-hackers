/**
 * Dos cosas que se vieron en pantalla y que no tienen arreglo de maquetación: una de ellas es un
 * error de contenido y la otra un error de información.
 *
 * ## 1. La consola de SuperAdmin se tragaba el motivo del fallo
 *
 * El `catch` de la carga del resumen no tenía parámetro: el error se perdía entero y la
 * pantalla pintaba «no se pudo cargar la consola» con un botón de «Reintentar» para un `403`, un
 * `500` y un backend caído. Los tres se arseban igual, y el único recurso era reintentar.
 *
 * Peor: **`ApiError` no llevaba el `detail` del servidor**, así que aunque el error se hubiera
 * enseñado no habría dicho nada. Su `message` era la cadena `api_request_failed`, que es una clave
 * de traducción y no un mensaje, y como `Error.message` salía tal cual en cualquier sitio que la
 * imprimiera.
 *
 * ## 2. El selector de permisos tenía dos botones para lo mismo
 *
 * El botón del medio ya alterna: pone todo cuando no está todo y borra cuando está todo
 * (`chosen === total ? new Set() : todo`). El tercero hacía `new Set()` sin condición, o sea
 * exactamente lo mismo que el medio en el estado «todo seleccionado». Y su etiqueta era
 * `None` —la única en inglés de una pantalla en español— y además era la que se salía del
 * borde del modal.
 *
 * ## Por qué se leen los ficheros y no se monta nada
 *
 * Porque lo que se vigila son **contratos**: que el error no se pierda, que el `detail` viaje,
 * y que no haya dos acciones idénticas con etiquetas distintas. Eso se lee del fuente, y el test
 * corre sin navegador, sin red y en un segundo.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

// `src/features/api_access/` -> `frontend/` -> la raíz del repositorio: cuatro niveles.
const RAIZ = fileURLToPath(new URL('../../../../', import.meta.url))
const API = readFileSync(join(RAIZ, 'frontend/src/lib/api.ts'), 'utf8')
const RESUMEN = readFileSync(
  join(RAIZ, 'frontend/src/features/admin/AdminOverviewPage.tsx'),
  'utf8',
)
const SELECTOR = readFileSync(
  join(RAIZ, 'frontend/src/features/api_access/ScopeSelector.tsx'),
  'utf8',
)
const ES = JSON.parse(readFileSync(join(RAIZ, 'frontend/src/locales/es/apiAccess.json'), 'utf8'))
const EN = JSON.parse(readFileSync(join(RAIZ, 'frontend/src/locales/en/apiAccess.json'), 'utf8'))
const ADMIN_ES = JSON.parse(readFileSync(join(RAIZ, 'frontend/src/locales/es/admin.json'), 'utf8'))

describe('El fallo de la consola se puede diagnosticar', () => {
  it('el catch guarda el error en vez de tirarlo', () => {
    // El `catch` sin parámetro es el fallo: la pantalla no tenía nada que enseñar.
    expect(RESUMEN).not.toMatch(/\.catch\(\(\) => \{/)
    expect(RESUMEN).toMatch(/\.catch\(\(fallo: unknown\) => \{/)
  })

  it('ApiError lleva el detail del servidor', () => {
    // Sin esto, el error solo tenía un número y el `message` era la clave `api_request_failed`.
    expect(API).toMatch(/readonly detalle: string \| null/)
    expect(API).toMatch(/constructor\(status: number, detalle: string \| null = null\)/)
    // Y el cuerpo se lee antes de lanzar, porque una `Response` solo se consume una vez.
    const antes = API.indexOf('throw new ApiError(response.status, detalle)')
    const lectura = API.indexOf('await response.clone().json()')
    expect(lectura).toBeGreaterThan(-1)
    expect(lectura, 'el detail tiene que leerse antes del throw').toBeLessThan(antes)
  })

  it('cada motivo tiene su propio texto y su propia accion', () => {
    // Un `403` no se arregla reintentando. Con un solo texto para los cuatro casos, el operador
    // pulsa «Reintentar» y no pasa nada, que es lo que pasaba.
    for (const clave of [
      'failureNetwork',
      'failureSession',
      'failureForbidden',
      'failureServer',
      'failureUnknown',
    ]) {
      expect(ADMIN_ES.overview[clave], `falta overview.${clave} en español`).toBeTruthy()
    }
    // Y el estado HTTP se enseña, que es lo que distingue los casos entre sí.
    expect(ADMIN_ES.overview.failureStatus).toContain('{{status}}')
  })
})

describe('El selector de permisos no tiene dos botones para lo mismo', () => {
  it('el boton del medio ya alterna entre poner todo y borrar', () => {
    // Es lo que hace que un tercer boton de «borrar» sea un duplicado y no una conveniencia.
    expect(SELECTOR).toMatch(/chosen === total \? new Set\(\) : new Set\(/)
  })

  it('no queda ninguna etiqueta «None» en la interfaz', () => {
    // Se retiro el boton, no se tradujo: un boton que no hace falta, traducido, sigue sin hacer
    // falta.
    expect(SELECTOR).not.toContain('selectNone')
    expect(ES.scopes.selectNone).toBeUndefined()
    expect(EN.scopes.selectNone).toBeUndefined()
  })

  it('las acciones rapidas caben en dos botones', () => {
    // Tres enlaces con separadores verticales en una cabecera que se reparte con
    // `space-between` era lo que empujaba el último fuera del modal.
    const rapidas = /<div className="scope-quick-actions">([\s\S]*?)<\/div>/.exec(SELECTOR)
    expect(rapidas, 'no se encuentra el bloque de acciones rápidas').not.toBeNull()
    const botones = (rapidas![1].match(/className="link-button"/g) ?? []).length
    expect(botones).toBe(2)
  })
})

describe('Ninguna etiqueta del panel de API queda en otro idioma', () => {
  it('las etiquetas cortas de permisos están traducidas en los dos idiomas', () => {
    // `selectNone` era `None` en los dos: ni era inglés ni era español, era un texto olvidado.
    // La comprobación mira que no haya una etiqueta que sea una palabra inglesa suelta.
    const sospechosas = /^(None|Default|Defaults|Selected|Clear|Select|All|Search|Filter)$/
    for (const [idioma, datos] of [
      ['es', ES],
      ['en', EN],
    ] as const) {
      for (const [clave, valor] of Object.entries(datos.scopes as Record<string, unknown>)) {
        if (typeof valor !== 'string' || valor.includes('{{')) continue
        expect(
          valor,
          `scopes.${clave} en ${idioma} parece texto sin traducir`,
        ).not.toMatch(sospechosas)
      }
    }
  })
})
