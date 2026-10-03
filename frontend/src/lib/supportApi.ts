/**
 * Cliente del sistema de tickets de soporte.
 *
 * ## Por qué hay dos grupos de funciones y no una con un parámetro de rol
 *
 * Es el mismo motivo que en el backend. Una función que decide "soy cliente o soy soporte"
 * según quién llama tiene la mitad de sus ramas probadas en cada modo, y un fallo en la
 * condición deja que un cliente escriba como soporte. Aquí el corte es físico: las funciones
 * de administración **no** aceptan `organizationId` y las de cliente **no** aceptan token de
 * superusuario como autoridad, así que no hay ninguna condición que pueda decidir mal.
 *
 * ## Por qué ninguna función acepta `is_admin_reply`
 *
 * Porque el servidor lo deduce de la ruta. Un parámetro en el cliente sería un parámetro que
 * el backend ignora, y un parámetro que el backend ignora en un campo de autorización es una
 * invitación a confiar en él.
 */

import { API_BASE_URL } from '../config'
import type {
  AdminTicketFilters,
  AdminTicketUpdate,
  SupportSummary,
  TicketCreatePayload,
  TicketDetail,
  TicketFilters,
  TicketMessage,
  TicketPage,
  TicketReplyPayload,
} from '../types/support'
import { ApiError } from './api'

/**
 * Petición con tenant.
 *
 * `organizationId` es obligatorio en las cinco funciones de cliente. No es opcional con
 * valor por defecto: un `undefined` silencioso produciría un `403` cuyo mensaje no diría
 * qué faltaba, y el diagnóstico apuntaría al servidor cuando el problema es la llamada.
 */
async function tenantRequest<T>(
  path: string,
  init: RequestInit,
  token: string,
  organizationId: string,
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Authorization', `Bearer ${token}`)
  // Solo se pone si hay valor. Mandar la cabecera **vacia** no es lo mismo que no mandarla:
  headers.set('X-Organization-Id', organizationId)

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status)
  }
  return (await response.json()) as T
}

/**
 * Petición de la consola de soporte.
 *
 * **No** manda `X-Organization-Id`. La consola cruza tenants y mandar la cabecera sería
 * enviar un filtro que el servidor ignora, además de sugerir que la vista está acotada. La
 * frontera real es `is_superuser`, y la aplica el servidor.
 */
async function adminRequest<T>(path: string, init: RequestInit, token: string): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Authorization', `Bearer ${token}`)

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status)
  }
  return (await response.json()) as T
}

/** Construye la query string de la cola de soporte descartando lo vacío. */
function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [clave, valor] of Object.entries(params)) {
    if (valor === undefined || valor === '') continue
    search.set(clave, String(valor))
  }
  const serializado = search.toString()
  return serializado ? `?${serializado}` : ''
}

// --------------------------------------------------------------------------- //
// Cliente
// --------------------------------------------------------------------------- //

export function getSupportSummary(
  token: string,
  organizationId: string,
): Promise<SupportSummary> {
  return tenantRequest<SupportSummary>(
    '/api/v1/support/summary',
    { method: 'GET' },
    token,
    organizationId,
  )
}

/**
 * Los tickets del workspace, con filtros y paginación.
 *
 * Antes solo aceptaba un `status` suelto y sin `limit`, así que la vista se quedaba con las
 * primeras veinticinco filas del workspace y las demás no existían para nadie: no había forma de
 * llegar a ellas. Los filtros viajan en la query y **no** se aplican en el cliente, por la razón
 * de siempre: sin `total` no hay forma de saber si lo que no sale de la respuesta existe.
 */
export function getMyTickets(
  token: string,
  organizationId: string,
  filters: TicketFilters = {},
): Promise<TicketPage> {
  return tenantRequest<TicketPage>(
    `/api/v1/support/tickets${query({
      status: filters.status,
      query: filters.query,
      created_from: filters.created_from,
      created_to: filters.created_to,
      limit: filters.limit,
      offset: filters.offset,
    })}`,
    { method: 'GET' },
    token,
    organizationId,
  )
}

export function getMyTicket(
  token: string,
  organizationId: string,
  ticketId: string,
): Promise<TicketDetail> {
  return tenantRequest<TicketDetail>(
    `/api/v1/support/tickets/${ticketId}`,
    { method: 'GET' },
    token,
    organizationId,
  )
}

/**
 * Abre un ticket con su primer mensaje.
 *
 * `URGENT` sin plan Enterprise devuelve `403`. La función no lo impide: la garantía es del
 * servidor, y un filtro en el cliente que lo comprobara y devolviera `0` sería una segunda
 * regla que divergiría de la primera en cuanto una se actualizara.
 */
export function createTicket(
  token: string,
  organizationId: string,
  payload: TicketCreatePayload,
): Promise<TicketDetail> {
  return tenantRequest<TicketDetail>(
    '/api/v1/support/tickets',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

/**
 * Añade un mensaje del cliente al hilo.
 *
 * Devuelve `409` si el ticket está resuelto o cerrado: el hilo terminó y hay que abrir
 * otro. Escribir ahí reabriría una conversación que el soporte dio por buena sin que el
 * agente se entere.
 */
export function replyToTicket(
  token: string,
  organizationId: string,
  ticketId: string,
  payload: TicketReplyPayload,
): Promise<TicketMessage> {
  return tenantRequest<TicketMessage>(
    `/api/v1/support/tickets/${ticketId}/messages`,
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

// --------------------------------------------------------------------------- //
// Consola de soporte
// --------------------------------------------------------------------------- //

export function getAdminTickets(
  token: string,
  filters: AdminTicketFilters = {},
): Promise<TicketPage> {
  return adminRequest<TicketPage>(
    `/api/v1/admin/tickets${query({
      status: filters.status,
      priority: filters.priority,
      search: filters.search,
      limit: filters.limit,
      offset: filters.offset,
    })}`,
    { method: 'GET' },
    token,
  )
}

export function getAdminTicket(token: string, ticketId: string): Promise<TicketDetail> {
  return adminRequest<TicketDetail>(
    `/api/v1/admin/tickets/${ticketId}`,
    { method: 'GET' },
    token,
  )
}

/**
 * Cambia estado, prioridad o agente asignado.
 *
 * `assigned_to_user_id` solo se manda si quien llama ha decidido algo sobre la asignación.
 * El backend distingue "lo he quitado" de "no lo he pensado" por las claves que viajan, así
 * que incluir siempre la clave —aunque valga `null`— vaciaría el agente en cada cambio de
 * estado. Ver `AdminTicketUpdate`.
 */
export function updateAdminTicket(
  token: string,
  ticketId: string,
  payload: AdminTicketUpdate,
): Promise<TicketDetail> {
  return adminRequest<TicketDetail>(
    `/api/v1/admin/tickets/${ticketId}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
  )
}

/**
 * Responde como soporte.
 *
 * Es la ruta gemela de `replyToTicket` y la única diferencia visible es que no lleva
 * `X-Organization-Id`. El `is_admin_reply` lo pone el servidor, y por eso no aparece en
 * `TicketReplyPayload`.
 */
export function replyAsSupport(
  token: string,
  ticketId: string,
  payload: TicketReplyPayload,
): Promise<TicketMessage> {
  return adminRequest<TicketMessage>(
    `/api/v1/admin/tickets/${ticketId}/messages`,
    { method: 'POST', body: JSON.stringify(payload) },
    token,
  )
}
