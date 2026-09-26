/**
 * Cliente de la consola de SuperAdmin y de la facturación del cliente.
 *
 * Va en un fichero aparte del resto de `api.ts` por una razón concreta: **estas rutas no
 * aceptan `X-Organization-Id`**. Todas las demás pasan por el `request` con el tenant
 * resuelto desde el contexto, porque operan sobre el workspace de alguien. La consola de
 * administración opera sobre la plataforma, y mandarle la cabecera sería enviar un filtro
 * que el servidor ignora: el operador creería estar viendo su propio tenant cuando está
 * viendo los doscientos.
 *
 * Por eso aquí `organizationId` no aparece en las firmas de administración. Si algún día
 * alguien añade una función de admin a `api.ts` y le pone el tenant por costumbre, el
 * código compila y la vista se acota sin avisar. La separación lo hace visible.
 */

import { API_BASE_URL } from '../config'
import type {
  AdminAuditEntry,
  AdminCreditGrantResult,
  AdminMetric,
  AdminOrganization,
  AdminOrganizationPage,
  AdminSale,
  AdminSalePage,
  AdminUser,
  AdminUserPage,
  InfrastructureHealth,
  PlanTierAdmin,
  TenantLifecycle,
} from '../types/api'
import type { BillingSummary, CheckoutSession, CreditLedgerEntry } from '../types/billing'
import { ApiError } from './api'

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

/**
 * Construye una query string descartando lo vacío.
 *
 * Los valores ausentes y los `false` se omiten. Omitir un `false` es seguro **porque el
 * servidor trata la ausencia de un filtro como "sin filtro"**, y esa equivalencia está
 * escrita aquí para que no se dé por hecha: si algún día un parámetro pasa a tener un
 * valor por defecto distinto, esta función tiene que dejar de descartarlo.
 */
function query(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const search = new URLSearchParams()
  for (const [clave, valor] of Object.entries(params)) {
    // `false` se descarta igual que un valor ausente: el servidor ya trata la ausencia
    // de `only_superusers` como "no filtrar", y mandar `false` explícito solo añadiría
    // caracteres a cada petición de la lista de usuarios sin cambiar el resultado.
    if (valor === undefined || valor === null || valor === '' || valor === false) {
      continue
    }
    search.set(clave, String(valor))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ''
}

// --------------------------------------------------------------------------- //
// Resumen global
// --------------------------------------------------------------------------- //

export function getAdminOverview(token: string): Promise<{
  metrics: AdminMetric[]
  infrastructure: InfrastructureHealth
  generated_at: string
}> {
  return adminRequest('/api/v1/admin/overview', {}, token)
}

// --------------------------------------------------------------------------- //
// Tenants
// --------------------------------------------------------------------------- //

export function getAdminOrganizations(
  token: string,
  params: {
    plan?: PlanTierAdmin | null
    lifecycle?: TenantLifecycle | null
    search?: string | null
    limit?: number
    offset?: number
  } = {},
): Promise<AdminOrganizationPage> {
  return adminRequest(
    `/api/v1/admin/organizations${query({ limit: 50, offset: 0, ...params })}`,
    {},
    token,
  )
}

export function updateAdminOrganizationPlan(
  token: string,
  organizationId: string,
  planTier: PlanTierAdmin,
): Promise<AdminOrganization> {
  return adminRequest(
    `/api/v1/admin/organizations/${organizationId}`,
    { method: 'PATCH', body: JSON.stringify({ plan_tier: planTier }) },
    token,
  )
}

export function grantAdminCredits(
  token: string,
  organizationId: string,
  amount: string,
  note = '',
): Promise<AdminCreditGrantResult> {
  return adminRequest(
    `/api/v1/admin/organizations/${organizationId}/credits`,
    { method: 'POST', body: JSON.stringify({ amount, note }) },
    token,
  )
}

export function deactivateAdminOrganization(
  token: string,
  organizationId: string,
): Promise<AdminOrganization> {
  return adminRequest(
    `/api/v1/admin/organizations/${organizationId}`,
    { method: 'DELETE' },
    token,
  )
}

// --------------------------------------------------------------------------- //
// Usuarios
// --------------------------------------------------------------------------- //

export function getAdminUsers(
  token: string,
  params: { search?: string | null; onlySuperusers?: boolean; limit?: number; offset?: number } = {},
): Promise<AdminUserPage> {
  return adminRequest(
    `/api/v1/admin/users${query({ limit: 50, offset: 0, ...params })}`,
    {},
    token,
  )
}

// --------------------------------------------------------------------------- //
// Ventas
// --------------------------------------------------------------------------- //

export function getAdminSales(
  token: string,
  params: { organizationId?: string | null; limit?: number; offset?: number } = {},
): Promise<AdminSalePage> {
  return adminRequest(
    `/api/v1/admin/sales${query({ limit: 50, offset: 0, ...params })}`,
    {},
    token,
  )
}

export type { AdminSale, AdminUser }

// --------------------------------------------------------------------------- //
// Auditoría global
// --------------------------------------------------------------------------- //

export interface AdminAuditFilters {
  organizationId?: string | null
  action?: string | null
  search?: string | null
  limit?: number
  offset?: number
}

export function getAdminAuditLog(
  token: string,
  filters: AdminAuditFilters = {},
): Promise<{ items: AdminAuditEntry[]; total: number; limit: number; offset: number }> {
  return adminRequest(
    `/api/v1/admin/audit-log${query({
      limit: 50,
      offset: 0,
      organization_id: filters.organizationId,
      action: filters.action,
      search: filters.search,
    })}`,
    {},
    token,
  )
}

// --------------------------------------------------------------------------- //
// Facturación del cliente
// --------------------------------------------------------------------------- //
//
// Estas tres sí son del cliente y sí llevan tenant. Viven aquí para que la vista de
// facturación tenga sus imports de un sitio, no porque pertenezcan al mismo plano: el
// `adminRequest` de arriba no manda la cabecera, y reutilizarlo sin más devolvería un
// `403` en la primera pantalla que se pintara.

async function requestWithTenant<T>(
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
  headers.set('X-Organization-Id', organizationId)

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status)
  }
  return (await response.json()) as T
}

export function getBillingSummary(token: string, organizationId: string): Promise<BillingSummary> {
  return requestWithTenant<BillingSummary>('/api/v1/billing/summary', {}, token, organizationId)
}

export function getCreditLedger(
  token: string,
  organizationId: string,
): Promise<CreditLedgerEntry[]> {
  return requestWithTenant<CreditLedgerEntry[]>(
    '/api/v1/billing/credits/ledger',
    {},
    token,
    organizationId,
  )
}

export function createCheckoutSession(
  token: string,
  organizationId: string,
  credits: number,
  successUrl: string,
  cancelUrl: string,
): Promise<CheckoutSession> {
  return requestWithTenant<CheckoutSession>(
    '/api/v1/billing/checkout-session',
    {
      method: 'POST',
      body: JSON.stringify({ credits, success_url: successUrl, cancel_url: cancelUrl }),
    },
    token,
    organizationId,
  )
}
