/**
 * Lo que las dos pantallas comparten: el estado del trabajo y el de la insignia.
 *
 * ## Por qué vive aquí y no en cada página
 *
 * Porque las dos pintan el mismo estado y duplicar el mapa es garantizar que un día se
 * actualiza en una y no en la otra. Con la insignia duplicada, `/networks` acabaría enseñando
 * «En cola» mientras `/containers` enseña «Terminado», y nadie sabría cuál de las dos está
 * bien.
 *
 * Y son **datos**, no componentes: por eso van en un `.ts` y no en un `.tsx`. Un fichero que
 * mezcla los dos hace que el recarga en caliente del desarrollo deje de funcionar, porque
 * webpack invalidate el módulo entero cuando cambia cualquiera de las dos cosas. El aviso de
 * `oxlint` sobre esto no es cosmético: se pierde el estado del editor en cada cambio.
 *
 * ## Por qué el mapa es explícito y no `status.toLowerCase()`
 *
 * Porque el enum trae los valores en mayúsculas y las claves están en minúsculas, y con
 * `toLowerCase` funcionó hasta que el backend añadió un estado: la clave no existía, i18next
 * devolvió la propia ruta y la tabla pintó `jobs.NUEVOESTADO` en crudo. Sin error y sin que
 * ninguna prueba se enterara.
 *
 * Con un mapa, un estado nuevo que no se traduzca **no compila** al construir el objeto, que es
 * un fallo ruidoso en la revisión en vez de un texto crudo en producción.
 */

import type { AgentJobStatus } from '../../types/agents'

/** El estado del backend, y la clase de la insignia que lo pinta. */
export const CLASE_POR_ESTADO: Record<AgentJobStatus, string> = {
  COMPLETED: 'badge badge-success',
  FAILED: 'badge badge-error',
  QUEUED: 'badge badge-warning',
  CLAIMED: 'badge badge-info',
  RUNNING: 'badge badge-info',
}

/** Los estados, en el orden en que salen en el desplegable: de lo terminado a lo que espera. */
export const ESTADOS: readonly AgentJobStatus[] = [
  'COMPLETED',
  'FAILED',
  'RUNNING',
  'CLAIMED',
  'QUEUED',
]

/** La clave de traducción de un estado, sin adivinar. */
export function etiquetaDeEstado(clave: AgentJobStatus): string {
  return `states.${clave}`
}
