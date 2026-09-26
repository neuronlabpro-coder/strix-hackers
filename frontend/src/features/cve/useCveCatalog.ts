import { useEffect, useState } from 'react'

import { getCVEYears, getTrendingKEV, searchCVE } from '../../lib/api'
import type { CVESearchParams, CVESeverity, CVEPage } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const PAGE_SIZE = 25
const TRENDING_LIMIT = 8
// Un año de CVE antes de 1999 no existe: la lista de navegación empieza donde el
// identificador puede empezar, y no se pide un rango que devolvería años vacíos.
const FIRST_CVE_YEAR = 1999

export const SEVERITIES: CVESeverity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

export interface CveFilters {
  /** Texto ya aplicado al backend, no el que se está escribiendo. */
  query: string
  severity: CVESeverity | null
  isKevOnly: boolean
  year: number | null
}

export interface CveState {
  page: CVEPage | null
  trending: CVEPage | null
  years: number[]
  isLoading: boolean
  loadFailed: boolean
  filters: CveFilters
  setFilters: (filters: CveFilters) => void
  clearFilters: () => void
  hasFilters: boolean
  refresh: () => void
}

const EMPTY_FILTERS: CveFilters = {
  query: '',
  severity: null,
  isKevOnly: false,
  year: null,
}

const EMPTY_PAGE: CVEPage = { items: [], total: 0, limit: PAGE_SIZE, offset: 0 }

export function useCveCatalog(): CveState {
  const { token, selectedOrganizationId } = useAuth()
  const [filters, setFiltersState] = useState<CveFilters>(EMPTY_FILTERS)
  const [reloadToken, setReloadToken] = useState(0)
  const [result, setResult] = useState<{
    key: string
    page: CVEPage
    trending: CVEPage
    years: number[]
    failed: boolean
  }>({
    key: '',
    page: EMPTY_PAGE,
    trending: EMPTY_PAGE,
    years: [],
    failed: false,
  })

  const requestKey = JSON.stringify([token, selectedOrganizationId, filters, reloadToken])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true

    const params: CVESearchParams = {
      limit: PAGE_SIZE,
      offset: 0,
      ...(filters.query ? { query: filters.query } : {}),
      ...(filters.severity ? { severity: filters.severity } : {}),
      ...(filters.isKevOnly ? { is_kev_only: true } : {}),
      ...(filters.year ? { year: filters.year } : {}),
    }

    // Las tres peticiones van en paralelo: el catálogo, la lista de KEV y los años son
    // independientes, y esperarlos en serie triplicaría el tiempo hasta el primer
    // pintado de la tabla. `allSettled` y no `all` porque los años son un adorno de
    // navegación: si fallan, la tabla y el KEV siguen siendo utilizables.
    void Promise.allSettled([
      searchCVE(token, selectedOrganizationId, params),
      getTrendingKEV(token, selectedOrganizationId, TRENDING_LIMIT),
      getCVEYears(token, selectedOrganizationId),
    ]).then(([search, trending, years]) => {
      if (!isActive) {
        return
      }
      if (search.status === 'rejected') {
        setResult((current) => ({ ...current, key: requestKey, failed: true }))
        return
      }
      setResult({
        key: requestKey,
        page: search.value,
        trending: trending.status === 'fulfilled' ? trending.value : EMPTY_PAGE,
        years:
          years.status === 'fulfilled' ? normalizeYears(years.value.years) : normalizeYears([]),
        failed: false,
      })
    })

    return () => {
      isActive = false
    }
  }, [filters, reloadToken, requestKey, selectedOrganizationId, token])

  return {
    page: isCurrent ? result.page : null,
    trending: isCurrent ? result.trending : null,
    years: isCurrent ? result.years : [],
    isLoading: Boolean(token && selectedOrganizationId) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    filters,
    setFilters: setFiltersState,
    clearFilters: () => setFiltersState(EMPTY_FILTERS),
    hasFilters: Boolean(filters.query || filters.severity || filters.isKevOnly || filters.year),
    refresh: () => setReloadToken((current) => current + 1),
  }
}

/**
 * Ordena los años y descarta los previos a 1999.
 *
 * El backend ya filtra por el patrón del identificador, así que un año imposible
 * no debería llegar aquí. Se filtra igual porque el coste es cero y un `1998` en la
 * navegación sería una mentira sobre el catálogo.
 */
function normalizeYears(years: number[]): number[] {
  return [...new Set(years)].filter((year) => year >= FIRST_CVE_YEAR).sort((a, b) => b - a)
}

export { PAGE_SIZE, TRENDING_LIMIT, FIRST_CVE_YEAR }
