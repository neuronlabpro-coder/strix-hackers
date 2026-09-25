import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, Check } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { getOnboardingStatus } from '../../lib/api'
import type { OnboardingStatus, OnboardingStepKey } from '../../types/api'
import { useAuth } from '../auth/useAuth'

/**
 * Destino de cada paso. Se definen aquí y no en el backend porque son rutas de
 * navegación del panel, no estado del tenant; el backend solo dice qué pasos
 * están cumplidos.
 */
const STEP_DESTINATION: Record<OnboardingStepKey, string> = {
  connect_git: '/repositories',
  import_repository: '/repositories',
  run_first_scan: '/pentests',
}

interface OnboardingSnapshot {
  key: string
  status: OnboardingStatus | null
  failed: boolean
}

export function OnboardingChecklist() {
  const { t } = useTranslation('onboarding')
  const { token, selectedOrganizationId } = useAuth()
  const [snapshot, setSnapshot] = useState<OnboardingSnapshot>({
    key: '',
    status: null,
    failed: false,
  })
  const [isCollapsed, setIsCollapsed] = useState(false)

  const requestKey = `${token}:${selectedOrganizationId}`
  const isCurrent = snapshot.key === requestKey
  const status = isCurrent ? snapshot.status : null
  const isLoading = Boolean(token && selectedOrganizationId) && !isCurrent && !snapshot.failed
  const loadFailed = isCurrent && snapshot.failed

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getOnboardingStatus(token, selectedOrganizationId)
      .then((payload) => {
        if (isActive) {
          setSnapshot({ key: requestKey, status: payload, failed: false })
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
  }, [requestKey, selectedOrganizationId, token])

  const progressPercent =
    status && status.total_steps > 0
      ? Math.round((status.completed_steps / status.total_steps) * 100)
      : 0

  return (
    <section className="content-card onboarding" aria-labelledby="onboarding-title">
      <div className="onboarding-head">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h2 id="onboarding-title">{t('title')}</h2>
        </div>
        <div className="onboarding-summary">
          {status ? (
            <span className="mono onboarding-progress-label">
              {t('progress', {
                completed: status.completed_steps,
                total: status.total_steps,
              })}
            </span>
          ) : null}
          <button
            className="icon-button"
            type="button"
            aria-expanded={!isCollapsed}
            aria-controls="onboarding-steps"
            onClick={() => setIsCollapsed((current) => !current)}
            aria-label={isCollapsed ? t('expand') : t('collapse')}
          >
            {isCollapsed ? (
              <ChevronDown size={18} aria-hidden="true" />
            ) : (
              <ChevronUp size={18} aria-hidden="true" />
            )}
          </button>
        </div>
      </div>

      <div
        className="onboarding-bar"
        role="progressbar"
        aria-valuenow={status?.completed_steps ?? 0}
        aria-valuemin={0}
        aria-valuemax={status?.total_steps ?? 3}
        aria-label={t('title')}
      >
        <span className="onboarding-bar-fill" style={{ width: `${progressPercent}%` }} />
      </div>

      {isCollapsed ? null : isLoading ? (
        <p className="chart-empty">{t('states.loading')}</p>
      ) : loadFailed || !status ? (
        <p className="inline-notice inline-notice-warning">{t('states.error')}</p>
      ) : status.is_complete ? (
        <p className="inline-notice" role="status">
          {t('complete')}
        </p>
      ) : (
        <ol className="onboarding-steps" id="onboarding-steps">
          {status.steps.map((step) => (
            <li
              key={step.key}
              className={
                step.completed ? 'onboarding-step onboarding-step-done' : 'onboarding-step'
              }
            >
              <span className="onboarding-marker" aria-hidden="true">
                {step.completed ? <Check size={13} /> : null}
              </span>
              <div className="onboarding-step-body">
                <p className="onboarding-step-title">
                  {t(`steps.${step.key}.title`)}
                  <span className="visually-hidden">
                    {step.completed
                      ? t('progress', {
                          completed: status.completed_steps,
                          total: status.total_steps,
                        })
                      : ''}
                  </span>
                </p>
                <p className="chart-empty">{t(`steps.${step.key}.description`)}</p>
              </div>
              {step.completed ? null : (
                <Link className="secondary-button" to={STEP_DESTINATION[step.key]}>
                  <span>{t(`steps.${step.key}.action`)}</span>
                </Link>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
