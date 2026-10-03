import { useMemo, useState } from 'react'
import { Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { formatDateTime } from '../../lib/format'
import { useToast } from '../shared/toast-context'
import { Pagination } from '../shared/Pagination'
import { NewTicketDialog } from './NewTicketDialog'
import { PriorityPill, StatusPill } from './TicketPills'
import {
  EMPTY_QUERY,
  TICKET_STATUSES,
  useSupportTickets,
  type TicketsQuery,
} from './useSupportTickets'

/**
 * Vista de tickets del cliente, en `/settings/support`.
 *
 * ## Por qué la lista **sí** pagina
 *
 * Porque el endpoint aceptaba `limit` y `offset` y esta vista no los mandaba: el backend
 * aplicaba su tope por defecto de veinticinco y los tickets que quedaban detrás no existían
 * para nadie, sin barra y sin aviso de que faltara nada. El comentario anterior decía que
 * paginar «obligaría a un cliente a buscar su propio ticket en páginas», y era cierto solo
 * mientras la lista no llegara a veinticinco. Con buscador encima —que es lo que hace falta
 * para no perder un ticket de hace tres meses entre veinte de esta semana— la paginación es
 * lo que hace que el buscador devuelva exactamente lo pedido.
 *
 * ## Por qué el rango de fechas es de **alta** y no de actualización
 *
 * Porque la columna de la tabla es `updated_at`, que se mueve con cada mensaje: un filtro
 * sobre ella daría un resultado distinto cada diez minutos, según si alguien ha contestado.
 * `created_at` no cambia nunca. La columna se sigue llamando «Actualizado» y el filtro se
 * anuncia como rango de alta, con la explicación en el `title` de las dos etiquetas.
 *
 * ## Por qué las tarjetas de arriba no dependen del filtro
 *
 * Porque cuentan **todos** los tickets del workspace. Van en su propia petición —con su
 * propia clave— para que cambiar de página o de estado no las vuelva a pedir, y para que un
 * `429` de la cabecera no deje la pantalla sin números justo al paginar.
 */
export function SupportTicketsPage() {
  const { t } = useTranslation('support')
  const { notify } = useToast()
  const navigate = useNavigate()

  const [dialogOpen, setDialogOpen] = useState(false)

  const { page, summary, isLoading, loadFailed, query, setQuery, setPage, reload } =
    useSupportTickets()

  const tickets = useMemo(() => page?.items ?? [], [page])

  const hasFilters = Boolean(
    query.status || query.search.trim() || query.createdFrom || query.createdTo,
  )

  function cambiar(campo: 'search' | 'createdFrom' | 'createdTo', valor: string): void {
    setQuery({ ...query, [campo]: valor } satisfies TicketsQuery)
  }

  return (
    <section className="settings-section">
      <div className="support-kpi-grid">
        <div className="kpi-card">
          <p className="kpi-label">{t('summary.open')}</p>
          <p className="kpi-value">{summary?.open_count ?? 0}</p>
          <p className="kpi-hint">{t('summary.openHint')}</p>
        </div>
        <div className="kpi-card">
          <p className="kpi-label">{t('summary.waiting')}</p>
          <p className="kpi-value">{summary?.waiting_count ?? 0}</p>
          <p className="kpi-hint">{t('summary.waitingHint')}</p>
        </div>
        <div className="kpi-card">
          <p className="kpi-label">{t('summary.resolved')}</p>
          <p className="kpi-value">{summary?.resolved_count ?? 0}</p>
          <p className="kpi-hint">{t('summary.resolvedHint')}</p>
        </div>
      </div>

      <div className="settings-form-actions">
        <button className="primary-button" type="button" onClick={() => setDialogOpen(true)}>
          <Plus size={16} aria-hidden="true" />
          <span>{t('newTicket')}</span>
        </button>
      </div>

      {/* Los cuatro filtros llevan etiqueta visible, incluido el buscador. Con etiquetas en
          todos, `align-items: start` de la barra los deja compartiendo línea de control; un
          buscador sin etiqueta sería una fila más corta y quedaría pegado a las etiquetas de
          al lado, que es justo lo que el `align-self: end` de
          `.filter-bar > .search-field` viene a arreglar en las pantallas que no llevan
          etiqueta. */}
      <div className="filter-bar">
        <div className="filter-field filter-field-search">
          <label htmlFor="support-tickets-search">{t('filters.search')}</label>
          <span className="search-field">
            <Search size={16} aria-hidden="true" />
            <input
              id="support-tickets-search"
              type="search"
              value={query.search}
              placeholder={t('filters.searchPlaceholder')}
              onChange={(event) => cambiar('search', event.target.value)}
            />
          </span>
        </div>
        <div className="filter-field">
          <label htmlFor="support-tickets-status">{t('filters.status')}</label>
          <select
            id="support-tickets-status"
            value={query.status ?? ''}
            onChange={(event) =>
              setQuery({
                ...query,
                status: (event.target.value || null) as TicketsQuery['status'],
              })
            }
          >
            <option value="">{t('filters.allStatuses')}</option>
            {TICKET_STATUSES.map((estado) => (
              <option key={estado} value={estado}>
                {t(`statuses.${estado}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="support-tickets-created-from" title={t('filters.dateHint')}>
            {t('filters.dateFrom')}
          </label>
          <input
            id="support-tickets-created-from"
            type="date"
            value={query.createdFrom}
            onChange={(event) => cambiar('createdFrom', event.target.value)}
          />
        </div>
        <div className="filter-field">
          <label htmlFor="support-tickets-created-to" title={t('filters.dateHint')}>
            {t('filters.dateTo')}
          </label>
          <input
            id="support-tickets-created-to"
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

      {/* `isLoading` a secas y no `isLoading && tickets.length === 0`: el hook conserva la
          página anterior mientras llega la nueva, y con la segunda forma se pintarían las filas
          del filtro anterior bajo el título del nuevo. Ese error ya se corrigió en
          `AdminOperationsPage` y aquí se evita por construcción. */}
      {isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{t('retry')}</span>
          </button>
        </div>
      ) : tickets.length === 0 ? (
        <div className="empty-card">
          <h2>{hasFilters ? t('states.noResultsTitle') : t('empty.title')}</h2>
          <p>{hasFilters ? t('states.noResultsDescription') : t('empty.description')}</p>
        </div>
      ) : (
        <>
          <div className="table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('table.number')}</th>
                  <th scope="col">{t('table.subject')}</th>
                  <th scope="col">{t('table.priority')}</th>
                  <th scope="col">{t('table.status')}</th>
                  <th scope="col">{t('table.messages')}</th>
                  <th scope="col">{t('table.updated')}</th>
                </tr>
              </thead>
              <tbody>
                {tickets.map((ticket) => (
                  <tr
                    key={ticket.id}
                    className="ticket-row-clickable"
                    onClick={() => navigate(`/settings/support/${ticket.id}`)}
                  >
                    <td>
                      <span className="ticket-number">{ticket.ticket_number}</span>
                    </td>
                    <td>
                      <div className="ticket-subject">
                        <strong>{ticket.subject}</strong>
                        <span>
                          {t(`categories.${ticket.category}`)} · {ticket.created_by_email}
                        </span>
                      </div>
                    </td>
                    <td>
                      <PriorityPill priority={ticket.priority} />
                    </td>
                    <td>
                      <StatusPill status={ticket.status} />
                    </td>
                    <td>{ticket.message_count}</td>
                    <td>{formatDateTime(ticket.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* La paginación va **debajo** de la tabla y habla el idioma del endpoint: `total`,
              `limit` y `offset` llegan en la respuesta y el componente no traduce nada de su
              cuenta. Solo aparece si hay más de una página. */}
          {page ? (
            <Pagination
              total={page.total}
              limit={page.limit}
              offset={page.offset}
              onOffsetChange={setPage}
              namespace="support"
            />
          ) : null}
        </>
      )}

      <NewTicketDialog
        open={dialogOpen}
        summary={summary}
        onOpenChange={setDialogOpen}
        onCreated={(ticketId, ticketNumber) => {
          notify('success', t('messages.openTicketSuccess', { number: ticketNumber }))
          reload()
          navigate(`/settings/support/${ticketId}`)
        }}
      />
    </section>
  )
}
