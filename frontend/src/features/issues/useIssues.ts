import { useEffect, useState } from 'react'

import { getVulnerabilities } from '../../lib/api'
import type { IssueStatus, VulnerabilityListItem, VulnerabilitySeverity } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const SEVERITIES: VulnerabilitySeverity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']
const STATUSES: IssueStatus[] = ['OPEN', 'IN_PROGRESS', 'FIXED', 'SNOOZED', 'IGNORED']
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
  severityTotals: Record<VulnerabilitySeverity, number>
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

const EMPTY_TOTALS: Record<VulnerabilitySeverity, number> = {
  CRITICAL: 0,
  HIGH: 0,
  MEDIUM: 0,
  LOW: 0,
  INFO: 0,
}

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
    severityTotals: EMPTY_TOTALS,
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
        setResult({
          key: requestKey,
          items: page.items,
          severityTotals: countBySeverity(page.items),
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
    severityTotals: isCurrent ? result.severityTotals : EMPTY_TOTALS,
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

function countBySeverity(
  findings: VulnerabilityListItem[],
): Record<VulnerabilitySeverity, number> {
  const totals: Record<VulnerabilitySeverity, number> = { ...EMPTY_TOTALS }
  for (const finding of findings) {
    totals[finding.severity] += 1
  }
  return totals
}

export { SEVERITIES, STATUSES, PAGE_SIZE }
