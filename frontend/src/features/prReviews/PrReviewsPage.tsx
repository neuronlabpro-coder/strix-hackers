import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { formatDate } from '../../lib/format'
import { STATUSES, usePrReviews, rowStatus, type RowStatus } from './usePrReviews'

export function PrReviewsPage() {
  const { t } = useTranslation('prReviews')
  const { page, metrics, isLoading, loadFailed, statusFilter, setStatusFilter, refresh } =
    usePrReviews()

  return (
    <section className="page-section" aria-labelledby="pr-reviews-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="pr-reviews-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <button className="secondary-button" type="button" onClick={refresh}>
          <RefreshCw size={16} aria-hidden="true" />
          <span>{t('actions.retry')}</span>
        </button>
      </div>

      <div className="metric-grid">
        <div className="metric-card">
          <span className="metric-value">{metrics?.total ?? '—'}</span>
          <span className="metric-caption">{t('metrics.audited')}</span>
        </div>
        <div className="metric-card">
          <span className="metric-value">{metrics?.clean ?? '—'}</span>
          <span className="metric-caption">{t('metrics.clean')}</span>
        </div>
        <div className="metric-card">
          <span className="metric-value">{metrics?.blocking ?? '—'}</span>
          <span className="metric-caption">{t('metrics.blocking')}</span>
        </div>
        <div className="metric-card">
          <span className="metric-value">
            {metrics ? metrics.issues_critical + metrics.issues_high : '—'}
          </span>
          <span className="metric-caption">{t('metrics.issues')}</span>
        </div>
      </div>

      <div className="filter-bar">
        <div className="filter-field">
          <label htmlFor="pr-reviews-status">{t('filters.status')}</label>
          <select
            id="pr-reviews-status"
            value={statusFilter ?? ''}
            onChange={(event) => setStatusFilter((event.target.value || null) as never)}
          >
            <option value="">{t('filters.allStatuses')}</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`status.${status}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={refresh}>
            <span>{t('actions.retry')}</span>
          </button>
        </div>
      ) : page && page.items.length === 0 ? (
        <div className="empty-card">
          <h2>{statusFilter ? t('states.noResults') : t('states.empty')}</h2>
          <p>{t('states.emptyDescription')}</p>
        </div>
      ) : page ? (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('table.repository')}</th>
                <th scope="col">{t('table.pullRequest')}</th>
                <th scope="col">{t('table.author')}</th>
                <th scope="col">{t('table.status')}</th>
                <th scope="col">{t('table.issues')}</th>
                <th scope="col">{t('table.finishedAt')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((review) => {
                const status = rowStatus(review)
                return (
                  <tr key={review.id}>
                    <th scope="row" className="mono">
                      {review.repository_name}
                    </th>
                    <td>
                      <span className="mono">#{review.pr_number}</span>
                      <span className="table-secondary">{review.pr_title}</span>
                    </td>
                    <td>{review.pr_author}</td>
                    <td>
                      <span className={`badge badge-review-${status.toLowerCase()}`}>
                        {t(`row.${status}`)}
                      </span>
                    </td>
                    <td className="mono">
                      {review.issues_caught_critical > 0 ? (
                        <span className="badge badge-status-critical">
                          {t('severity.critical')} {review.issues_caught_critical}
                        </span>
                      ) : null}
                      {review.issues_caught_high > 0 ? (
                        <span className="badge badge-status-high">
                          {t('severity.high')} {review.issues_caught_high}
                        </span>
                      ) : null}
                      {review.issues_caught_critical === 0 &&
                      review.issues_caught_high === 0 ? (
                        <span className="chart-empty">{t('table.noIssues')}</span>
                      ) : null}
                    </td>
                    <td>
                      {review.finished_at ? formatDate(review.finished_at) : t('table.inProgress')}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  )
}

export type { RowStatus }
