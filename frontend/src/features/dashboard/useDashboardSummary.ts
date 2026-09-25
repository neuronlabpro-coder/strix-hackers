import { useCallback, useEffect, useMemo, useState } from 'react'

import { getDashboardSummary } from '../../lib/api'
import type { DashboardSummary } from '../../types/api'
import { useAuth } from '../auth/useAuth'

export interface DashboardSummaryState {
  summary: DashboardSummary | null
  isLoading: boolean
  loadFailed: boolean
  patchRepositoryFlag: (
    repositoryId: string,
    flag: 'pr_reviews_enabled' | 'is_active',
    value: boolean,
  ) => void
  refresh: () => void
}

export function useDashboardSummary(): DashboardSummaryState {
  const { token, selectedOrganizationId } = useAuth()
  const [summary, setSummary] = useState<DashboardSummary | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
  const hasSession = Boolean(token && selectedOrganizationId)

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }

    let isActive = true
    void getDashboardSummary(token, selectedOrganizationId)
      .then((payload) => {
        if (isActive) {
          setSummary(payload)
          setLoadFailed(false)
        }
      })
      .catch(() => {
        if (isActive) {
          setLoadFailed(true)
        }
      })

    return () => {
      isActive = false
    }
  }, [token, selectedOrganizationId, reloadToken])

  const refresh = useCallback(() => setReloadToken((current) => current + 1), [])

  const patchRepositoryFlag = useCallback(
    (repositoryId: string, flag: 'pr_reviews_enabled' | 'is_active', value: boolean) => {
      setSummary((current) => {
        if (!current) {
          return current
        }
        const repositories = current.repositories.map((repository) =>
          repository.id === repositoryId ? { ...repository, [flag]: value } : repository,
        )
        return {
          ...current,
          repositories,
          repositories_monitored: repositories.filter(
            (repository) => repository.pr_reviews_enabled && repository.is_active,
          ).length,
        }
      })
    },
    [],
  )

  // El estado de carga se deriva: nunca se escribe dentro del efecto.
  const isLoading = hasSession && summary === null && !loadFailed

  return useMemo(
    () => ({ summary, isLoading, loadFailed, patchRepositoryFlag, refresh }),
    [isLoading, loadFailed, patchRepositoryFlag, refresh, summary],
  )
}
