/**
 * Toda clave que se pide en el código existe en español y en inglés, y los dos idiomas
 * enseñan lo mismo.
 *
 * ## Qué defecto fija
 *
 * El que se vio en la captura: la tabla pintaba `jobs.QUEUED` en crudo. El enum trae los
 * estados en mayúsculas y las claves estaban en minúsculas, así que la traducción nunca se
 * buscaba. Salió un texto crudo en pantalla y ninguna prueba se enteró, porque **no había
 * ninguna prueba que comprobara que las claves existen**.
 *
 * Y la causa de fondo era más amplia que ese estado: en este módulo el idioma estaba puesto en
 * el sitio equivocado dos veces. Una en el `toLowerCase()` que adivinaba la clave en vez de
 * declararla. Y otra, más sutil, en los motivos del agente: el agente escribía su motivo en
 * español y la plataforma lo pintaba tal cual, de modo que la pantalla en inglés leía el
 * motivo de un escaneo en el idioma del agente. Por eso el agente manda ahora un **código**
 * junto al texto, y este test vigila que cada código tenga su traducción.
 *
 * ## Por qué se lee el código como texto y no se importa el componente
 *
 * Porque montar el componente exigiría un token, una organización y una red, y un test que
 * necesita tres identificadores para comprobar que existe una clave es un test que se acaba
 * de identificadores para comprobar que existe una clave es un test que se acaba quedando
 * a medias. Lo que se comprueba aquí es una **propiedad de los ficheros**: cada
 * `t('...')` tiene su clave en los dos idiomas. Se lee el fuente porque es lo que hay, y
 * porque leerlo es lo que permite que este test corra sin navegador, sin red y en un segundo.
 *
 * ## Por qué las formas no cuentan como claves
 *
 * `` t(`${espacio}.form.title`) `` no es una clave: es una forma, y las dos ramas se comprueban
 * por separado. Si se contaran como una, el test passaría con `espacio` sin comprobar ninguna de
 * las dos, que es exactamente el fallo que هذا test existe para cazar.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const AQUI = dirname(fileURLToPath(import.meta.url))
const BASE = join(AQUI, '..', '..')
const LOCALES = join(BASE, 'locales')

/** Los estados del enum del backend. Si se añade uno, esta lista tiene que crecer. */
const ESTADOS = ['QUEUED', 'CLAIMED', 'RUNNING', 'COMPLETED', 'FAILED'] as const

/** Los dos espacios de trabajo, porque las dos pantallas son las que piden las claves. */
const ESPACIOS = ['containers', 'networks'] as const

type Json = Record<string, unknown>

function leerJson(ruta: string): Json {
  return JSON.parse(readFileSync(ruta, 'utf8')) as Json
}

/** Todas las claves de un objeto de traducciones, en notación de punto. */
function clavesPlanas(nodo: unknown, prefijo = ''): string[] {
  if (nodo === null || typeof nodo !== 'object' || Array.isArray(nodo)) {
    return [prefijo]
  }
  return Object.entries(nodo as Json).flatMap(([clave, valor]) =>
    clavesPlanas(valor, prefijo ? `${prefijo}.${clave}` : clave)
  )
}

/** Las claves de los locales que vienen del namespace `common`. */
const CLAVES_DE_COMUN = ['common:cancel', 'common:close', 'common:status']

const es = leerJson(join(LOCALES, 'es', 'agents.json'))
const en = leerJson(join(LOCALES, 'en', 'agents.json'))
const clavesEs = new Set(clavesPlanas(es))
const clavesEn = new Set(clavesPlanas(en))

/** Los `.ts` y `.tsx` del módulo de agentes, **sin los tests**.
 *
 * El propio test queda fuera a propósito: su título es `` cada `t("clave")` literal del
 * código existe `` y eso no es una clave sino el nombre de un caso. Si se escaneara a sí
 * mismo, fallaría siempre y no por un defecto del módulo.
 */
function fuentesDeAgentes(): Array<{ nombre: string; texto: string }> {
  const encontrados: Array<{ nombre: string; texto: string }> = []
  for (const entrada of readdirSync(AQUI)) {
    if (!entrada.endsWith('.ts') && !entrada.endsWith('.tsx')) continue
    if (entrada.endsWith('.test.ts') || entrada.endsWith('.test.tsx')) continue
    const ruta = join(AQUI, entrada)
    if (!statSync(ruta).isFile()) continue
    encontrados.push({
      nombre: entrada,
      texto: readFileSync(ruta, 'utf8').replace(/\r\n/g, '\n'),
    })
  }
  return encontrados
}

const fuentes = fuentesDeAgentes()

/** Los valores de la clave, por idioma, para comprobar que no están vacíos ni son la clave. */
function valorDe(nodo: unknown, clave: string): unknown {
  return clave
    .split('.')
    .reduce<unknown>(
      (actual, parte) =>
        actual !== null && typeof actual === 'object'
          ? (actual as Json)[parte]
          : undefined,
      nodo
    )
}

describe('Las claves de los agentes existen y están en los dos idiomas', () => {
  it('el módulo tiene fuentes que comprobar', () => {
    // Sin esto, un `readdirSync` que no encuentra la carpeta deja el resto del test en verde
    // porque no hay nada que comparar: cero casos comprobados es un test que no comprueba.
    expect(fuentes.length).toBeGreaterThanOrEqual(7)
  })

  it('español e inglés tienen exactamente las mismas claves', () => {
    const soloEs = [...clavesEs].filter((clave) => !clavesEn.has(clave))
    const soloEn = [...clavesEn].filter((clave) => !clavesEs.has(clave))
    expect({ soloEs, soloEn }).toEqual({ soloEs: [], soloEn: [] })
  })

  it('ninguna traducción está vacía ni es su propia clave', () => {
    // Una cadena vacía se pinta como hueco, y i18next devuelve la propia ruta cuando falta la
    // clave: las dos cosas son el mismo defecto visto desde el otro lado.
    const vacias: string[] = []
    for (const [nombre, documento] of [
      ['es', es],
      ['en', en],
    ] as const) {
      for (const clave of clavesPlanas(documento)) {
        const valor = valorDe(documento, clave)
        if (typeof valor !== 'string' || valor.trim().length === 0 || valor === clave) {
          vacias.push(`${nombre}:${clave}`)
        }
      }
    }
    expect(vacias).toEqual([])
  })

  it('cada `t("clave")` literal del código existe en ambos idiomas', () => {
    const literales = fuentes.flatMap(({ nombre, texto }) =>
      [...texto.matchAll(/\bt\(\s*['"]([a-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)['"]/g)].map(
        (coincidencia) => `${nombre}: ${coincidencia[1]}`
      )
    )
    // El test tiene que estar mirando algo: si el patrón no engancha nada, todas las
    // comprobaciones de abajo pasarían sin comprobar nada.
    expect(literales.length).toBeGreaterThan(80)

    const faltan: string[] = []
    for (const entrada of literales) {
      const clave = entrada.slice(entrada.indexOf(': ') + 2)
      if (CLAVES_DE_COMUN.includes(clave)) continue
      if (!clavesEs.has(clave)) faltan.push(`${entrada} (falta en es)`)
      if (!clavesEn.has(clave)) faltan.push(`${entrada} (falta en en)`)
    }
    expect(faltan).toEqual([])
  })

  it('las dos formas con `${espacio}` existen en los dos espacios', () => {
    // Aquí es donde se cazaría el otro fallo: si `networks.form.title` existiera y
    // `containers.form.title` no, la pantalla de contenedores mostraría la ruta de la clave.
    const formas = fuentes.flatMap(({ nombre, texto }) =>
      [...texto.matchAll(/`\$\{espacio\}([A-Za-z0-9_.]+)`/g)].map(
        (coincidencia) => `${nombre}: ${coincidencia[1]}`
      )
    )
    // Trece: siete del formulario y seis del detalle. Si baja, alguien ha movido las formas
    // fuera del módulo y este test ha dejado de mirar la mitad de lo que se pinta.
    expect(formas.length).toBeGreaterThanOrEqual(13)

    const faltan: string[] = []
    for (const entrada of formas) {
      const cola = entrada.slice(entrada.indexOf(': ') + 2)
      for (const espacio of ESPACIOS) {
        const clave = espacio + cola
        if (!clavesEs.has(clave)) faltan.push(`${entrada} -> ${clave} (falta en es)`)
        if (!clavesEn.has(clave)) faltan.push(`${entrada} -> ${clave} (falta en en)`)
      }
    }
    expect(faltan).toEqual([])
  })

  it('los cinco estados del enum están traducidos en los dos idiomas', () => {
    const faltan = ESTADOS.filter(
      (estado) => !clavesEs.has(`states.${estado}`) || !clavesEn.has(`states.${estado}`)
    )
    expect(faltan).toEqual([])
  })

  it('cada código de motivo del agente tiene su traducción', () => {
    // El agente manda `codigo_*` junto al texto del motivo. Si aquí aparece un código sin
    // traducir, la pantalla enseñará el texto del agente —que viene en el idioma del agente— en
    // un panel que puede estar en otro. Es el mismo defecto de antes, disfrazado.
    const motivos = fuentes.find((f) => f.nombre === 'motivos.ts')
    expect(motivos).toBeDefined()
    const codigos = [...(motivos?.texto ?? '').matchAll(/^\s{2}([a-z_]+):\s*'limits\./gm)].map(
      (coincidencia) => coincidencia[1]
    )
    expect(codigos.length).toBeGreaterThanOrEqual(4)

    const faltan: string[] = []
    for (const codigo of codigos) {
      const clave = /'([^']+)'/.exec(
        (motivos?.texto ?? '').split(`${codigo}:`)[1]?.split('\n')[0] ?? ''
      )?.[1]
      if (!clave) continue
      if (!clavesEs.has(clave)) faltan.push(`${codigo} -> ${clave} (falta en es)`)
      if (!clavesEn.has(clave)) faltan.push(`${codigo} -> ${clave} (falta en en)`)
    }
    expect(faltan).toEqual([])
  })
})
