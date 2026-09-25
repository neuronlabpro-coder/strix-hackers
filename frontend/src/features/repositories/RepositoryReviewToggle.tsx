import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { updateRepository } from '../../lib/api'
import { useAuth } from '../auth/useAuth'
import { useDashboardSummary } from '../dashboard/useDashboardSummary'

export interface RepositoryReviewToggleProps {
  repositoryId: string
  enabled: boolean
  /** Actualización optimista local mientras la API responde. */
  onOptimisticChange: (enabled: boolean) => void
  onFailure: () => void
}

/**
 * Toggle de revisiones automáticas de PR. Reutiliza el `PATCH` del backend y el
 * parche optimista del resumen para que dashboard y repositorios nunca divergan.
 */
export function RepositoryReviewToggle({
  repositoryId,
  enabled,
  onOptimisticChange,
  onFailure,
}: RepositoryReviewToggleProps) {
  const { t } = useTranslation('repositories')
  const { token, selectedOrganizationId } = useAuth()
  const [isPending, setIsPending] = useState(false)

  const handleToggle = async () => {
    if (!token || !selectedOrganizationId || isPending) {
      return
    }
    const nextValue = !enabled
    setIsPending(true)
    onOptimisticChange(nextValue)
    try {
      await updateRepository(token, selectedOrganizationId, repositoryId, {
        pr_reviews_enabled: nextValue,
      })
    } catch {
      onOptimisticChange(enabled)
      onFailure()
    } finally {
      setIsPending(false)
    }
  }

  return (
    <button
      className={enabled ? 'toggle toggle-on' : 'toggle'}
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={enabled ? t('prReviews.disable') : t('prReviews.enable')}
      disabled={isPending}
      onClick={() => void handleToggle()}
    >
      <span className="toggle-track" aria-hidden="true">
        <span className="toggle-thumb" />
      </span>
      <span className="toggle-label">{enabled ? t('prReviews.on') : t('prReviews.off')}</span>
    </button>
  )
}

/** Envoltorio que sincroniza el toggle con el resumen de dashboard. */
export function ConnectedRepositoryToggle({
  repositoryId,
  enabled,
  onFailure,
}: {
  repositoryId: string
  enabled: boolean
  onFailure: () => void
}) {
  const { patchRepositoryFlag } = useDashboardSummary()
  return (
    <RepositoryReviewToggle
      repositoryId={repositoryId}
      enabled={enabled}
      onOptimisticChange={(value) =>
        patchRepositoryFlag(repositoryId, 'pr_reviews_enabled', value)
      }
      onFailure={onFailure}
    />
  )
}
