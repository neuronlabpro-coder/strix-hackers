/**
 * Validación de frontmatter OKF en el navegador, y composición del mismo desde un formulario.
 *
 * ## Por qué se valida aquí y no solo en el backend
 *
 * Porque el backend ya responde con un error que **nombra el campo que falta**, y eso llega
 * después de que el usuario ha escrito varios cientos de líneas y pulsado guardar. Validar en
 * el navegador convierte un error de envío en un aviso mientras se escribe, que es cuando
 * todavía se puede arreglar sin perder nada.
 *
 * ## Por qué se parece al parser del backend y no es una reimplementación libre
 *
 * Porque el formato OKF tiene una especificación y el backend la aplica —`parse_okf`, en
 * `backend/apps/knowledge/okf.py`—. Un validador que acepta lo que el backend rechaza produce un
 * documento que pasa la validación en pantalla y falla al guardar; y uno que rechaza lo que el
 * backend acepta hace lo contrario. Las dos reglas están duplicadas a propósito, y la razon
 * está escrita en el backend: la especificación del formato manda, no la implementación.
 *
 * Lo que **no** se replica es el análisis de Markdown del cuerpo. El backend es deliberadamente
 * permisivo ahí —un enlace roto se descarta solo— y comprobarlo en el navegador convertiría el
 * editor en algo que rechaza documentos válidos.
 */

/** Los cuatro `type` que acepta el frontmatter, en la grafía de la especificación. */
export const OKF_TYPES = ['concept', 'business_rule', 'api_spec', 'architecture'] as const
export type OkfType = (typeof OKF_TYPES)[number]

/**
 * Del `type` de la especificación al valor del enum de la columna.
 *
 * El mapeo vive **aquí** y no en el backend, porque es una traducción de vocabulario de formato a
 * vocabulario de base de datos y solo el navegador la necesita: quien llama a la API manda
 * `doc_type` en el enum, y el `content` lleva el `type` en la grafía del formato.
 */
export const TIPO_OKF_A_ENUM: Record<OkfType, string> = {
  concept: 'DOCUMENTATION',
  business_rule: 'BUSINESS_RULE',
  api_spec: 'API_SPEC',
  architecture: 'ARCHITECTURE',
}

export const ENUM_A_TIPO_OKF: Record<string, OkfType> = {
  DOCUMENTATION: 'concept',
  BUSINESS_RULE: 'business_rule',
  API_SPEC: 'api_spec',
  ARCHITECTURE: 'architecture',
}

export interface Frontmatter {
  type?: string
  title?: string
  description?: string
  tags?: string[]
}

/** Un campo que no cumple, con lo que se le pide. El mensaje es lo que se muestra. */
export interface ProblemaFrontmatter {
  campo: 'type' | 'title' | 'description' | 'tags' | 'formato' | 'cuerpo'
  mensaje: string
}

const CLAVE: RegExp = /^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$/
const ETIQUETA: RegExp = /"([^"]+)"|([A-Za-z0-9_./+-]+)/g

/** Escapa un valor para escribirlo entre comillas en el frontmatter. */
function entrecomillado(valor: string): string {
  return `"${valor.replace(/"/g, '\\"')}"`
}

/**
 * Divide un documento en su frontmatter y su cuerpo.
 *
 * Devuelve `null` en el frontmatter cuando no hay valla de apertura, que es el caso en el que
 * el error no es de un campo concreto sino de formato.
 */
export function dividirDocumento(contenido: string): {
  frontmatterLineas: string[]
  cuerpo: string
  tieneFrontmatter: boolean
  cerrado: boolean
} {
  const sinBom = contenido.replace(/^﻿/, '')
  if (!/^---[ \t]*\r?\n/.test(sinBom)) {
    return { frontmatterLineas: [], cuerpo: sinBom, tieneFrontmatter: false, cerrado: false }
  }
  const trasApertura = sinBom.replace(/^---[ \t]*\r?\n/, '')
  const corte = /\r?\n---[ \t]*(?:\r?\n|$)/.exec(trasApertura)
  if (corte === null) {
    return {
      frontmatterLineas: trasApertura.split('\n'),
      cuerpo: '',
      tieneFrontmatter: true,
      cerrado: false,
    }
  }
  return {
    frontmatterLineas: trasApertura.slice(0, corte.index).split('\n'),
    cuerpo: trasApertura.slice(corte.index + corte[0].length).trim(),
    tieneFrontmatter: true,
    cerrado: true,
  }
}

/** Lee los pares `clave: valor` de la cabecera. */
export function leerFrontmatter(lineas: string[]): Frontmatter {
  const campos: Record<string, string> = {}
  let claveActual: string | null = null
  for (const cruda of lineas) {
    if (cruda.trim() === '' || cruda.trimStart().startsWith('#')) continue
    const emparejada = CLAVE.exec(cruda)
    if (emparejada !== null) {
      claveActual = emparejada[1]
      if (claveActual !== null) campos[claveActual] = emparejada[2] ?? ''
      continue
    }
    // Una linea mas profunda continua el valor anterior: es como se escribe un `tags:` en
    // varias lineas.
    if (claveActual !== null && /^\s/.test(cruda)) {
      campos[claveActual] = `${campos[claveActual] ?? ''} ${cruda.trim()}`.trim()
    }
  }
  return {
    type: campos.type,
    title: campos.title,
    description: campos.description,
    tags: parsearEtiquetas(campos.tags ?? ''),
  }
}

function parsearEtiquetas(bruto: string): string[] {
  let limpio = bruto.trim()
  if (limpio.startsWith('[') && limpio.endsWith(']')) limpio = limpio.slice(1, -1)
  const salida: string[] = []
  ETIQUETA.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = ETIQUETA.exec(limpio)) !== null) {
    const valor = m[1] ?? m[2]
    if (valor) salida.push(valor.toLowerCase())
  }
  return Array.from(new Set(salida))
}

/**
 * Valida un documento OKF completo.
 *
 * Devuelve **todos** los problemas, no el primero: quien escribe un frontmatter desde cero
 * suele tener tres cosas mal y descubrirlo de una en una obliga a tres viajes de ida y vuelta.
 */
export function validarOkf(contenido: string): ProblemaFrontmatter[] {
  const problemas: ProblemaFrontmatter[] = []
  const { frontmatterLineas, cuerpo, tieneFrontmatter, cerrado } = dividirDocumento(contenido)

  if (!tieneFrontmatter) {
    problemas.push({
      campo: 'formato',
      mensaje: 'okf.missingFrontmatter',
    })
    return problemas
  }
  if (!cerrado) {
    problemas.push({ campo: 'formato', mensaje: 'okf.unclosedFrontmatter' })
    return problemas
  }

  const campos = leerFrontmatter(frontmatterLineas)

  const tipo = (campos.type ?? '').trim().toLowerCase()
  if (tipo === '') {
    problemas.push({ campo: 'type', mensaje: 'okf.missingType' })
  } else if (!(OKF_TYPES as readonly string[]).includes(tipo)) {
    problemas.push({ campo: 'type', mensaje: 'okf.invalidType' })
  }

  if ((campos.title ?? '').trim() === '') {
    problemas.push({ campo: 'title', mensaje: 'okf.missingTitle' })
  }
  if ((campos.description ?? '').trim() === '') {
    problemas.push({ campo: 'description', mensaje: 'okf.missingDescription' })
  }
  if (cuerpo.trim() === '') {
    problemas.push({ campo: 'cuerpo', mensaje: 'okf.emptyBody' })
  }

  return problemas
}

/**
 * Compone un documento OKF a partir de los campos del formulario.
 *
 * La composición vive en el cliente y no en el backend a propósito: el backend recibe el
 * documento entero y no tiene por qué saber que hay un formulario. Quien use la API a mano
 * escribe su frontmatter; quien usa el panel lo tiene compuesto, y los dos terminan con el
 * mismo formato.
 */
export function componerOkf(campos: {
  type: OkfType
  title: string
  description: string
  tags: string[]
  body: string
}): string {
  const lineas = [
    '---',
    `type: ${campos.type}`,
    `title: ${entrecomillado(campos.title)}`,
    `description: ${entrecomillado(campos.description)}`,
  ]
  if (campos.tags.length > 0) {
    lineas.push(`tags: [${campos.tags.map(entrecomillado).join(', ')}]`)
  }
  lineas.push('---', '', campos.body.trim(), '')
  return lineas.join('\n')
}

/** Extrae de un documento lo que el formulario necesita, para editarlo sin perder el cuerpo. */
export function extraerParaFormulario(contenido: string): {
  type: OkfType
  title: string
  description: string
  tags: string[]
  body: string
} {
  const { frontmatterLineas, cuerpo } = dividirDocumento(contenido)
  const campos = leerFrontmatter(frontmatterLineas)
  const tipo = (campos.type ?? '').trim().toLowerCase() as OkfType
  return {
    type: (OKF_TYPES as readonly string[]).includes(tipo) ? tipo : 'concept',
    title: campos.title ?? '',
    description: campos.description ?? '',
    tags: campos.tags ?? [],
    body: cuerpo,
  }
}
