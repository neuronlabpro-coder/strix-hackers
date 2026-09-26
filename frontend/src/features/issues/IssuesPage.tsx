import { useState } from 'react'
import { LayoutGrid, RefreshCw, Rows3, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import type { IssueStatus, VulnerabilityListItem, VulnerabilitySeverity } from '../../types/api'
import { KanbanBoard } from './KanbanBoard'
import {
  PAGE_SIZE,
  SEVERITIES,
  STATUSES,
  useIssues,
  type IssuesViewMode,
} from './useIssues'

const SEVERITY_CLASS: Record<VulnerabilitySeverity, string> = {
  CRITICAL: 'severity-swatch-critical',
  HIGH: 'severity-swatch-high',
  MEDIUM: 'severity-swatch-medium',
  LOW: 'severity-swatch-low',
  INFO: 'severity-swatch-info',
}

function formatDateTime(value: string, locale: string, fallback: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) {
    return fallback
  }
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed)
}

export function IssuesPage() {
  const { t } = useTranslation('issues')
  const [viewMode, setViewMode] = useState<IssuesViewMode>('table')
  const {
    items,
    severityTotals,
    total,
    offset,
    limit,
    isLoading,
    loadFailed,
    query,
    setQuery,
    setPage,
    refresh,
  } = useIssues()
  const hasFilters = Boolean(query.severity || query.status || query.search || query.target)

  return (
    <section className="page-section" aria-labelledby="issues-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="issues-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <div className="segmented" role="group" aria-label={t('viewMode.label')}>
            <button
              className="segmented-button"
              type="button"
              aria-pressed={viewMode === 'table'}
              onClick={() => setViewMode('table')}
            >
              <Rows3 size={15} aria-hidden="true" />
              <span>{t('viewMode.table')}</span>
            </button>
            <button
              className="segmented-button"
              type="button"
              aria-pressed={viewMode === 'board'}
              onClick={() => setViewMode('board')}
            >
              <LayoutGrid size={15} aria-hidden="true" />
              <span>{t('viewMode.board')}</span>
            </button>
          </div>
          <button className="secondary-button" type="button" onClick={refresh}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('filters.clear')}</span>
          </button>
        </div>
      </div>

      <div className="severity-strip" role="group" aria-label={t('severityCounts.label')}>
        {SEVERITIES.map((severity) => (
          <button
            key={severity}
            className={`severity-pill severity-pill-${severity.toLowerCase()}`}
            type="button"
            aria-pressed={query.severity === severity}
            onClick={() =>
              setQuery({ ...query, severity: query.severity === severity ? null : severity })
            }
          >
            <span>{t(`severityCounts.${severity}`)}</span>
            <span>{severityTotals[severity]}</span>
          </button>
        ))}
      </div>

      {/*
        `align-items: flex-end` vive en la clase, no aquí. Los tres desplegables llevan
        etiqueta encima y el buscador no, así que alinear por arriba dejaba el buscador
        20 px más alto que sus vecinos: alineando los fondos, la fila de campos queda
        recta y las etiquetas conservan su separación respecto a su campo.
      */}
      <div className="filter-bar">
        <label className="search-field">
          <Search size={16} aria-hidden="true" />
          <span className="visually-hidden">{t('filters.searchLabel')}</span>
          <input
            type="search"
            value={query.search}
            placeholder={t('filters.searchPlaceholder')}
            onChange={(event) => setQuery({ ...query, search: event.target.value })}
          />
        </label>
        <div className="filter-field">
          <label htmlFor="issue-severity">{t('filters.severity')}</label>
          <select
            id="issue-severity"
            value={query.severity ?? ''}
            onChange={(event) =>
              setQuery({
                ...query,
                severity: (event.target.value || null) as VulnerabilitySeverity | null,
              })
            }
          >
            <option value="">{t('filters.allSeverities')}</option>
            {SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>
                {t(`severityCounts.${severity}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="issue-status">{t('filters.status')}</label>
          <select
            id="issue-status"
            value={query.status ?? ''}
            onChange={(event) =>
              setQuery({ ...query, status: (event.target.value || null) as IssueStatus | null })
            }
          >
            <option value="">{t('filters.allStatuses')}</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`status.${status}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="issue-target">{t('columns.target')}</label>
          <input
            id="issue-target"
            type="text"
            value={query.target}
            onChange={(event) => setQuery({ ...query, target: event.target.value })}
          />
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
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="empty-card">
          <h2>{hasFilters ? t('states.noResults') : t('states.empty')}</h2>
          <p>{t('states.emptyDescription')}</p>
        </div>
      ) : viewMode === 'table' ? (
        <IssuesTable items={items} />
      ) : (
        <KanbanBoard findings={items} viewMode={viewMode} />
      )}

      {!isLoading && !loadFailed && items.length > 0 ? (
        <nav className="pagination" aria-label={t('pagination.label')}>
          <span>
            {t('pagination.summary', {
              from: offset + 1,
              to: Math.min(offset + limit, total),
              total,
            })}
          </span>
          <div className="pagination-actions">
            <button
              className="secondary-button"
              type="button"
              disabled={offset === 0}
              onClick={() => setPage(Math.max(0, offset - PAGE_SIZE))}
            >
              <span>{t('pagination.previous')}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setPage(offset + PAGE_SIZE)}
            >
              <span>{t('pagination.next')}</span>
            </button>
          </div>
        </nav>
      ) : null}
    </section>
  )
}

function IssuesTable({ items }: { items: VulnerabilityListItem[] }) {
  const { t } = useTranslation('issues')
  const { t: tCommon } = useTranslation('common')
  const { i18n } = useTranslation()
  const locale = i18n.language

  return (
    <div className="table-wrapper">
      <table className="data-table">
        <caption className="visually-hidden">{t('title')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('columns.severity')}</th>
            <th scope="col">{t('columns.title')}</th>
            <th scope="col">{t('columns.target')}</th>
            <th scope="col">{t('columns.cvss')}</th>
            <th scope="col">{t('columns.status')}</th>
            <th scope="col">{t('columns.discovered')}</th>
            <th scope="col">{t('columns.open')}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((finding) => (
            <tr key={finding.id}>
              <td>
                <span className="provider-cell">
                  <span
                    className={`severity-swatch ${SEVERITY_CLASS[finding.severity]}`}
                    aria-hidden="true"
                  />
                  <span>{t(`severityCounts.${finding.severity}`)}</span>
                </span>
              </td>
              <td>{finding.title}</td>
              <td>
                <span className="mono">{finding.affected_target}</span>
              </td>
              <td>
                <span className="mono">{finding.cvss_score.toFixed(1)}</span>
              </td>
              <td>
                <span className={`badge badge-status-${finding.status.toLowerCase()}`}>
                  {t(`status.${finding.status}`)}
                </span>
              </td>
              <td>
                <span className="mono timestamp">
                  {formatDateTime(finding.discovered_at, locale, tCommon('values.unknown'))}
                </span>
              </td>
              <td>
                <Link
                  className="secondary-button"
                  to={`/issues/${finding.id}`}
                  aria-label={`${t('columns.open')}: ${finding.title}`}
                >
                  <span>{t('columns.open')}</span>
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
