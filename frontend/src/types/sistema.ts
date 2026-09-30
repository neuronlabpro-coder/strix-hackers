/**
 * El sistema para el que se despliega el agente, y cómo se normaliza.
 *
 * ## Por qué esta normalización existe
 *
 * Porque la columna llegó a pintar una clave en crudo en la tabla: `agent.system.undefined.name`.
 * La causa fue que la respuesta de la API no traía el campo —un backend de antes del cambio— y el
 * `undefined` se metió en el nombre de la clave.
 *
 * ## Por qué un `switch` y no un `?? 'desconocido'`
 *
 * Porque `??` solo cubre `null` y `undefined`, que son los dos valores que un backend viejo
 * devuelve. No cubre `''`, ni `'Win2022'`, ni `'WINDOWS'`, ni un número. Cada uno de esos
 * nombres se habría translated a una clave que no existe, y el síntoma —una fila con
 * `agent.system.WINDOWS.name` en pantalla— es el mismo que el de la clave sin traducir, que es
 * justo el defecto que este módulo viene a quitar.
 *
 * ## Por qué se declara en un fichero propio y no dentro del componente
 *
 * Porque lo usan la tabla, el modal y la guía, y porque necesita una prueba propia: una función
 * de normalización sin la lista de valores válidos al lado es la forma más corta de que el
 * próximo sistema que se añada se quede sin traducir sin que nadie se entere.
 */

import type { SistemaObjetivo } from './agents'

/** Los valores que el backend acepta. En el mismo orden que el enum de `agents/schemas.py`. */
export const SISTEMAS: readonly SistemaObjetivo[] = [
  'linux',
  'windows',
  'macos',
  'desconocido',
]

/** El valor de la API, o `undefined` si el backend es más antiguo que el campo. */
export type SistemaDeLaApi = SistemaObjetivo | string | null | undefined

/**
 * Devuelve un sistema conocido, o `desconocido` si lo que llega no lo es.
 *
 * ## Por qué `desconocido` y no un error
 *
 * Porque esta función decide **qué se enseña**, no si los datos son válidos. Un sistema
 * desconocido es una fila antigua, un backend viejo o un dato escrito a mano, y en los tres
 * casos la respuesta útil es «no lo sé, te enseño la variante que funciona en todos», no un
 * `throw` que tumba la tabla entera por una celda.
 */
export function normalizaSistema(valor: SistemaDeLaApi): SistemaObjetivo {
  if (typeof valor !== 'string') return 'desconocido'
  const minusculas = valor.trim().toLowerCase()
  return (SISTEMAS as readonly string[]).includes(minusculas)
    ? (minusculas as SistemaObjetivo)
    : 'desconocido'
}
