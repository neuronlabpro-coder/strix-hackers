import { useEffect, useState } from 'react'

import { getPRReviewMetrics, getPRReviews } from '../../lib/api'
import type { PRReviewMetrics, PRReviewPage, PRReviewStatus } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const PAGE_SIZE = 25

/**
 * Estados que la tabla muestra, translated.
 *
 * El dominio guarda `QUEUED | SCANNING | PASSED | FAILED | ERROR` porque esos estados
 * son los que produce el worker y los que escriben los webhooks; renombrarlos a
 * `APPROVED | BLOCKED | PENDING` en la base significaría reinterpretar el historial ya
 * registrado. El cambio de vocabulario es de presentación, y por eso vive aquí y no
 * en el backend: la columna dice lo que le importa a quien lee la tabla, que es si el
 * merge está bloqueado.
 */
type RowStatus = 'APPROVED' | 'BLOCKED' | 'PENDING' | 'ERROR'

function rowStatus(review: {
  status: PRReviewStatus
  merge_blocked: boolean
}): RowStatus {
  // El orden importa: `merge_blocked` gana sobre el estado del escaneo. Un escaneo que
  // terminó en `PASSED` con un hallazgo de alta severidad sigue bloqueando el merge, y
  // un `FAILED` sin hallazgos relevantes no debería hacerlo. Lo que impide el merge es
  // la bandera, no el estado.
  if (review.merge_blocked) {
    return 'BLOCKED'
  }
  if (review.status === 'ERROR') {
    return 'ERROR'
  }
  if (review.status === 'PASSED') {
    return 'APPROVED'
  }
  if (review.status === 'FAILED') {
    return 'BLOCKED'
  }
  return 'PENDING'
}

const STATUSES: PRReviewStatus[] = ['PASSED', 'FAILED', 'SCANNING', 'QUEUED', 'ERROR']

export interface PrReviewsState {
  page: PRReviewPage | null
  metrics: PRReviewMetrics | null
  isLoading: boolean
  loadFailed: boolean
  statusFilter: PRReviewStatus | null
  setStatusFilter: (status: PRReviewStatus | null) => void
  refresh: () => void
}

export function usePrReviews(): PrReviewsState {
  const { token, selectedOrganizationId } = useAuth()
  const [statusFilter, setStatusFilter] = useState<PRReviewStatus | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [result, setResult] = useState<{
    key: string
    page: PRReviewPage | null
    metrics: PRReviewMetrics | null
    failed: boolean
  }>({ key: '', page: null, metrics: null, failed: false })

  const requestKey = JSON.stringify([token, selectedOrganizationId, statusFilter, reloadToken])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    // Los indicadores de cabecera se piden una vez por carga y no dependen del filtro:
    // los KPI del Tenant describen su actividad total, y recalcularlos al cambiar el
    // filtro haría que las cifras de la cabecera dejaran de cuadrar con la tabla.
    void Promise.allSettled([
      getPRReviews(token, selectedOrganizationId, {
        limit: PAGE_SIZE,
        offset: 0,
        ...(statusFilter ? { status: statusFilter } : {}),
      }),
      getPRReviewMetrics(token, selectedOrganizationId),
    ]).then(([page, metrics]) => {
      if (!isActive) {
        return
      }
      if (page.status === 'rejected') {
        setResult((current) => ({ ...current, key: requestKey, failed: true }))
        return
      }
      setResult({
        key: requestKey,
        page: page.value,
        metrics: metrics.status === 'fulfilled' ? metrics.value : null,
        failed: false,
      })
    })
    return () => {
      isActive = false
    }
  }, [reloadToken, requestKey, selectedOrganizationId, statusFilter, token])

  return {
    page: isCurrent ? result.page : null,
    metrics: isCurrent ? result.metrics : null,
    isLoading: Boolean(token && selectedOrganizationId) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    statusFilter,
    setStatusFilter,
    refresh: () => setReloadToken((current) => current + 1),
  }
}

export { PAGE_SIZE, STATUSES, rowStatus }
export type { RowStatus }
