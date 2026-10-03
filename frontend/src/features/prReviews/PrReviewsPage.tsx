import { RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { formatDate } from '../../lib/format'
import { Pagination } from '../shared/Pagination'
import {
  EMPTY_QUERY,
  STATUSES,
  usePrReviews,
  rowStatus,
  type PrReviewsQuery,
  type RowStatus,
} from './usePrReviews'

export function PrReviewsPage() {
  const { t } = useTranslation('prReviews')
  const { page, metrics, isLoading, loadFailed, query, setQuery, setPage, refresh } =
    usePrReviews()

  const hasFilters = Boolean(
    query.status || query.search.trim() || query.createdFrom || query.createdTo,
  )

  function cambiar(campo: 'search' | 'createdFrom' | 'createdTo', valor: string): void {
    setQuery({ ...query, [campo]: valor } satisfies PrReviewsQuery)
  }

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

      {/*
        Los cuatro filtros llevan etiqueta visible, incluido el buscador. Con etiquetas en
        todos, `align-items: start` de la barra los deja compartiendo línea de control; un
        buscador sin etiqueta sería una fila más corta y quedaría pegado a las etiquetas de
        al lado, que es justo lo que el `align-self: end` de `.filter-bar > .search-field`
        viene a arreglar en las pantallas que no llevan etiqueta.
      */}
      <div className="filter-bar">
        <div className="filter-field filter-field-search">
          <label htmlFor="pr-reviews-search">{t('filters.search')}</label>
          <span className="search-field">
            <Search size={16} aria-hidden="true" />
            <input
              id="pr-reviews-search"
              type="search"
              value={query.search}
              placeholder={t('filters.searchPlaceholder')}
              onChange={(event) => cambiar('search', event.target.value)}
            />
          </span>
        </div>
        <div className="filter-field">
          <label htmlFor="pr-reviews-status">{t('filters.status')}</label>
          <select
            id="pr-reviews-status"
            value={query.status ?? ''}
            onChange={(event) =>
              setQuery({
                ...query,
                status: (event.target.value || null) as PrReviewsQuery['status'],
              })
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
        {/*
          El rango va sobre la fecha de **alta** de la revisión, no sobre la de finalización
          que muestra la tabla. La explicación va en el `title` de las dos etiquetas, que es
          donde cabe sin romper la alineación de la barra: una línea de ayuda dentro del
          `.filter-field` añadiría altura a un solo campo y descuadraría la fila.
        */}
        <div className="filter-field">
          <label htmlFor="pr-reviews-created-from" title={t('filters.dateHint')}>
            {t('filters.dateFrom')}
          </label>
          <input
            id="pr-reviews-created-from"
            type="date"
            value={query.createdFrom}
            onChange={(event) => cambiar('createdFrom', event.target.value)}
          />
        </div>
        <div className="filter-field">
          <label htmlFor="pr-reviews-created-to" title={t('filters.dateHint')}>
            {t('filters.dateTo')}
          </label>
          <input
            id="pr-reviews-created-to"
            type="date"
            value={query.createdTo}
            onChange={(event) => cambiar('createdTo', event.target.value)}
          />
        </div>
        {hasFilters ? (
          <button
            className="ghost-button filter-bar-clear"
            type="button"
            onClick={() => setQuery(EMPTY_QUERY)}
          >
            <span>{t('actions.clearFilters')}</span>
          </button>
        ) : null}
      </div>

      {/* `isLoading` a secas y no `isLoading && page === null`: el hook conserva la página
          anterior mientras llega la nueva, y con la segunda forma se pintarían las filas
          del filtro anterior bajo el título del nuevo. */}
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
          <h2>{hasFilters ? t('states.noResults') : t('states.empty')}</h2>
          <p>{hasFilters ? t('states.noResultsDescription') : t('states.emptyDescription')}</p>
        </div>
      ) : page ? (
        <>
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
                      <td className="mono cell-inline">
                        {review.issues_caught_critical > 0 ? (
                          <span className="badge badge-status-critical">
                            {/* La clave va en mayúsculas porque así está en `prReviews.json`
                                (`severity.CRITICAL`), igual que los estados de la fila de
                                arriba. Con minúsculas i18next no encontraba nada y pintaba
                                literalmente `severity.critical` en el badge. */}
                            {t('severity.CRITICAL')} {review.issues_caught_critical}
                          </span>
                        ) : null}
                        {review.issues_caught_high > 0 ? (
                          <span className="badge badge-status-high">
                            {t('severity.HIGH')} {review.issues_caught_high}
                          </span>
                        ) : null}
                        {review.issues_caught_critical === 0 &&
                        review.issues_caught_high === 0 ? (
                          <span className="chart-empty">{t('table.noIssues')}</span>
                        ) : null}
                      </td>
                      <td>
                        {review.finished_at
                          ? formatDate(review.finished_at)
                          : t('table.inProgress')}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {/* La paginación va **debajo** de la tabla y habla el idioma del endpoint: `total`,
              `limit` y `offset` llegan en la respuesta y el componente no traduce nada de su
              cuenta. Solo aparece si hay más de una página, que es lo que decide el propio
              componente con `total <= limit`. */}
          <Pagination
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            onOffsetChange={setPage}
            namespace="prReviews"
          />
        </>
      ) : null}
    </section>
  )
}

export type { RowStatus }