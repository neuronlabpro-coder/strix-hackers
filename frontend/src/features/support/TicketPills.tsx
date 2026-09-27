import { useTranslation } from 'react-i18next'

import type { TicketPriority, TicketStatus } from '../../types/support'

/**
 * Píldora de prioridad.
 *
 * ## Por qué lleva el punto de color **y** el texto
 *
 * Porque el color no es accesible por sí solo. Tres píldoras que solo se distinguen por un
 * tono de rojo, ámbar y verde son indistinguibles para quien tiene deuteranopía y
 * invisibles en una impresión en blanco y negro. El punto acelera la lectura de quien ve
 * bien los colores y el texto es lo que sostiene a quien no.
 *
 * Los tres tonos cumplen contraste AA sobre la superficie: `#dc4444`, `#c9a84a` y
 * `#17a163` sobre `#121316` dan contraste superior a 4,5:1.
 */
export function PriorityPill({ priority }: { priority: TicketPriority }) {
  const { t } = useTranslation('support')
  return (
    <span className={`pill pill-${classForPriority(priority)}`}>
      <span className="pill-dot" aria-hidden="true" />
      <span>{t(`priorities.${priority}`)}</span>
    </span>
  )
}

function classForPriority(priority: TicketPriority): string {
  switch (priority) {
    case 'URGENT':
      return 'urgent'
    case 'NORMAL':
      return 'normal'
    case 'LOW':
      return 'low'
  }
}

/**
 * Píldora de estado.
 *
 * `CLOSED` lleva un punto hueco y `RESOLVED` uno relleno del mismo tono: los dos últimos
 * estados de la vida de un ticket se parecen pero no son lo mismo, y un ticket resuelto que
 * el cliente no acepta se reabre —lo que solo se puede saber si los dos estados son
 * distintos—.
 */
export function StatusPill({ status }: { status: TicketStatus }) {
  const { t } = useTranslation('support')
  return (
    <span className={`pill pill-${classForStatus(status)}`}>
      <span className="pill-dot" aria-hidden="true" />
      <span>{t(`statuses.${status}`)}</span>
    </span>
  )
}

function classForStatus(status: TicketStatus): string {
  switch (status) {
    case 'OPEN':
      return 'open'
    case 'IN_PROGRESS':
      return 'progress'
    case 'RESOLVED':
    case 'CLOSED':
      return 'closed'
  }
}
