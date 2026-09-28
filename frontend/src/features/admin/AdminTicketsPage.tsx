import { useCallback, useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { formatDateTime } from '../../lib/format'
import {
  getAdminTicket,
  getAdminTickets,
  replyAsSupport,
  updateAdminTicket,
} from '../../lib/supportApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { TicketDetail, TicketPriority, TicketStatus } from '../../types/support'
import { PriorityPill, StatusPill } from '../support/TicketPills'

const PAGE_SIZE = 25

/**
 * Consola de triaje de soporte, en `/admin/tickets`.
 *
 * ## Por qué los urgentes se **fijan arriba** en vez de ordenarse primero
 *
 * Ordenar por prioridad pondría el urgente de hace tres semanas en la primera fila y el
 * normal de hace una hora en la quinta, detrás de cuatro urgentes viejos. El agente
 * abriría la cola creyendo que lo de arriba es lo importante de hoy, y lo importante de hoy
 * está en la última fila.
 *
 * Fijándolos se conservan las dos cosas: el urgente viejo se ve sin buscarlo, y el reciente
 * queda arriba del resto. El orden dentro de cada grupo sigue siendo el del servidor, por
 * `updated_at`, que es lo que el agente quiere: lo último que pasó.
 *
 * ## Por qué se ordena **en el cliente** y no en la consulta
 *
 * Porque la consulta va paginada y el servidor no puede saber qué parte de los urgentes
 * cabe en la página. Pedir los urgentes por separado y pegarlos delante duplicaría el camino
 * de renderizado y abriría una ventana entre las dos peticiones donde la lista no cuadra
 * con el total. Ordenar la página que ya se tiene es exacto para lo que se muestra, y el
 * total sigue siendo el del servidor.
 */
export function AdminTicketsPage() {
  const { t } = useTranslation('support')
  const { t: tAdmin } = useTranslation('admin')
  const { token } = useAuth()
  const { notify } = useToast()

  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<TicketStatus | ''>('')
  const [priorityFilter, setPriorityFilter] = useState<TicketPriority | ''>('')
  const [urgentPinned, setUrgentPinned] = useState(true)

  const [selected, setSelected] = useState<TicketDetail | null>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [mutating, setMutating] = useState(false)

  /**
   * La clave son los filtros y la página. Es lo que decide qué filas se muestran, así que
   * es lo único que puede hacer que la lista cambie: si la clave no cambiara al paginar, el
   * hook no volvería a pedir y los botones de página no harían nada.
   */
  const filtros = useMemo(
    () =>
      JSON.stringify({
        search: search.trim(),
        status: statusFilter,
        priority: priorityFilter,
        page,
      }),
    [search, statusFilter, priorityFilter, page],
  )

  const fetcher = useCallback(
    async (key: string) => {
      const activeToken = token
      if (activeToken === null) {
        throw new Error('sin token')
      }
      const parsed = JSON.parse(key) as {
        search: string
        status: TicketStatus | ''
        priority: TicketPriority | ''
        page: number
      }
      return getAdminTickets(activeToken, {
        status: parsed.status === '' ? undefined : parsed.status,
        priority: parsed.priority === '' ? undefined : parsed.priority,
        search: parsed.search === '' ? undefined : parsed.search,
        limit: PAGE_SIZE,
        offset: parsed.page * PAGE_SIZE,
      })
    },
    [token],
  )

  const { data, isLoading, loadFailed, reload } = useAsyncResource(fetcher, filtros)

  const tickets = useMemo(() => data?.items ?? [], [data])
  const total = data?.total ?? 0

  const ordenados = useMemo(() => {
    if (!urgentPinned) {
      return tickets
    }
    return [
      ...tickets.filter((ticket) => ticket.priority === 'URGENT'),
      ...tickets.filter((ticket) => ticket.priority !== 'URGENT'),
    ]
  }, [tickets, urgentPinned])

  const urgentesVisibles = ordenados.filter((ticket) => ticket.priority === 'URGENT').length

  async function openTicket(ticketId: string) {
    if (!token) return
    try {
      setSelected(await getAdminTicket(token, ticketId))
    } catch {
      notify('error', t('console.loadError'))
    }
  }

  async function patchTicket(patch: { status?: TicketStatus; priority?: TicketPriority }) {
    if (!token || selected === null) return
    setMutating(true)
    try {
      const updated = await updateAdminTicket(token, selected.id, patch)
      setSelected(updated)
      reload()
      if (patch.status !== undefined) {
        notify(
          'success',
          t('console.statusUpdated', {
            number: updated.ticket_number,
            status: t(`statuses.${patch.status}`),
          }),
        )
      }
      if (patch.priority !== undefined) {
        notify(
          'success',
          t('console.priorityUpdated', {
            number: updated.ticket_number,
            priority: t(`priorities.${patch.priority}`),
          }),
        )
      }
    } catch {
      notify('error', t('console.updateError'))
    } finally {
      setMutating(false)
    }
  }

  async function onReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || selected === null || sending) return
    const contenido = draft.trim()
    if (contenido.length === 0) return

    setSending(true)
    try {
      const message = await replyAsSupport(token, selected.id, { content: contenido })
      setDraft('')
      // El mensaje insertado se pinta desde la respuesta en vez de recargar el detalle
      // entero. Es la única llamada cuya respuesta **es** el objeto que hay que mostrar, y
      // refrescar por ella sería una petición extra para leer lo que ya está en la mano.
      setSelected({
        ...selected,
        messages: [...selected.messages, message],
        message_count: selected.message_count + 1,
        updated_at: message.created_at,
      })
      reload()
      notify('success', t('console.replySent', { email: selected.created_by_email }))
    } catch {
      notify('error', t('console.replyError'))
    } finally {
      setSending(false)
    }
  }

  return (
    <section className="page-section">
      <header className="settings-section-heading">
        <h1>{tAdmin('sections.tickets')}</h1>
        <p>{t('console.subtitle')}</p>
      </header>

      <div className="triage-toolbar">
        <div className="field">
          <label htmlFor="triage-search">{t('console.filterSearch')}</label>
          <input
            id="triage-search"
            type="search"
            value={search}
            placeholder="#TK-1005"
            onChange={(event) => {
              setSearch(event.target.value)
              setPage(0)
            }}
            aria-describedby="triage-search-hint"
          />
          <p className="field-hint" id="triage-search-hint">
            {t('console.filterSearchHint')}
          </p>
        </div>
        <div className="field">
          <label htmlFor="triage-status">{t('console.filterAllStatuses')}</label>
          <select
            id="triage-status"
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.target.value as TicketStatus | '')
              setPage(0)
            }}
          >
            <option value="">{t('console.filterAllStatuses')}</option>
            {(['OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED'] as const).map((value) => (
              <option key={value} value={value}>
                {t(`statuses.${value}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="triage-priority">{t('console.filterAllPriorities')}</label>
          <select
            id="triage-priority"
            value={priorityFilter}
            onChange={(event) => {
              setPriorityFilter(event.target.value as TicketPriority | '')
              setPage(0)
            }}
          >
            <option value="">{t('console.filterAllPriorities')}</option>
            {(['URGENT', 'NORMAL', 'LOW'] as const).map((value) => (
              <option key={value} value={value}>
                {t(`priorities.${value}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="triage-actions">
        <label className="priority-option triage-pin">
          <input
            type="checkbox"
            checked={urgentPinned}
            onChange={(event) => setUrgentPinned(event.target.checked)}
          />
          <span className="priority-option-copy">
            <strong>{t('console.urgentPinned')}</strong>
            <span>{t('console.urgentPinnedHint')}</span>
          </span>
        </label>
        {urgentesVisibles > 0 ? (
          <span className="triage-urgent-banner">
            <span>{t('console.urgentPinned')}</span>
            <span>{urgentesVisibles}</span>
          </span>
        ) : null}
      </div>

      {loadFailed ? (
        <div className="empty-card">
          <p>{t('console.loadError')}</p>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{tAdmin('actions.retry')}</span>
          </button>
        </div>
      ) : null}

      {isLoading && tickets.length === 0 ? (
        <p className="field-hint">{t('loading')}</p>
      ) : null}

      {!isLoading && !loadFailed && ordenados.length > 0 ? (
        <div className="table-wrapper">
          <table className="data-table console-table">
            <thead>
              <tr>
                <th scope="col">{t('console.table.number')}</th>
                <th scope="col">{t('console.table.tenant')}</th>
                <th scope="col">{t('console.table.subject')}</th>
                <th scope="col">{t('console.table.priority')}</th>
                <th scope="col">{t('console.table.status')}</th>
                <th scope="col">{t('console.table.updated')}</th>
              </tr>
            </thead>
            <tbody>
              {ordenados.map((ticket) => (
                <tr
                  key={ticket.id}
                  className={[
                    'ticket-row-clickable',
                    urgentPinned && ticket.priority === 'URGENT' ? 'ticket-row-pinned' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onClick={() => void openTicket(ticket.id)}
                >
                  <td>
                    <span className="ticket-number">{ticket.ticket_number}</span>
                  </td>
                  <td>
                    <span className="provider-cell">{ticket.organization_name}</span>
                  </td>
                  <td>
                    <div className="ticket-subject">
                      <strong>{ticket.subject}</strong>
                      <span>{ticket.created_by_email}</span>
                    </div>
                  </td>
                  <td>
                    <PriorityPill priority={ticket.priority} />
                  </td>
                  <td>
                    <StatusPill status={ticket.status} />
                  </td>
                  <td>{formatDateTime(ticket.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {!isLoading && !loadFailed && ordenados.length === 0 ? (
        <p className="field-hint">{t('console.empty')}</p>
      ) : null}

      <div className="pagination-summary">
        <span>
          {tAdmin('pagination.summary', {
            from: total === 0 ? 0 : page * PAGE_SIZE + 1,
            to: Math.min((page + 1) * PAGE_SIZE, total),
            total,
          })}
        </span>
      </div>
      <div className="triage-actions">
        <button
          className="secondary-button"
          type="button"
          disabled={page === 0}
          onClick={() => setPage((current) => Math.max(0, current - 1))}
        >
          <span>{tAdmin('pagination.previous')}</span>
        </button>
        <button
          className="secondary-button"
          type="button"
          disabled={(page + 1) * PAGE_SIZE >= total}
          onClick={() => setPage((current) => current + 1)}
        >
          <span>{tAdmin('pagination.next')}</span>
        </button>
      </div>

      {selected === null ? null : (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) setSelected(null)
          }}
        >
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="triage-ticket-title"
          >
            <div className="modal-header">
              <h2 id="triage-ticket-title">
                <span className="ticket-number">{selected.ticket_number}</span>{' '}
                {selected.subject}
              </h2>
            </div>
            <div className="modal-body">
              <p className="field-hint">
                {selected.organization_name} · {selected.created_by_email}
              </p>

              <div className="triage-actions">
                <select
                  className="select-input"
                  value={selected.status}
                  disabled={mutating}
                  onChange={(event) =>
                    void patchTicket({ status: event.target.value as TicketStatus })
                  }
                  aria-label={t('console.table.status')}
                >
                  {(['OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED'] as const).map((value) => (
                    <option key={value} value={value}>
                      {t(`statuses.${value}`)}
                    </option>
                  ))}
                </select>
                <select
                  className="select-input"
                  value={selected.priority}
                  disabled={mutating}
                  onChange={(event) =>
                    void patchTicket({ priority: event.target.value as TicketPriority })
                  }
                  aria-label={t('console.table.priority')}
                >
                  {(['URGENT', 'NORMAL', 'LOW'] as const).map((value) => (
                    <option key={value} value={value}>
                      {t(`priorities.${value}`)}
                    </option>
                  ))}
                </select>
              </div>

              {selected.assigned_to_email === null ? (
                <p className="field-hint">{t('console.lastAdminHint')}</p>
              ) : null}

              <div className="ticket-thread">
                {selected.messages.map((message) => (
                  <article
                    key={message.id}
                    className={[
                      'ticket-message',
                      message.is_admin_reply ? 'ticket-message-support' : 'ticket-message-own',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <div className="ticket-message-header">
                      <span className="ticket-message-author">
                        {message.is_admin_reply ? t('detail.supportAuthor') : message.sender_email}
                      </span>
                      <span className="ticket-message-time">
                        {formatDateTime(message.created_at)}
                      </span>
                    </div>
                    <p className="ticket-message-content">{message.content}</p>
                  </article>
                ))}
              </div>

              <form className="triage-reply" onSubmit={onReply}>
                <div className="field">
                  <label htmlFor="triage-reply">{t('console.replyPlaceholder')}</label>
                  <textarea
                    id="triage-reply"
                    value={draft}
                    maxLength={8000}
                    onChange={(event) => setDraft(event.target.value)}
                  />
                </div>
                <div className="settings-form-actions">
                  <button
                    className="primary-button"
                    type="submit"
                    disabled={draft.trim().length === 0 || sending}
                  >
                    <span>{sending ? t('console.replyWorking') : t('console.replySubmit')}</span>
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
