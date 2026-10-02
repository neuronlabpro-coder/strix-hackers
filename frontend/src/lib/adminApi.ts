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
  AdminAgentPage,
  AdminAuditEntry,
  AdminCreditGrantResult,
  AdminMetric,
  AdminOrganization,
  AdminOrganizationPage,
  AdminSale,
  AdminSalePage,
  AdminUser,
  AdminUserPage,
  AdminUserUpdate,
  InfrastructureHealth,
  PlanTierAdmin,
  TenantLifecycle,
} from '../types/api'
import type {
  AbortReason,
  CancelarScanResponse,
  ContenedorPage,
  JobOperacionPage,
  LimpiarContenedorResponse,
  ReviewOperacionPage,
  ScanOperacionPage,
} from '../types/api' 
import type { BillingSummary, CheckoutSession, CreditLedgerEntry } from '../types/billing'
import type {
  PlatformCreditPack,
  PlatformCreditPackCreatePayload,
  PlatformCreditPackUpdatePayload,
  PlatformPriceChangePage,
  PlatformPricing,
  PlatformPricingDetail,
  PlatformPricingUpdatePayload,
  PlatformVolumeTier,
  PlatformVolumeTierCreatePayload,
  PlatformVolumeTierUpdatePayload,
} from '../types/api'
import type {
  OrganizationPriceOverrideWrite,
  OrganizationPricingDetail,
  PactadoPrecio,
} from '../types/api'

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
    `/api/v1/admin/tenants${query({ limit: 50, offset: 0, ...params })}`,
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
    `/api/v1/admin/tenants/${organizationId}`,
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
    `/api/v1/admin/tenants/${organizationId}/credits`,
    { method: 'POST', body: JSON.stringify({ amount, note }) },
    token,
  )
}

export function deactivateAdminOrganization(
  token: string,
  organizationId: string,
): Promise<AdminOrganization> {
  return adminRequest(
    `/api/v1/admin/tenants/${organizationId}`,
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

/**
 * Activa o desactiva una cuenta, y le da o le quita el superusuario.
 *
 * Un campo ausente no se manda, y `false` sí. El backend los distingue —desactivar es
 * `{"is_active": false}` y "no cambiar" es `{}`— y confundirlos convertiría una baja de
 * cuenta en una promoción de permisos.
 */
export function updateAdminUser(
  token: string,
  userId: string,
  changes: AdminUserUpdate,
): Promise<AdminUser> {
  return adminRequest(
    `/api/v1/admin/users/${userId}`,
    { method: 'PATCH', body: JSON.stringify(changes) },
    token,
  )
}
// --------------------------------------------------------------------------- //
// Agentes de escaneo
// --------------------------------------------------------------------------- //

/**
 * Los agentes de **todos** los tenants, con el nombre de su organizacion.
 *
 * ## Por que esta ruta no lleva `organizationId`
 *
 * Por lo que dice el comentario de la cabecera del fichero, y aqui con mas fuerza: el
 * operador de plataforma tiene una pregunta que ningun cliente puede contestar —si los agentes
 * estan conectados— y esa pregunta solo tiene respuesta desde fuera. Anadirle el tenant por
 * costumbre haria que la pantalla pareciera acotada cuando justamente lo que se busca es no
 * estarlo.
 */
export function getAdminAgents(
  token: string,
  params: { limit?: number; offset?: number } = {},
): Promise<AdminAgentPage> {
  return adminRequest(`/api/v1/admin/agents${query({ limit: 50, offset: 0, ...params })}`, {}, token)
}

/**
 * Da de baja el agente de un cliente desde la consola.
 *
 * ## Por que tiene su propia ruta y no la del panel del cliente
 *
 * Porque la del panel resuelve el tenant por `X-Organization-Id` y comprueba que el agente es
 * de ese tenant. Aqui no hay tenant: un operador de plataforma no pertenece a un workspace.
 * Reutilizar la ruta obligaria a inventar uno en la peticion solo para que la comprobacion
 * pasara, que es aislamiento de mentira: la vista seguiria cruzando tenants, con un disfraz.
 *
 * Y el motivo es **obligatorio** aqui y opcional en el panel, y la diferencia es quien esta
 * pulsando: un corte sobre la red de otro cliente sin explicacion es indistinguible de una
 * intervencion sin justificar.
 */
export function revokeAgentAsAdmin(
  token: string,
  agentId: string,
  reason: string,
): Promise<{ estado: string }> {
  return adminRequest(
    `/api/v1/admin/agents/${agentId}/revoke`,
    { method: 'POST', body: JSON.stringify({ reason }) },
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
    `/api/v1/admin/audit${query({
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

/**
 * Abre una sesión de Stripe Checkout.
 *
 * ## Por qué el cuerpo solo lleva `mode` y `credits`
 *
 * Ni el importe ni el descuento viajan. El precio lo decide `price_for_credits` en el
 * servidor y el tramo lo decide la escalera: mandar cualquiera de los dos sería aceptar
 * que quien llama decide cuánto paga por cuánto, que es la definición de un endpoint de
 * cobro roto. El backend los rechaza con un `422` de `extra="forbid"`, así que ni
 * siquiera hay forma de intentarlo.
 *
 * En modo `subscription` el `credits` **no** se manda. El esquema lo rechaza: pagar la cuota
 * y además acreditar créditos es cobrar dos veces por lo mismo, y el cliente creería haber
 * comprado saldo.
 */
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
      body: JSON.stringify({
        mode: 'credits',
        credits,
        success_url: successUrl,
        cancel_url: cancelUrl,
      }),
    },
    token,
    organizationId,
  )
}

/**
 * Abre la suscripción Pro.
 *
 * Es una función aparte y no un `mode` opcional en la anterior porque el cuerpo es otro: no
 * lleva créditos, y mandar un `0` —que es lo que daría un `credits: 0` con el esquema
 * actual— lo rechazaría el validador de mínimo. Dos petitiones con dos cuerpos distintos
 * son dos funciones.
 */
export function createSubscriptionCheckout(
  token: string,
  organizationId: string,
  successUrl: string,
  cancelUrl: string,
): Promise<CheckoutSession> {
  return requestWithTenant<CheckoutSession>(
    '/api/v1/billing/checkout-session',
    {
      method: 'POST',
      body: JSON.stringify({
        mode: 'subscription',
        success_url: successUrl,
        cancel_url: cancelUrl,
      }),
    },
    token,
    organizationId,
  )
}

// --------------------------------------------------------------------------- //
// Precios de plataforma
// --------------------------------------------------------------------------- //
//
// ## Por que van en este fichero y no en `lib/api.ts`
//
// Porque `lib/api.ts` manda `X-Organization-Id` en cada peticion, y un precio de plataforma
// **no pertenece a ninguna organizacion**: pertenece a la plataforma. Mandar el identificador
// de un tenant a una ruta que no lo usa es la forma de que alguien lo lea despues como un
// filtro implicito y anada un dia el precio por organizacion sin darse cuenta. Aqui la
// ausencia del identificador es la propia documentacion de que la ruta cruza tenants.
//
// Y ahi vive `getLLMModels` sin embargo, que si cruza tenants. Es deuda, no patron.

export function getPlatformPricing(token: string): Promise<PlatformPricingDetail> {
  return adminRequest('/api/v1/admin/pricing', {}, token)
}

export function updatePlatformPricing(
  token: string,
  payload: PlatformPricingUpdatePayload,
): Promise<PlatformPricing> {
  return adminRequest(
    '/api/v1/admin/pricing',
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
  )
}

export function createPlatformCreditPack(
  token: string,
  payload: PlatformCreditPackCreatePayload,
): Promise<PlatformCreditPack> {
  return adminRequest(
    '/api/v1/admin/pricing/packs',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
  )
}

export function updatePlatformCreditPack(
  token: string,
  packId: string,
  payload: PlatformCreditPackUpdatePayload,
): Promise<PlatformCreditPack> {
  return adminRequest(
    `/api/v1/admin/pricing/packs/${encodeURIComponent(packId)}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
  )
}

export function createPlatformVolumeTier(
  token: string,
  payload: PlatformVolumeTierCreatePayload,
): Promise<PlatformVolumeTier> {
  return adminRequest(
    '/api/v1/admin/pricing/tiers',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
  )
}

export function updatePlatformVolumeTier(
  token: string,
  tierId: string,
  payload: PlatformVolumeTierUpdatePayload,
): Promise<PlatformVolumeTier> {
  return adminRequest(
    `/api/v1/admin/pricing/tiers/${encodeURIComponent(tierId)}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
  )
}

/** Filtros del historico de precios. */
export interface PlatformPriceChangeFilters {
  clave?: string | null
  limit?: number
  offset?: number
}

/**
 * El historico de precios, del mas reciente al mas antiguo.
 *
 * ## Por que filtra por clave y no por fecha
 *
 * Porque la pregunta que se hace es «quien movio este precio», no «que paso tal dia». La clave
 * identifica el precio —un escalar, o el identificador de un pack o de un tramo— y el filtro va
 * ahi. Filtrar por rango de fechas sobre una tabla append-only obliga a paginar desde el origen,
 * que es justo lo que hace lento un historico que solo crece.
 */
export function getPlatformPriceChanges(
  token: string,
  filters: PlatformPriceChangeFilters = {},
): Promise<PlatformPriceChangePage> {
  return adminRequest(
    `/api/v1/admin/pricing/changes${query({
      limit: 50,
      offset: 0,
      clave: filters.clave,
    })}`,
    {},
    token,
  )
}


// --------------------------------------------------------------------------- //
// Consola de operaciones
// --------------------------------------------------------------------------- //
//
// ## Por que van en este fichero y no en `lib/api.ts`
//
// Porque son rutas de alcance plataforma: no aceptan `X-Organization-Id` y su respuesta trae ya
// el nombre de la organizacion de cada fila. Si se mandara un identificador de tenant, un dia
// alguien lo leeria como un filtro y las listas dejarian de mostrarlos todos, que es justo lo que
// esta consola tiene que hacer.

export interface OperationsFilters {
  status?: string | null
  organizationId?: string | null
  soloColgados?: boolean
  limit?: number
  offset?: number
}

export function getAdminScans(
  token: string,
  filters: OperationsFilters = {},
): Promise<ScanOperacionPage> {
  return adminRequest(
    `/api/v1/admin/operations/scans${query({
      limit: 50,
      offset: 0,
      estado: filters.status,
      organization_id: filters.organizationId,
      solo_colgados: filters.soloColgados,
    })}`,
    {},
    token,
  )
}

/**
 * Cancela un escaneo y devuelve el dinero si el motivo obliga.
 *
 * ## Por que el motivo va en el cuerpo y no se deduce
 *
 * Porque un proceso colgado y una cancelacion del cliente se ven **exactamente igual**: los dos
 * estan en `RUNNING`. Si el servidor lo dedujera del tiempo transcurrido, un cliente que
 * cancela a los cinco minutos y un proceso que lleva cuarenta colgado acabarian con el mismo
 * trato, y uno de los dos esta mal.
 */
export function cancelAdminScan(
  token: string,
  runId: string,
  motivo: AbortReason,
  nota: string,
): Promise<CancelarScanResponse> {
  return adminRequest(
    `/api/v1/admin/operations/scans/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST', body: JSON.stringify({ motivo, nota }) },
    token,
  )
}

/**
 * Limpia el contenedor, la red y el directorio de un run terminado.
 *
 * ## Por que es una ruta y no `cancelar`
 *
 * Porque limpiar un contenedor **no** es cancelar el trabajo. Un run `FAILED` con
 * `cleanup_pending` ya termino —con sus hallazgos y su evidencia emitidos— y lo unico que sigue
 * vivo son los recursos. Cancelar un run terminado da `409`, correctamente.
 */
export function cleanupAdminScanContainer(
  token: string,
  runId: string,
): Promise<LimpiarContenedorResponse> {
  return adminRequest(
    `/api/v1/admin/operations/scans/${encodeURIComponent(runId)}/cleanup`,
    { method: 'POST', body: JSON.stringify({}) },
    token,
  )
}

export function getAdminContainers(token: string): Promise<ContenedorPage> {
  return adminRequest('/api/v1/admin/operations/containers', {}, token)
}

export function getAdminReviews(
  token: string,
  filters: OperationsFilters = {},
): Promise<ReviewOperacionPage> {
  return adminRequest(
    `/api/v1/admin/operations/reviews${query({
      limit: 50,
      offset: 0,
      estado: filters.status,
      organization_id: filters.organizationId,
    })}`,
    {},
    token,
  )
}

export function getAdminJobs(
  token: string,
  filters: OperationsFilters = {},
): Promise<JobOperacionPage> {
  return adminRequest(
    `/api/v1/admin/operations/jobs${query({
      limit: 50,
      offset: 0,
      estado: filters.status,
      organization_id: filters.organizationId,
    })}`,
    {},
    token,
  )
}

/**
 * Los precios que rigen a una organización, con lo pactado encima de la plataforma.
 *
 * ## Por qué esto no lleva cabecera de organización
 *
 * Porque va por `adminRequest`, que no manda `X-Organization-Id`, y porque el `organization_id` va
 * en la ruta y es el que se busca. Si además mandara un tenant, habría dos fuentes de verdad
 * para saber de quién es la ficha, y la que acertara mal sería la del cuerpo.
 */
export function getOrganizationPricing(
  token: string,
  organizationId: string,
): Promise<OrganizationPricingDetail> {
  return adminRequest(
    `/api/v1/admin/organizations/${encodeURIComponent(organizationId)}/price-overrides`,
    {},
    token,
  )
}

/** Pacta un precio. Sustituye al vigente si lo hay, sin borrar nada. */
export function createOrganizationPriceOverride(
  token: string,
  organizationId: string,
  payload: OrganizationPriceOverrideWrite,
): Promise<PactadoPrecio> {
  return adminRequest(
    `/api/v1/admin/organizations/${encodeURIComponent(organizationId)}/price-overrides`,
    { method: 'POST', body: JSON.stringify(payload) },
    token,
  )
}
