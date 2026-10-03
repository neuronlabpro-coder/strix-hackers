import { useEffect, useState } from 'react'

import { getVulnerabilities } from '../../lib/api'
import type {
  IssueStatus,
  SeverityCount,
  StatusCount,
  VulnerabilityListItem,
  VulnerabilitySeverity,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'

const SEVERITIES: VulnerabilitySeverity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']
const STATUSES: IssueStatus[] = [
  'OPEN',
  'IN_PROGRESS',
  'REMEDIATION_PROPOSED',
  'FIXED',
  'SNOOZED',
  'IGNORED',
]
const PAGE_SIZE = 25

export type IssuesViewMode = 'table' | 'board'

export interface IssuesQuery {
  severity: VulnerabilitySeverity | null
  status: IssueStatus | null
  search: string
  target: string
}

export interface IssuesState {
  items: VulnerabilityListItem[]
  /** Recuento por severidad de **todo** lo que casaba con el filtro. */
  severityTotals: Record<VulnerabilitySeverity, number>
  /** Recuento por estado de todo lo que casaba con el filtro. */
  statusTotals: Record<IssueStatus, number>
  /** El reparto ya en la forma que espera el gráfico, en el orden del modelo. */
  severityCounts: readonly SeverityCount[]
  statusCounts: readonly StatusCount[]
  total: number
  limit: number
  offset: number
  isLoading: boolean
  loadFailed: boolean
  query: IssuesQuery
  setQuery: (query: IssuesQuery) => void
  setPage: (offset: number) => void
  refresh: () => void
}

const EMPTY_QUERY: IssuesQuery = { severity: null, status: null, search: '', target: '' }

/**
 * Los recuentos vacíos, y son **objeto congelado**.
 *
 * Porque es el valor de reserva que se devuelve mientras llega la respuesta, y con un objeto
 * normal cada render de carga crearía uno nuevo: el `useMemo` del esquema de la gráfica
 * depende de él, se invalidaría en cada render y la memoización no memoizaría nada. Con
 * `Object.freeze` además el fallo de escribir en él sale en modo estricto en vez de
 * corromper el estado en silencio.
 */
const SEVERITY_TOTALS_VACIOS: Record<VulnerabilitySeverity, number> = Object.freeze({
  CRITICAL: 0,
  HIGH: 0,
  MEDIUM: 0,
  LOW: 0,
  INFO: 0,
})

const STATUS_TOTALS_VACIOS: Record<IssueStatus, number> = Object.freeze({
  OPEN: 0,
  IN_PROGRESS: 0,
  REMEDIATION_PROPOSED: 0,
  FIXED: 0,
  SNOOZED: 0,
  IGNORED: 0,
})

const SEVERITY_COUNTS_VACIAS: readonly SeverityCount[] = Object.freeze(
  SEVERITIES.map((severity) => Object.freeze({ severity, total: 0 })),
)

const STATUS_COUNTS_VACIAS: readonly StatusCount[] = Object.freeze(
  STATUSES.map((status) => Object.freeze({ status, total: 0 })),
)

/**
 * Resultado de la ultima consulta resuelta. `key` identifica la peticion que
 * produjo estos datos: mientras no coincida con la peticion actual, la vista
 * esta cargando. Asi el estado de carga se deriva durante el render y el efecto
 * solo sincroniza con la API.
 */
interface IssuesResult {
  key: string
  items: VulnerabilityListItem[]
  severityTotals: Record<VulnerabilitySeverity, number>
  statusTotals: Record<IssueStatus, number>
  severityCounts: readonly SeverityCount[]
  statusCounts: readonly StatusCount[]
  total: number
  limit: number
  failed: boolean
}

export function useIssues(): IssuesState {
  const { token, selectedOrganizationId } = useAuth()
  const [offset, setOffset] = useState(0)
  const [query, setQueryState] = useState<IssuesQuery>(EMPTY_QUERY)
  const [reloadToken, setReloadToken] = useState(0)
  const [result, setResult] = useState<IssuesResult>({
    key: '',
    items: [],
    severityTotals: SEVERITY_TOTALS_VACIOS,
    statusTotals: STATUS_TOTALS_VACIOS,
    severityCounts: SEVERITY_COUNTS_VACIAS,
    statusCounts: STATUS_COUNTS_VACIAS,
    total: 0,
    limit: PAGE_SIZE,
    failed: false,
  })

  const requestKey = JSON.stringify([token, selectedOrganizationId, offset, query, reloadToken])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getVulnerabilities(token, selectedOrganizationId, {
      limit: PAGE_SIZE,
      offset,
      ...(query.severity ? { severity: query.severity } : {}),
      ...(query.status ? { status: query.status } : {}),
      ...(query.search ? { search: query.search } : {}),
      ...(query.target ? { target: query.target } : {}),
    })
      .then((page) => {
        if (!isActive) {
          return
        }
        // Los recuentos vienen del servidor y cuentan todo el conjunto filtrado, no la página.
        // Antes se contaban aquí sobre `page.items`, que son veinticinco filas: con más
        // hallazgos que eso, el reparto que pintaban las píldoras de severidad era el de la
        // última página, ySayendo del mismo dato que el que enseña el gráfico.
        const severityCounts = page.severity_breakdown ?? []
        const statusCounts = page.status_breakdown ?? []
        setResult({
          key: requestKey,
          items: page.items,
          severityTotals: totalesPorClave(severityCounts, 'severity', SEVERITY_TOTALS_VACIOS),
          statusTotals: totalesPorClave(statusCounts, 'status', STATUS_TOTALS_VACIOS),
          severityCounts,
          statusCounts,
          total: page.total,
          limit: page.limit,
          failed: false,
        })
      })
      .catch(() => {
        if (isActive) {
          setResult((current) => ({ ...current, key: requestKey, failed: true }))
        }
      })
    return () => {
      isActive = false
    }
  }, [offset, query, reloadToken, requestKey, selectedOrganizationId, token])

  return {
    items: isCurrent ? result.items : [],
    severityTotals: isCurrent ? result.severityTotals : SEVERITY_TOTALS_VACIOS,
    statusTotals: isCurrent ? result.statusTotals : STATUS_TOTALS_VACIOS,
    severityCounts: isCurrent ? result.severityCounts : SEVERITY_COUNTS_VACIAS,
    statusCounts: isCurrent ? result.statusCounts : STATUS_COUNTS_VACIAS,
    total: isCurrent ? result.total : 0,
    limit: isCurrent ? result.limit : PAGE_SIZE,
    offset,
    isLoading: Boolean(token && selectedOrganizationId) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    query,
    setQuery: (next: IssuesQuery) => {
      setQueryState(next)
      setOffset(0)
    },
    setPage: (nextOffset: number) => setOffset(Math.max(0, nextOffset)),
    refresh: () => setReloadToken((current) => current + 1),
  }
}

/**
 * Pasa una lista de conteos del servidor a un `Record` con todas las claves.
 *
 * ## Por qué se rellenan las claves que faltan
 *
 * Porque la respuesta no trae filas para los valores que no existen —un `GROUP BY` solo
 * devuelve lo que hay— y quien pinta la rampa necesita las cinco filas siempre. Con
 * `severity_breakdown` sin la fila `CRITICAL` porque no hay ninguno, la barra desaparecía y
 * el hueco que dejaba parecía un fallo de render en vez de «cero de críticos».
 *
 * ## Por qué se comprueba `in` en vez de castear
 *
 * Porque el `Record` devuelto tiene que tener exactamente las claves del dominio. Con un
 * `as Record<TClave, number>` ciego, una severidad que el servidor invente mañana —`BLOCKER`,
 * decir— se colaría en el objeto y las píldoras del filtro la sumarían sin haber un color
 * asignado. Descartarla aquí es la diferencia entre un dato que no se ve y un dato que
 * rompe la pantalla.
 */
function totalesPorClave<TClave extends string, TConteo extends object>(
  conteos: readonly TConteo[],
  clave: string,
  vacios: Record<TClave, number>,
): Record<TClave, number> {
  const totales: Record<TClave, number> = { ...vacios }
  for (const conteo of conteos) {
    const valor = (conteo as Record<string, unknown>)[clave]
    const total = (conteo as Record<string, unknown>).total
    if (typeof valor === 'string' && valor in totales && typeof total === 'number') {
      totales[valor as TClave] = total
    }
  }
  return totales
}

export { SEVERITIES, STATUSES, PAGE_SIZE }