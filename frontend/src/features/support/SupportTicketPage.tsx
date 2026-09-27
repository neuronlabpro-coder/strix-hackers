import { useCallback, useState, type FormEvent } from 'react'
import { ArrowLeft, ShieldCheck } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'

import { formatDateTime } from '../../lib/format'
import { getMyTicket, replyToTicket } from '../../lib/supportApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { TicketDetail, TicketStatus } from '../../types/support'
import { PriorityPill, StatusPill } from './TicketPills'

/**
 * Estados en los que el cliente puede seguir escribiendo.
 *
 * Es una **copia** de `ABIERTO_PARA_CLIENTE` del backend, y la diferencia es deliberada: el
 * backend devuelve `409` al escribir en un hilo terminado, y el panel tiene que ofrecer el
 * botón correcto **antes**. Una lista de dos valores que hay que sincronizar con otra de
 * dos en el servidor es el precio de que el botón no desaparezca después de pulsarlo.
 *
 * Si divergieran, el síntoma sería un `409` en un ticket que el panel creía abierto, y el
 * mensaje de error explicaría por qué: hay que abrir otro.
 */
const ESCRIBIBLE: readonly TicketStatus[] = ['OPEN', 'IN_PROGRESS']

/**
 * Detalle de un ticket con su hilo, en `/settings/support/:ticketId`.
 *
 * ## Por qué el hilo no trae paginación
 *
 * Un ticket es una conversación con principio y final, y su longitud la fija el soporte. El
 * `thread` tiene `max-height` y scroll propio para que un intercambio de cuarenta mensajes
 * no empuje el formulario de respuesta fuera de la pantalla: quien llega a escribir quiere
 * tener el botón de envío a la vista.
 *
 * ## Por qué se recarga el detalle entero tras responder
 *
 * Porque `replyToTicket` devuelve **solo** el mensaje nuevo, no el ticket. El `updated_at`
 * y el `message_count` del ticket acaban de cambiar, y recargar cuesta una petición a un
 * recurso que el navegador ya tiene. Insertar el mensaje a mano en el estado sería
 * mantener dos fuentes de verdad para el mismo hilo, que divergirían en cuanto el mensaje
 * tuviera un segundo decimal distinto entre una y otra respuesta.
 */
export function SupportTicketPage() {
  const { t } = useTranslation('support')
  const { ticketId } = useParams<{ ticketId: string }>()
  const { token, user, selectedOrganizationId } = useAuth()
  const { notify } = useToast()

  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)

  const organizationId = selectedOrganizationId
  const currentUserId = user?.id ?? null

  const fetcher = useCallback(
    async (key: string): Promise<TicketDetail> => {
      const activeToken = token
      if (activeToken === null || organizationId === null) {
        throw new Error('sin sesion')
      }
      // `key` es el id del ticket, no el del workspace: es lo que decide el contenido del
      // detalle, y por eso tiene que ser la clave. Cambiar de ticket con el enlace equivocado
      // dejaría el hilo del anterior montado, y el flag `vigente` del hook descarta la
      // respuesta obsoleta antes de que llegue a pintarse.
      return getMyTicket(activeToken, organizationId, key)
    },
    [token, organizationId],
  )

  const {
    data: ticket,
    isLoading,
    loadFailed,
    reload,
  } = useAsyncResource<TicketDetail>(fetcher, ticketId ?? null)

  async function onReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || !organizationId || ticket === null || sending) return
    const contenido = draft.trim()
    if (contenido.length === 0) return

    setSending(true)
    try {
      await replyToTicket(token, organizationId, ticket.id, { content: contenido })
      setDraft('')
      notify('success', t('detail.replySent'))
      reload()
    } catch {
      notify('error', t('messages.replyError'))
      setSending(false)
    }
  }

  if (loadFailed || (ticket === null && !isLoading)) {
    return (
      <section className="settings-section">
        <div className="empty-card">
          <h2>{t('messages.detailError')}</h2>
          <Link className="secondary-button" to="/settings/support">
            <span>{t('detail.back')}</span>
          </Link>
        </div>
      </section>
    )
  }

  if (ticket === null) {
    return <p className="field-hint">{t('loading')}</p>
  }

  const escribible = ESCRIBIBLE.includes(ticket.status)

  return (
    <section className="settings-section">
      <div className="settings-form-actions">
        <Link className="admin-back-link" to="/settings/support">
          <ArrowLeft size={15} aria-hidden="true" />
          <span>{t('detail.back')}</span>
        </Link>
      </div>

      <header className="settings-section-heading">
        <h1>
          <span className="ticket-number">{ticket.ticket_number}</span> {ticket.subject}
        </h1>
        <p>
          {t('detail.openedBy', { email: ticket.created_by_email })} ·{' '}
          {ticket.assigned_to_email === null
            ? t('detail.unassigned')
            : t('detail.assignedTo', { email: ticket.assigned_to_email })}
        </p>
      </header>

      <div className="triage-actions">
        <PriorityPill priority={ticket.priority} />
        <StatusPill status={ticket.status} />
        <span className="provider-cell">
          {t('detail.categoryLabel')}: {t(`categories.${ticket.category}`)}
        </span>
      </div>

      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('detail.thread')}</h2>
        </div>

        {ticket.messages.length === 0 ? (
          <p className="empty-thread">{t('detail.threadEmpty')}</p>
        ) : (
          <div className="ticket-thread">
            {ticket.messages.map((message) => {
              // El `id` del usuario viene de la sesión y es opcional: una sesión guardada
              // antes de que existiera no lo tiene. Cuando falta, el mensaje no se marca como
              // propio y se dibuja a la izquierda como el de cualquier otro. Es el fallo
              // honesto —«no sé de quién es»— frente a marcarlo bien por casualidad.
              const propio = currentUserId !== undefined && message.sender_user_id === currentUserId
              return (
                <article
                  key={message.id}
                  className={[
                    'ticket-message',
                    message.is_admin_reply ? 'ticket-message-support' : '',
                    propio && !message.is_admin_reply ? 'ticket-message-own' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                >
                  <div className="ticket-message-header">
                    <span className="ticket-message-author">
                      {propio ? t('detail.clientAuthor') : message.sender_email}
                    </span>
                    {/*
                      La etiqueta de soporte **solo** aparece en los mensajes del equipo.

                      Un mensaje del cliente lleva su propio nombre y basta. Poner «Tú» en
                      todos y «Soporte» en los del equipo haría que la posición a la
                      izquierda ya lo dijera, y la etiqueta sería redundante.
                    */}
                    {message.is_admin_reply ? (
                      <span className="ticket-message-tag">
                        <ShieldCheck size={11} aria-hidden="true" />
                        <span>{t('detail.supportAuthor')}</span>
                      </span>
                    ) : null}
                    <span className="ticket-message-time">
                      {formatDateTime(message.created_at)}
                    </span>
                  </div>
                  <p className="ticket-message-content">{message.content}</p>
                </article>
              )
            })}
          </div>
        )}
      </div>

      {escribible ? (
        <form className="triage-reply" onSubmit={onReply}>
          <div className="field">
            <label htmlFor="ticket-reply">{t('detail.replyPlaceholder')}</label>
            <textarea
              id="ticket-reply"
              value={draft}
              maxLength={8000}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                // Shift+Enter hace salto de línea; Enter envía. Lo contrario rompería
                // cualquier mensaje de varias líneas, que es la mitad de los tickets de
                // soporte: pegar un log tiene seis líneas.
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  event.currentTarget.form?.requestSubmit()
                }
              }}
              aria-describedby="ticket-reply-hint"
            />
            <p className="field-hint" id="ticket-reply-hint">
              {t('detail.replyHint')}
            </p>
          </div>
          <div className="settings-form-actions">
            <button
              className="primary-button"
              type="submit"
              disabled={draft.trim().length === 0 || sending}
            >
              <span>{sending ? t('detail.replyWorking') : t('detail.replySubmit')}</span>
            </button>
          </div>
        </form>
      ) : (
        <div className="panel">
          <p className="danger-note">
            {t('detail.threadClosed', { status: t(`statuses.${ticket.status}`).toLowerCase() })}
          </p>
          <div className="settings-form-actions">
            <Link className="secondary-button" to="/settings/support">
              <span>{t('detail.threadClosedAction')}</span>
            </Link>
          </div>
        </div>
      )}
    </section>
  )
}
