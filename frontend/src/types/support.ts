/**
 * Tipos del sistema de tickets de soporte.
 *
 * ## Por qué el hilo no viaja en el listado
 *
 * `TicketSummary` no tiene `messages`. Un cliente con veinte tickets abiertos cargaría veinte
 * conversaciones enteras para pintar una tabla que enseña seis filas por página, y el hilo
 * ya está cargado cuando el usuario abre el ticket. Son dos peticiones y dos respuestas
 * distintas, no una con todo dentro.
 *
 * ## Por qué los importes y los identificadores viajan como cadenas
 *
 * Igual que en `types/billing.ts`: los `Decimal` de Pydantic serializan a cadena, y un
 * número JSON es un binario en coma flotante donde `0.1` no es exactamente `0.1`. Los UUID
 * viajan como cadena porque es lo que emite el servidor, y convertirlos a `UUID` en el
 * cliente obligaría a cada pantalla a hacerlo.
 */

export type TicketCategory = 'TECHNICAL' | 'BILLING' | 'VULNERABILITY_REVIEW' | 'FEATURE_REQUEST'

export type TicketPriority = 'LOW' | 'NORMAL' | 'URGENT'

export type TicketStatus = 'OPEN' | 'IN_PROGRESS' | 'RESOLVED' | 'CLOSED'

export interface TicketMessage {
  id: string
  sender_user_id: string
  sender_email: string
  /**
   * `true` si lo escribió el equipo de soporte.
   *
   * Es lo único que separa las dos voces en el hilo, y el cliente **no** puede ponerlo a
   * `true`: el servidor lo deduce de la ruta por la que llegó la petición. Por eso no
   * aparece en ningún tipo de entrada.
   */
  is_admin_reply: boolean
  content: string
  created_at: string
}

export interface TicketSummary {
  id: string
  /** `#TK-1001`. El número que el cliente lee en voz alta y el agente busca. */
  ticket_number: string
  subject: string
  category: TicketCategory
  priority: TicketPriority
  status: TicketStatus
  organization_id: string
  organization_name: string
  created_by_user_id: string
  created_by_email: string
  assigned_to_user_id: string | null
  assigned_to_email: string | null
  created_at: string
  /** Última actividad. Es lo que ordena la lista: lo último que pasó, no lo más antiguo. */
  updated_at: string
  message_count: number
}

export interface TicketDetail extends TicketSummary {
  messages: TicketMessage[]
}

export interface TicketPage {
  items: TicketSummary[]
  total: number
  limit: number
  offset: number
}

export interface SupportSummary {
  open_count: number
  waiting_count: number
  resolved_count: number
  /**
   * Si este workspace puede abrir tickets `URGENT`.
   *
   * El panel lo lee para decidir si el selector muestra la opción activa o bloqueada con
   * su candado. La garantía es el `403` del servidor: esto es cortesía, no permiso.
   */
  can_request_urgent: boolean
  plan_tier: string
}

export interface TicketCreatePayload {
  subject: string
  category: TicketCategory
  priority: TicketPriority
  message: string
}

export interface TicketReplyPayload {
  content: string
}

/** Filtros de la cola global de soporte. Todos opcionales. */
export interface AdminTicketFilters {
  status?: TicketStatus
  priority?: TicketPriority
  search?: string
  limit?: number
  offset?: number
}

/**
 * Filtros de la vista de cliente, en `/settings/support`.
 *
 * `query` es el nombre que usa el endpoint, no `search`: son dos rutas distintas con dos
 * clientes distintos y cada una escribe en su cliente. El parámetro `search` de la consola se
 * conserva porque renombrar uno que ya está en uso rompe a su llamador sin avisar.
 */
export interface TicketFilters {
  status?: TicketStatus
  query?: string
  /** Fecha de alta en formato `AAAA-MM-DD`. */
  created_from?: string
  /** Fecha de alta en formato `AAAA-MM-DD`, **inclusiva**: cubre el día entero. */
  created_to?: string
  limit?: number
  offset?: number
}

/**
 * Cambio administrativo sobre un ticket.
 *
 * `assigned_to_user_id` es opcional y **distinguible de "no lo he tocado"**: mandarlo a
 * `null` desasigna, omitirlo deja el agente como estaba. El backend usa `model_fields_set`
 * para esa distinción, y un `PATCH` con el objeto entero no podría expresarla.
 */
export interface AdminTicketUpdate {
  status?: TicketStatus
  priority?: TicketPriority
  assigned_to_user_id?: string | null
}
