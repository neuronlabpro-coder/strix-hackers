import { useCallback, useMemo, useState } from 'react'
import { Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { formatDateTime } from '../../lib/format'
import { getMyTickets, getSupportSummary } from '../../lib/supportApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { SupportSummary, TicketSummary } from '../../types/support'
import { NewTicketDialog } from './NewTicketDialog'
import { PriorityPill, StatusPill } from './TicketPills'

/** Lo que el endpoint devuelve para los dos recursos que se piden a la vez. */
interface TicketsPayload {
  tickets: TicketSummary[]
  summary: SupportSummary
}

/**
 * Vista de tickets del cliente, en `/settings/support`.
 *
 * ## Por qué la lista se pide sin paginar
 *
 * El endpoint acepta `limit` y `offset`, y la lista se pide con el tope. Un workspace con
 * veinte tickets ve los veinte, y el ticket que abrió hace un mes sigue a la vista. Paginar
 * obligaría a un cliente a buscar su propio ticket en páginas, que es exactamente lo que no
 * se hace cuando se viene a soporte: se viene a comprobar si han contestado.
 *
 * ## Por qué la clave de la petición es el `organizationId` y no un contador
 *
 * Porque la clave tiene que cambiar **exactamente** cuando el resultado cambia, ni antes ni
 * después. El identificador del workspace es lo único que puede cambiar los datos desde
 * fuera de esta vista: los filtros son de la consola de soporte, y el `reload` usa un
 * contador interno que comparte clave a propósito. Con la clave mal elegida, cambiar de
 * workspace dejaría los tickets del anterior en pantalla hasta que la nueva respuesta
 * llegara, que es justo la fuga entre tenants que R3 prohíbe.
 */
export function SupportTicketsPage() {
  const { t } = useTranslation('support')
  const { token, selectedOrganizationId } = useAuth()
  const { notify } = useToast()
  const navigate = useNavigate()

  const [dialogOpen, setDialogOpen] = useState(false)

  const organizationId = selectedOrganizationId

  const fetcher = useCallback(
    async (key: string): Promise<TicketsPayload> => {
      const activeToken = token
      if (activeToken === null) {
        throw new Error('sin token')
      }
      // La clave **es** el identificador del workspace, y se usa para pedir. No es un
      // adorno que el hook ignore: es lo que garantiza que los datos de la pantalla
      // pertenecen al workspace de la cabecera.
      const [listado, resumen] = await Promise.all([
        getMyTickets(activeToken, key),
        getSupportSummary(activeToken, key),
      ])
      return { tickets: listado.items, summary: resumen }
    },
    [token],
  )

  const { data, isLoading, loadFailed, reload } = useAsyncResource<TicketsPayload>(
    fetcher,
    organizationId,
  )

  const tickets = useMemo(() => data?.tickets ?? [], [data])
  const summary = data?.summary ?? null

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
          <Plus size={15} aria-hidden="true" />
          <span>{t('newTicket')}</span>
        </button>
      </div>

      {loadFailed ? (
        <div className="empty-card">
          <p>{t('messages.loadError')}</p>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{t('retry')}</span>
          </button>
        </div>
      ) : null}

      {isLoading && tickets.length === 0 ? (
        <p className="field-hint">{t('loading')}</p>
      ) : null}

      {!isLoading && !loadFailed && tickets.length > 0 ? (
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
      ) : null}

      {!isLoading && !loadFailed && tickets.length === 0 ? (
        <div className="empty-card">
          <h2>{t('empty.title')}</h2>
          <p>{t('empty.description')}</p>
        </div>
      ) : null}

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
