import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/useAuth'

export function DashboardPage() {
  const { t } = useTranslation('common')
  const { organizations, selectedOrganizationId } = useAuth()
  const selectedOrganization = organizations.find(
    (organization) => organization.id === selectedOrganizationId,
  )
  const workspaceName = selectedOrganization?.name ?? t('noWorkspace')

  return (
    <section className="page-section" aria-labelledby="dashboard-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('workspaceReady')}</p>
          <h1 id="dashboard-title">{t('dashboard.title')}</h1>
          <p className="page-description">{t('dashboard.welcome', { workspace: workspaceName })}</p>
        </div>
      </div>
      <div className="metric-grid">
        <article className="metric-card">
          <p className="eyebrow">{t('dashboard.metrics.securityScore')}</p>
          <strong className="metric-value">—</strong>
          <span className="metric-caption">{t('dashboard.metrics.pending')}</span>
        </article>
        <article className="metric-card">
          <p className="eyebrow">{t('dashboard.metrics.openIssues')}</p>
          <strong className="metric-value">—</strong>
          <span className="metric-caption">{t('dashboard.metrics.pending')}</span>
        </article>
        <article className="metric-card">
          <p className="eyebrow">{t('dashboard.metrics.prsReviewed')}</p>
          <strong className="metric-value">—</strong>
          <span className="metric-caption">{t('dashboard.metrics.pending')}</span>
        </article>
        <article className="metric-card">
          <p className="eyebrow">{t('dashboard.metrics.pentests')}</p>
          <strong className="metric-value">—</strong>
          <span className="metric-caption">{t('dashboard.metrics.pending')}</span>
        </article>
      </div>
      <section className="content-card">
        <p className="eyebrow">{t('dashboard.getStarted')}</p>
        <h2>{t('dashboard.getStartedTitle')}</h2>
        <p>{t('dashboard.getStartedDescription')}</p>
      </section>
    </section>
  )
}
