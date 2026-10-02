import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { createTicket } from '../../lib/supportApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import type { SupportSummary, TicketCategory, TicketPriority } from '../../types/support'
import { PriorityPill } from './TicketPills'

/**
 * Modal de apertura de ticket.
 *
 * ## Por qué la prioridad `URGENT` se deshabilita en vez de desaparecer
 *
 * Porque `URGENT` es **una función de pago**, y una función de pago que no se ve no se
 * vende. Un cliente que no sabe que existen tickets urgentes no puede preguntar por ellos.
 *
 * Deshabilitada con su candado y su etiqueta `ENTERPRISE` **dentro** de la opción, la
 * información queda donde se toma la decisión. Con el motivo debajo del formulario, el
 * usuario lo lee después de haber decidido.
 *
 * ## Por qué el bloqueo del panel no sustituye al `403` del servidor
 *
 * Porque son dos cosas distintas. El panel dice «esto cuesta un plan»; el servidor dice
 * «no lo puedes hacer». Si el panel lo comprobara y bloqueara la petición, habría dos
 * reglas y divergirían en cuanto una se actualizara. Aquí el panel solo informa y el `403`
 * es la garantía: el envío **no** se deshabilita cuando la urgencia está bloqueada, porque
 * un cliente que insiste se lleva la explicación buena en vez de un botón muerto.
 *
 * ## Por qué el componente está montado siempre y se oculta con `null`
 *
 * Porque devuelve `null` en vez de `if (!open) return null` en el padre: el diálogo se
 * monta una vez y sus cinco `useState` sobreviven entre aperturas. Montarlo y desmontarlo en
 * cada apertura tira el borrador a medias si el usuario cierra sin querer, y un asunto de
 * veinte minutos de escribir no se tira por un clic fuera.
 *
 * El estado se reinicia explícitamente en el `onSubmit` correcto, no al cerrar, por la
 * razón contraria: cerrar no es cancelar. Quien cierra sin querer y vuelve a abrir
 * encuentra lo que había escrito.
 */
interface NewTicketDialogProps {
  open: boolean
  summary: SupportSummary | null
  onOpenChange: (open: boolean) => void
  onCreated: (ticketId: string, ticketNumber: string) => void
}

const MIN_SUBJECT = 3
const MIN_MESSAGE = 10

export function NewTicketDialog({
  open,
  summary,
  onOpenChange,
  onCreated,
}: NewTicketDialogProps) {
  const { t } = useTranslation('support')
  const { token, selectedOrganizationId } = useAuth()
  const { notify } = useToast()

  const [subject, setSubject] = useState('')
  const [category, setCategory] = useState<TicketCategory>('TECHNICAL')
  const [message, setMessage] = useState('')
  const [priority, setPriority] = useState<TicketPriority>('NORMAL')
  const [working, setWorking] = useState(false)

  if (!open) {
    return null
  }

  const urgentAllowed = summary?.can_request_urgent === true
  const subjectTooShort = subject.trim().length > 0 && subject.trim().length < MIN_SUBJECT
  const messageTooShort = message.trim().length > 0 && message.trim().length < MIN_MESSAGE
  const canSubmit =
    subject.trim().length >= MIN_SUBJECT && message.trim().length >= MIN_MESSAGE && !working

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || !selectedOrganizationId || !canSubmit) return
    setWorking(true)
    try {
      const ticket = await createTicket(token, selectedOrganizationId, {
        subject: subject.trim(),
        category,
        priority,
        message: message.trim(),
      })
      // Los campos se vacían **antes** de cerrar. Al revés, el componente ya se ha
      // desmontado y las cuatro asignaciones siguientes no existen: el texto se conserva y
      // aparece entero la próxima vez, que es como se pierde un asunto de media hora.
      setSubject('')
      setMessage('')
      setCategory('TECHNICAL')
      setPriority('NORMAL')
      onOpenChange(false)
      onCreated(ticket.id, ticket.ticket_number)
    } catch {
      notify('error', t('messages.openTicketError'))
      setWorking(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onOpenChange(false)
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-ticket-title"
      >
        <div className="modal-header">
          <h2 id="new-ticket-title">
            <span>{t('form.title')}</span>
          </h2>
        </div>
        <div className="modal-body">
          <p className="field-hint">{t('form.intro')}</p>

          <form className="settings-form" onSubmit={onSubmit}>
            <div className="field">
              <label htmlFor="ticket-subject">{t('form.subjectLabel')}</label>
              <input
                id="ticket-subject"
                type="text"
                value={subject}
                maxLength={255}
                placeholder={t('form.subjectPlaceholder')}
                onChange={(event) => setSubject(event.target.value)}
                aria-describedby="ticket-subject-hint"
                aria-invalid={subjectTooShort}
              />
              <p className="field-hint" id="ticket-subject-hint">
                {t('form.subjectHint')}
              </p>
              {subjectTooShort ? <p className="field-error">{t('form.subjectHint')}</p> : null}
            </div>

            <div className="field">
              <label htmlFor="ticket-category">{t('form.categoryLabel')}</label>
              <select
                id="ticket-category"
                value={category}
                onChange={(event) => setCategory(event.target.value as TicketCategory)}
                aria-describedby="ticket-category-hint"
              >
                {(['TECHNICAL', 'BILLING', 'VULNERABILITY_REVIEW', 'FEATURE_REQUEST'] as const).map(
                  (value) => (
                    <option key={value} value={value}>
                      {t(`categories.${value}`)}
                    </option>
                  ),
                )}
              </select>
              <p className="field-hint" id="ticket-category-hint">
                {t(`categoryDescriptions.${category}`)}
              </p>
            </div>

            <fieldset className="field">
              <legend>{t('form.priorityLabel')}</legend>
              <div className="priority-options">
                {(['LOW', 'NORMAL', 'URGENT'] as const).map((value) => {
                  const bloqueada = value === 'URGENT' && !urgentAllowed
                  return (
                    <label
                      key={value}
                      className={[
                        'priority-option',
                        priority === value ? 'priority-option-checked' : '',
                        bloqueada ? 'priority-option-locked' : '',
                      ]
                        .filter(Boolean)
                        .join(' ')}
                    >
                      {/*
                        El radio se **deshabilita** de verdad, no se atenúa con CSS.

                        Un radio deshabilitado por CSS sigue siendo enfocable con el teclado
                        y su valor se anuncia, que es lo que hace falta para que alguien que
                        no ve el candado pueda leer el motivo. Deshabilitarlo de verdad lo
                        saca de la tabulación, y entonces el motivo solo existe para quien
                        ve la pantalla, que es justo a quien no le hace falta.
                      */}
                      <input
                        type="radio"
                        name="ticket-priority"
                        value={value}
                        checked={priority === value}
                        disabled={bloqueada}
                        onChange={() => setPriority(value)}
                      />
                      <span className="priority-option-copy">
                        <strong>{t(`priorities.${value}`)}</strong>
                        <span>{t(`priorityHints.${value}`)}</span>
                      </span>
                      {bloqueada ? (
                        <span className="plan-gate-badge">
                          <span>{t('enterpriseBadge')}</span>
                        </span>
                      ) : (
                        <PriorityPill priority={value} />
                      )}
                    </label>
                  )
                })}
              </div>
              {!urgentAllowed ? <p className="field-hint">{t('urgentLockedHint')}</p> : null}
              <p className="field-hint">{t('form.priorityHint')}</p>
            </fieldset>

            <div className="field">
              <label htmlFor="ticket-message">{t('form.messageLabel')}</label>
              <textarea
                id="ticket-message"
                value={message}
                maxLength={8000}
                rows={5}
                placeholder={t('form.messagePlaceholder')}
                onChange={(event) => setMessage(event.target.value)}
                aria-describedby="ticket-message-hint"
                aria-invalid={messageTooShort}
              />
              <p className="field-hint" id="ticket-message-hint">
                {t('form.messageHint')}
              </p>
              {messageTooShort ? <p className="field-error">{t('form.messageHint')}</p> : null}
            </div>

            {/* Cancelar a la izquierda y confirmar a la derecha: es el orden del resto de
                modales del panel, y `.settings-form-actions` es una fila sin `row-reverse`,
                así que basta con el orden del DOM. Antes era al revés. */}
            <div className="settings-form-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => onOpenChange(false)}
                disabled={working}
              >
                <span>{t('form.cancel')}</span>
              </button>
              <button className="primary-button" type="submit" disabled={!canSubmit}>
                <span>{working ? t('form.working') : t('form.submit')}</span>
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}
