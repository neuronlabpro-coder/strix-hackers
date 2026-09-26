import { useState } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, Search, ShieldAlert, ShieldCheck } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { CVESeverity } from '../../types/api'
import { CveDetailModal } from './CveDetailModal'
import { formatDate, formatProbability } from './format'
import { SEVERITIES, useCveCatalog } from './useCveCatalog'

export function CvePage() {
  const { t } = useTranslation('cve')
  const { page, trending, years, isLoading, loadFailed, filters, setFilters, clearFilters, hasFilters, refresh } =
    useCveCatalog()
  const [draftQuery, setDraftQuery] = useState(filters.query)
  const [selectedCveId, setSelectedCveId] = useState<string | null>(null)

  // El texto del campo y la consulta aplicada son estados separados a propósito: si
  // no lo fueran, cada tecla dispararía una petición. El único camino que cambia
  // `filters.query` desde fuera es el botón de limpiar, y ahí se vacía también el
  // borrador; los demás filtros (año, severidad, KEV) no tocan lo que el usuario
  // está escribiendo, que es lo que espera al filtrar sin perder la búsqueda.
  const submitQuery = (event: React.FormEvent) => {
    event.preventDefault()
    setFilters({ ...filters, query: draftQuery.trim() })
  }

  const resetFilters = () => {
    setDraftQuery('')
    clearFilters()
  }

  return (
    <section className="page-section" aria-labelledby="cve-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="cve-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <button className="secondary-button" type="button" onClick={refresh}>
          <RefreshCw size={16} aria-hidden="true" />
          <span>{t('states.retry')}</span>
        </button>
      </div>

      <form className="filter-bar" role="search" onSubmit={submitQuery}>
        <label className="search-field">
          <Search size={16} aria-hidden="true" />
          <span className="visually-hidden">{t('search.label')}</span>
          <input
            type="search"
            value={draftQuery}
            placeholder={t('search.placeholder')}
            onChange={(event) => setDraftQuery(event.target.value)}
          />
        </label>
        <div className="filter-field">
          <label htmlFor="cve-severity">{t('search.severity')}</label>
          <select
            id="cve-severity"
            value={filters.severity ?? ''}
            onChange={(event) =>
              setFilters({ ...filters, severity: (event.target.value || null) as CVESeverity | null })
            }
          >
            <option value="">{t('search.allSeverities')}</option>
            {SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>
                {t(`severity.${severity}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field filter-field-check">
          <input
            id="cve-kev"
            type="checkbox"
            checked={filters.isKevOnly}
            onChange={(event) => setFilters({ ...filters, isKevOnly: event.target.checked })}
          />
          <label htmlFor="cve-kev" title={t('search.kevOnlyHint')}>
            {t('search.kevOnly')}
          </label>
        </div>
        {hasFilters ? (
          <button className="secondary-button" type="button" onClick={resetFilters}>
            <span>{t('search.clear')}</span>
          </button>
        ) : null}
      </form>

      <div className="cve-layout">
        <div className="cve-main">
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
          ) : page && page.items.length === 0 ? (
            <div className="empty-card">
              <h2>{t('search.noResults')}</h2>
              <p>{t('search.noResultsDescription')}</p>
            </div>
          ) : page ? (
            <>
              <p className="cve-count" aria-live="polite">
                {hasFilters
                  ? t('search.filtered', { count: page.total })
                  : t('search.resultsOther', { count: page.total })}
              </p>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">{t('table.cveId')}</th>
                      <th scope="col">{t('table.severity')}</th>
                      <th scope="col">{t('table.cvss')}</th>
                      <th scope="col">{t('table.epss')}</th>
                      <th scope="col">{t('table.published')}</th>
                      <th scope="col">{t('table.description')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((record) => (
                      <tr key={record.cve_id}>
                        <th scope="row">
                          <button
                            className="link-button mono"
                            type="button"
                            onClick={() => setSelectedCveId(record.cve_id)}
                          >
                            {record.cve_id}
                          </button>
                        </th>
                        <td>
                          <span className={`badge badge-status-${record.severity.toLowerCase()}`}>
                            {t(`severity.${record.severity}`)}
                          </span>
                          {record.is_kev ? (
                            <>
                              <span className="visually-hidden">{t('detail.kevTitle')}</span>
                              <ShieldAlert size={14} aria-hidden="true" className="cve-kev-icon" />
                            </>
                          ) : null}
                        </td>
                        <td className="mono">{record.cvss_score}</td>
                        <td className="mono">{formatProbability(record.epss_score) ?? '—'}</td>
                        <td>{formatDate(record.published_at)}</td>
                        <td className="cve-description">{record.description}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
        </div>

        <aside className="cve-sidebar" aria-label={t('sidebar.browseByYear')}>
          <section className="panel">
            <h2 className="panel-title">{t('sidebar.browseByYear')}</h2>
            <ul className="cve-year-list">
              <li>
                <button
                  className="cve-year"
                  type="button"
                  aria-pressed={filters.year === null}
                  onClick={() => setFilters({ ...filters, year: null })}
                >
                  {t('sidebar.allYears')}
                </button>
              </li>
              {years.map((year) => (
                <li key={year}>
                  <button
                    className="cve-year mono"
                    type="button"
                    aria-pressed={filters.year === year}
                    onClick={() => setFilters({ ...filters, year })}
                  >
                    {year}
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <section className="panel">
            <h2 className="panel-title">
              <ShieldCheck size={16} aria-hidden="true" />
              {t('sidebar.kevTitle')}
            </h2>
            <p className="panel-caption">{t('sidebar.kevDescription')}</p>
            {trending && trending.items.length > 0 ? (
              <ul className="cve-kev-list">
                {trending.items.map((record) => (
                  <li key={record.cve_id}>
                    <button
                      className="link-button mono"
                      type="button"
                      onClick={() => setSelectedCveId(record.cve_id)}
                    >
                      {record.cve_id}
                    </button>
                    <span className={`badge badge-status-${record.severity.toLowerCase()}`}>
                      {t(`severity.${record.severity}`)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="chart-empty">{t('sidebar.kevEmpty')}</p>
            )}
          </section>

          <section className="panel panel-promo">
            <h2 className="panel-title">{t('sidebar.promoTitle')}</h2>
            <p className="panel-caption">{t('sidebar.promoDescription')}</p>
            <Link className="primary-button" to="/pentests">
              <span>{t('sidebar.promoAction')}</span>
            </Link>
          </section>
        </aside>
      </div>

      <CveDetailModal cveId={selectedCveId} onClose={() => setSelectedCveId(null)} />
    </section>
  )
}
