import { useEffect, useState } from 'react'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { getAdminOrganizations, getInfrastructureHealth } from '../../lib/api'
import type { AdminOrganization, InfrastructureHealth } from '../../types/api'
import { useAuth } from '../auth/useAuth'

/** Ver `useIssues`: `key` identifica el sondeo al que pertenecen los datos. */
interface ConsoleSnapshot {
  key: string
  organizations: AdminOrganization[]
  organizationTotal: number
  health: InfrastructureHealth | null
  failed: boolean
}

export function AdminPage() {
  const { t } = useTranslation('admin')
  const { i18n } = useTranslation()
  const { token, selectedOrganizationId, user } = useAuth()
  const [probeToken, setProbeToken] = useState(0)
  const [snapshot, setSnapshot] = useState<ConsoleSnapshot>({
    key: '',
    organizations: [],
    organizationTotal: 0,
    health: null,
    failed: false,
  })
  const locale = i18n.language

  const requestKey = `${token}:${selectedOrganizationId}:${probeToken}`
  const isCurrent = snapshot.key === requestKey
  const health = isCurrent ? snapshot.health : null
  const isLoading = Boolean(token && selectedOrganizationId) && !isCurrent
  const loadFailed = isCurrent && snapshot.failed

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void Promise.all([
      getAdminOrganizations(token, selectedOrganizationId, 50, 0),
      getInfrastructureHealth(token, selectedOrganizationId),
    ])
      .then(([page, infrastructure]) => {
        if (isActive) {
          setSnapshot({
            key: requestKey,
            organizations: page.items,
            organizationTotal: page.total,
            health: infrastructure,
            failed: false,
          })
        }
      })
      .catch(() => {
        if (isActive) {
          setSnapshot({
            key: requestKey,
            organizations: [],
            organizationTotal: 0,
            health: null,
            failed: true,
          })
        }
      })
    return () => {
      isActive = false
    }
  }, [requestKey, selectedOrganizationId, token])

  if (isLoading) {
    return (
      <section className="page-section">
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      </section>
    )
  }

  if (loadFailed || !health) {
    return (
      <section className="page-section">
        <div className="empty-card">
          <span className="empty-card-mark" aria-hidden="true">
            <ShieldAlert size={20} />
          </span>
          <h2>{t('forbidden.title')}</h2>
          <p>{t('forbidden.description')}</p>
          <Link className="secondary-button" to="/dashboard">
            <span>{t('forbidden.back')}</span>
          </Link>
          <button className="secondary-button" type="button" onClick={() => setProbeToken((current) => current + 1)}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="page-section" aria-labelledby="admin-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="admin-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          {user ? <span className="badge mono">{user.email}</span> : null}
          <button className="secondary-button" type="button" onClick={() => setProbeToken((current) => current + 1)}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('health.refresh')}</span>
          </button>
        </div>
      </div>

      <section className="content-card" aria-labelledby="admin-health">
        <p className="eyebrow">{t('health.title')}</p>
        <h2 id="admin-health">{t('health.caption')}</h2>
        <div className="health-grid">
          <HealthCard
            title={t('health.database')}
            health={health.database}
            statusLabel={t(`health.dependencyStatus.${health.database.status}`)}
            latency={t('health.latency')}
            locale={locale}
          />
          <HealthCard
            title={t('health.cache')}
            health={health.cache}
            statusLabel={t(`health.dependencyStatus.${health.cache.status}`)}
            latency={t('health.latency')}
            locale={locale}
          />
        </div>
        <p className="chart-empty">
          {t(`health.status.${health.status}`)} · {t('health.checkedAt')}{' '}
          {new Intl.DateTimeFormat(locale, { timeStyle: 'medium' }).format(
            new Date(health.checked_at),
          )}
        </p>
      </section>

      <section className="content-card" aria-labelledby="admin-organizations">
        <p className="eyebrow">{t('organizations.title')}</p>
        <h2 id="admin-organizations">{t('organizations.caption')}</h2>
        {snapshot.organizations.length === 0 ? (
          <p className="chart-empty">{t('organizations.empty')}</p>
        ) : (
          <div className="table-wrapper">
            <table className="data-table">
              <caption className="visually-hidden">{t('organizations.title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('organizations.columns.name')}</th>
                  <th scope="col">{t('organizations.columns.slug')}</th>
                  <th scope="col">{t('organizations.columns.plan')}</th>
                  <th scope="col">{t('organizations.columns.credits')}</th>
                  <th scope="col">{t('organizations.columns.created')}</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.organizations.map((organization) => (
                  <tr key={organization.id}>
                    <td>{organization.name}</td>
                    <td>
                      <span className="mono">{organization.slug}</span>
                    </td>
                    <td>
                      <span className="badge">{t(`organizations.plan.${organization.plan_tier}`)}</span>
                    </td>
                    <td>
                      <span className="mono">
                        {new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(
                          organization.credit_balance,
                        )}
                      </span>
                    </td>
                    <td>
                      <span className="mono timestamp">
                        {new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(
                          new Date(organization.created_at),
                        )}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="chart-empty">
          {t('organizations.total', { total: snapshot.organizationTotal })}
        </p>
      </section>
    </section>
  )
}

function HealthCard({
  title,
  health,
  statusLabel,
  latency,
  locale,
}: {
  title: string
  health: InfrastructureHealth['database']
  statusLabel: string
  latency: string
  locale: string
}) {
  return (
    <div className="health-card">
      <p className="eyebrow">{title}</p>
      <span
        className={health.status === 'online' ? 'health-state badge-on' : 'health-state terminal-error'}
      >
        <span
          className={health.status === 'online' ? 'severity-swatch-critical' : 'severity-swatch-info'}
          aria-hidden="true"
        />
        <span>{statusLabel}</span>
      </span>
      <span className="chart-empty">
        {latency}:{' '}
        {new Intl.NumberFormat(locale, { style: 'unit', unit: 'millisecond' }).format(
          health.latency_ms,
        )}
      </span>
    </div>
  )
}
