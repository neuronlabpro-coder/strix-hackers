/**
 * La insignia de estado de un trabajo, compartida por las dos pantallas.
 *
 * ## Por qué es un componente y no una función que devuelve JSX
 *
 * Porque necesita `useTranslation`, y una función que devuelve JSX con un hook tiene que
 * ser un componente de todas formas. Declararla en `estados.ts` obligaría a ese fichero —que
 * solo tiene datos— a ser un `.tsx` con un import de React que no usa.
 *
 * Y es lo único que queda aquí: el mapa de clases, la lista de estados y el nombre de la clave
 * viven en `estados.ts`, que es donde se editan cuando el backend añade un estado.
 */

import { useTranslation } from 'react-i18next'

import type { AgentJob } from '../../types/agents'
import { CLASE_POR_ESTADO, etiquetaDeEstado } from './estados'

export function StatusBadge({ job }: { job: AgentJob }) {
  const { t } = useTranslation('agents')
  return <span className={CLASE_POR_ESTADO[job.status]}>{t(etiquetaDeEstado(job.status))}</span>
}
