/**
 * El sistema del agente, y que en pantalla salga su nombre y no una clave.
 *
 * ## Qué defecto fija
 *
 * El de la tabla de agentes: la columna de sistema pintaba la ruta de la clave, tal cual,
 * `agent.system.undefined.name`. Dos cosas de la misma fila cayendo a la vez, y solo se
 * arreglaba una.
 *
 * 1. **La normalización no cubría todos los valores.** Con `valor ?? 'desconocido'` solo se
 *    cubrían `null` y `undefined`. Un backend que devuelve `''`, o un `Win2022` escrito a mano,
 *    o `'WINDOWS'` en mayúsculas, se habrían colado igual.
 * 2. **El enum del backend y el tipo del frontend no estaban atados.** Nada impedía añadir
 *    `freebsd` al enum de Python sin que existiera su traducción, y el síntoma sería
 *    `agent.system.freebsd.name` en la tabla de un cliente. Ese caso se comprueba aquí leyendo
 *    el fichero del backend, que es lo único que puede decir la verdad.
 *
 * ## Por qué se lee el backend y no una lista escrita aquí
 *
 * Porque una lista escrita a mano es una lista que se queda corta. El enum de Python es la
 * fuente, y leerlo cuesta cuatro líneas; mantenerlo en dos sitios cuesta un bug.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { SISTEMAS, normalizaSistema } from './sistema'

const RAIZ = fileURLToPath(new URL('../../../', import.meta.url))
const ES = JSON.parse(
  readFileSync(join(RAIZ, 'frontend/src/locales/es/agents.json'), 'utf8'),
) as { agent: { system: Record<string, { name: string; hint: string }> } }
const EN = JSON.parse(
  readFileSync(join(RAIZ, 'frontend/src/locales/en/agents.json'), 'utf8'),
) as { agent: { system: Record<string, { name: string; hint: string }> } }
const SCHEMAS = readFileSync(join(RAIZ, 'backend/apps/agents/schemas.py'), 'utf8')

describe('El sistema del agente se normaliza y se traduce', () => {
  it('cada sistema del enum del backend tiene nombre y pista en los dos idiomas', () => {
    const delBackend = [...SCHEMAS.matchAll(/^\s{4}([A-Z_]+) = "([a-z]+)"$/gm)].map((m) => m[2])
    expect(delBackend.length, 'no se ha encontrado el enum del backend').toBeGreaterThan(0)

    const faltan: string[] = []
    for (const sistema of delBackend) {
      if (!SISTEMAS.includes(sistema as never)) {
        faltan.push(`${sistema} esta en el enum del backend y no en la lista del frontend`)
      }
      for (const [idioma, datos] of [
        ['es', ES],
        ['en', EN],
      ] as const) {
        const entrada = datos.agent.system[sistema]
        if (!entrada) faltan.push(`${sistema} no tiene traducción en ${idioma}`)
        else if (!entrada.name) faltan.push(`${sistema}.name esta vacia en ${idioma}`)
        else if (!entrada.hint) faltan.push(`${sistema}.hint esta vacia en ${idioma}`)
      }
    }
    expect(faltan).toEqual([])
  })

  it('los cuatro valores que el backend manda llegan sin tocarse', () => {
    // El camino feliz: si aquí un valor se cambiara, la normalización estaría haciendo algo que
    // no debería, y un sistema con tilde o con guion se quedaría en `desconocido` sin que nadie lo
    // notase.
    for (const sistema of SISTEMAS) {
      expect(normalizaSistema(sistema)).toBe(sistema)
    }
  })

  it('un sistema escrito con otras mayusculas se acepta, no se descarta', () => {
    // ## Por qué se acepta y no cae en «desconocido»
    //
    // Porque `WINDOWS` y `windows` son el mismo sistema, y convertirlo en «desconocido»
    // devolvería al operador a las instrucciones de Linux por escribir las mayúsculas. El
    // objetivo de la normalización es que no salga una clave rota, no rechazar lo que se puede
    // entender. `lowerCase` es la pieza que hace eso sin adivinar nada.
    expect(normalizaSistema('WINDOWS')).toBe('windows')
    expect(normalizaSistema('Windows')).toBe('windows')
    expect(normalizaSistema('MacOS')).toBe('macos')
  })

  it('un valor con espacios a los lados se acepta, porque es el mismo sistema', () => {
    // Una base de datos con `CHAR` en vez de `VARCHAR` devuelve `linux   ` con relleno. Sin el
    // `trim()`, ese agente acababa con `agent.system.linux.name` en la tabla.
    expect(normalizaSistema('  linux  ')).toBe('linux')
  })

  it('todo lo que no es un sistema conocido cae en desconocido, no en una clave rota', () => {
    // Los casos que un `?? 'desconocido'` no habría cogido. El primero es el que se vio en
    // pantalla; el resto son los que llegan si alguien escribe el dato a mano, si un backend
    // futuro renombra el valor o si el campo llega con un tipo que no es una cadena.
    const raros = [
      undefined,
      null,
      '',
      '   ',
      'Windows Server 2022',
      'Win2022',
      'freebsd',
      42,
      true,
      {},
      [],
    ] as unknown as Parameters<typeof normalizaSistema>[0][]

    for (const valor of raros) {
      expect(normalizaSistema(valor), `«${String(valor)}» debería caer en desconocido`).toBe(
        'desconocido',
      )
    }
  })

  it('la lista de sistemas no tiene duplicados ni huecos', () => {
    expect(new Set(SISTEMAS).size).toBe(SISTEMAS.length)
    expect(SISTEMAS).toContain('desconocido')
  })
})
