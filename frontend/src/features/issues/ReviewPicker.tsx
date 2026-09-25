import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { getRepositoryReviews } from '../../lib/api'
import type { PRReviewSummary } from '../../types/api'
import { useAuth } from '../auth/useAuth'

export interface ReviewPickerProps {
  repositoryId: string | null
  value: string
  onChange: (reviewId: string) => void
  disabled?: boolean
}

/**
 * Selector de la revisión de Pull Request que originó el hallazgo.
 *
 * Sustituye al campo de texto libre del Bloque 4.2: pedir el `review_id` a mano
 * invitaba a copiar un UUID equivocado y abrir la remediación sobre la revisión
 * incorrecta. Si el hallazgo no viene de una revisión de PR, el endpoint de
 * autofix no aplica y el componente lo declara en vez de ofrecer un desplegable
 * vacío que nadie podría usar bien.
 *
 * El estado se deriva de una clave que identifica la consulta resuelta, igual
 * que en `useIssues`, para que un recargado no devuelva el desplegable al estado
 * de carga.
 */
interface ReviewsSnapshot {
  key: string
  reviews: PRReviewSummary[]
  failed: boolean
}

export function ReviewPicker({ repositoryId, value, onChange, disabled }: ReviewPickerProps) {
  const { t } = useTranslation('issues')
  const { t: tRepositories } = useTranslation('repositories')
  const { token, selectedOrganizationId } = useAuth()
  const [snapshot, setSnapshot] = useState<ReviewsSnapshot>({
    key: '',
    reviews: [],
    failed: false,
  })

  const requestKey = `${token}:${selectedOrganizationId}:${repositoryId ?? ''}`
  const isCurrent = snapshot.key === requestKey
  const reviews = isCurrent ? snapshot.reviews : []
  const isLoading =
    repositoryId !== null &&
    Boolean(token && selectedOrganizationId) &&
    !isCurrent &&
    !snapshot.failed
  const loadFailed = isCurrent && snapshot.failed

  useEffect(() => {
    if (!repositoryId || !token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getRepositoryReviews(token, selectedOrganizationId, repositoryId, 50, 0)
      .then((page) => {
        if (isActive) {
          setSnapshot({ key: requestKey, reviews: page.items, failed: false })
        }
      })
      .catch(() => {
        if (isActive) {
          setSnapshot((current) => ({ ...current, key: requestKey, failed: true }))
        }
      })
    return () => {
      isActive = false
    }
  }, [repositoryId, requestKey, selectedOrganizationId, token])

  if (repositoryId === null) {
    return <p className="chart-empty">{tRepositories('autofix.notFromReview')}</p>
  }

  return (
    <div className="filter-field">
      <label htmlFor="review-picker">{t('detail.autofix.reviewId')}</label>
      <select
        id="review-picker"
        value={value}
        disabled={disabled || isLoading}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">
          {isLoading
            ? t('detail.autofix.reviewsLoading')
            : t('detail.autofix.selectReview')}
        </option>
        {reviews.map((review) => (
          <option key={review.id} value={review.id}>
            {t('detail.autofix.reviewOption', {
              number: review.pr_number,
              branch: review.source_branch,
              status: tRepositories(`reviewStatus.${review.status}`),
            })}
          </option>
        ))}
      </select>
      <p className="chart-empty">
        {loadFailed
          ? t('detail.autofix.reviewsError')
          : reviews.length === 0 && !isLoading
            ? t('detail.autofix.reviewsEmpty')
            : ''}
      </p>
    </div>
  )
}
