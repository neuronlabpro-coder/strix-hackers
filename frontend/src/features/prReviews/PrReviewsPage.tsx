import { LoaderCircle, RefreshCw, ScanSearch, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { formatDate } from '../../lib/format'
import { Pagination } from '../shared/Pagination'
import {
  EMPTY_QUERY,
  ESTADOS_RELANZABLES,
  STATUSES,
  usePrReviews,
  rowStatus,
  type PrReviewsQuery,
  type RowStatus,
} from './usePrReviews'

export function PrReviewsPage() {
  const { t } = useTranslation('prReviews')
  const { t: tCommon } = useTranslation('common')
  const {
    page,
    metrics,
    isLoading,
    loadFailed,
    query,
    setQuery,
    setPage,
    refresh,
    lanzamiento,
    lanzarAnalisis,
    descartarAviso,
  } = usePrReviews()

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
      ) : (
        <>
          {/*
            El aviso del lanzamiento va **entre** los filtros y la tabla, no encima de la
            pantalla. Es lo que decide si el botón ha hecho algo, y quien acaba de pulsarlo
            lo mira en la zona donde estaba mirando.

            Y sale con `role="status"` y no con `role="alert"`: es una confirmación de una
            acción que el usuario pidió, no un problema. `alert` interrumpe al lector de
            pantalla y aquí no hay nada que interrumpir.
          */}
          {lanzamiento.estado === 'en_curso' ? (
            <p className="inline-notice" role="status">
              {t('analyze.started')}
            </p>
          ) : null}
          {lanzamiento.estado === 'fallido' ? (
            <div className="inline-notice inline-notice-error" role="alert">
              <p>{lanzamiento.reintentable ? t('analyze.queueFailed') : t('analyze.notLaunchable')}</p>
              <button className="secondary-button" type="button" onClick={descartarAviso}>
                <span>{tCommon('actions.close')}</span>
              </button>
            </div>
          ) : null}
          {page && page.items.length === 0 ? (
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
                  {/*
                    La columna de acciones va **última**, y no junto al repositorio ni junto al
                    PR. Es una convención de todas las tablas del panel, y por una razón que aquí
                    es más fuerte que en las demás: es la única columna cuyo ancho depende del
                    estado. Con el botón en medio, una fila `ERROR` y una `PASSED` partirían el
                    mismo texto en puntos distintos, y una tabla con filas que no alinean se lee
                    como dos tablas.
                  */}
                  <th scope="col">{t('table.actions')}</th>
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
                      {/*
                        El botón sale solo en los estados relanzables, y sale **siempre** en
                        ellos: la decisión de si se puede relanzar la toma `ESTADOS_RELANZABLES`
                        en el hook, no una condición escrita aquí. Duplicar esa lista en el
                        componente es la forma de que las dos se desincronicen y de que aparezca
                        un botón que el backend va a rechazar con un `404`.

                        Y sale **vacío** —no con un guion— en los estados en curso. Es lo que
                        hacen las demás tablas del panel con las celdas que no aplican, y un guion
                        en la columna de acciones se lee como «pulsar y no pasa nada», que es
                        exactamente el estado vacío que este botón evita.
                      */}
                      <td className="cell-actions">
                        {ESTADOS_RELANZABLES.has(review.status) ? (
                          <button
                            className="icon-button"
                            type="button"
                            disabled={
                              lanzamiento.estado === 'en_curso' &&
                              lanzamiento.reviewId === review.id
                            }
                            aria-label={t('analyze.label', { pr: review.pr_number })}
                            title={t('analyze.label', { pr: review.pr_number })}
                            onClick={() => lanzarAnalisis(review.id)}
                          >
                            {lanzamiento.estado === 'en_curso' &&
                            lanzamiento.reviewId === review.id ? (
                              <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
                            ) : (
                              <ScanSearch size={16} aria-hidden="true" />
                            )}
                          </button>
                        ) : null}
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
        </>
      )}
    </section>
  )
}

export type { RowStatus }