/**
 * Cliente de la superficie de ataque.
 *
 * ## Por qué hay un error propio y no se reutiliza `ApiError`
 *
 * Porque el `409` del alta de dominio lleva una **estructura** —el motivo del conflicto— y
 * `ApiError` solo conserva el código. Con `ApiError` el panel tendría dos opciones y las dos
 * son malas: adivinar por el texto, que ata la decisión a un idioma, o tratar todo `409`
 * igual, que muestra "lo tiene otra organización" a un usuario que ya lo tenía en su
 * espacio de trabajo.
 *
 * `DomainConflictError` transporta el motivo ya tipado, y el componente decide qué botón
 * ofrecer sin leer español.
 *
 * ## Por qué el cuerpo se lee una sola vez y con guarda
 *
 * Porque un `response.json()` sobre un `204` —que es lo que devuelve el borrado— lanza
 * `SyntaxError`. Sin comprobar el estado antes de leer, el borrado de un dominio fallaría
 * con un error de parseo en vez de con un éxito, y el usuario vería un aviso de problema
 * después de que la operación se hizo bien.
 */

import { API_BASE_URL } from '../config'
import type {
  AssetFilters,
  AssetListResponse,
  DiscoveryEnqueuedResponse,
  DomainCreatePayload,
  DomainListResponse,
  DomainClaimConflict,
  VerifiedDomain,
  VerifyDomainResponse,
} from '../types/assets'
import { ApiError } from './api'

/**
 * El `409` del alta de dominio, con su motivo ya resuelto.
 *
 * `motivo` es `'DESCONOCIDO'` y no `null` para que el switch del panel sea exhaustivo: con
 * `null` habría que tratar el caso imposible, y con un valor fuera de la unión no. Un
 * servidor más nuevo que añada un motivo se vería aquí, que es donde tiene que verse.
 */
export class DomainConflictError extends Error {
  readonly motivo: DomainClaimConflict | 'DESCONOCIDO'
  readonly alreadyVerified: boolean
  readonly domainName: string

  constructor(
    motivo: DomainClaimConflict | 'DESCONOCIDO',
    alreadyVerified: boolean,
    domainName: string,
  ) {
    super('domain_conflict')
    this.name = 'DomainConflictError'
    this.motivo = motivo
    this.alreadyVerified = alreadyVerified
    this.domainName = domainName
  }
}

const MOTIVOS: readonly string[] = ['OTRO_WORKSPACE', 'ESTE_WORKSPACE', 'CARRERA']

/**
 * Lee un `409` y extrae su motivo.
 *
 * Se hace a mano y no con un type guard sobre `unknown` porque el cuerpo viene de `any` en
 * cuanto se parsea, y un type guard sobre `any` no comprueba nada. Se declara la forma
 * esperada y se comprueba campo a campo.
 */
function toConflictError(payload: unknown, fallbackName: string): DomainConflictError {
  if (typeof payload !== 'object' || payload === null) {
    return new DomainConflictError('DESCONOCIDO', false, fallbackName)
  }
  const detalle = payload as Record<string, unknown>
  const motivo = typeof detalle.motivo === 'string' ? detalle.motivo : 'DESCONOCIDO'
  return new DomainConflictError(
    (MOTIVOS as string[]).includes(motivo) ? (motivo as DomainClaimConflict) : 'DESCONOCIDO',
    detalle.already_verified === true,
    typeof detalle.domain_name === 'string' ? detalle.domain_name : fallbackName,
  )
}

async function tenantRequest<T>(
  path: string,
  init: RequestInit,
  token: string,
  organizationId: string,
  domainNameForError = '',
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Authorization', `Bearer ${token}`)
  headers.set('X-Organization-Id', organizationId)

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })

  if (response.status === 409) {
    // El cuerpo del `409` es el que dice por qué. Si no se puede leer, se degrada a
    // `DESCONOCIDO` en vez de reventar: el usuario tiene que saber igualmente que el
    // dominio está ocupado, aunque el botón sea el genérico.
    let cuerpo: unknown = null
    try {
      cuerpo = await response.json()
    } catch {
      cuerpo = null
    }
    throw toConflictError(cuerpo, domainNameForError)
  }

  if (!response.ok) {
    throw new ApiError(response.status)
  }

  if (response.status === 204) {
    // El borrado no tiene cuerpo que leer. Sin este early return, el `json()` de abajo
    // lanzaría `SyntaxError` sobre una respuesta vacía.
    return undefined as T
  }

  return (await response.json()) as T
}

/** Construye la query string del inventario descartando lo que no está puesto. */
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
// Dominios
// --------------------------------------------------------------------------- //

export function listDomains(token: string, organizationId: string): Promise<DomainListResponse> {
  return tenantRequest<DomainListResponse>(
    '/api/v1/assets/domains',
    { method: 'GET' },
    token,
    organizationId,
  )
}

export function createDomain(
  token: string,
  organizationId: string,
  payload: DomainCreatePayload,
): Promise<VerifiedDomain> {
  return tenantRequest<VerifiedDomain>(
    '/api/v1/assets/domains',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
    payload.domain_name,
  )
}

export function verifyDomain(
  token: string,
  organizationId: string,
  domainId: string,
): Promise<VerifyDomainResponse> {
  return tenantRequest<VerifyDomainResponse>(
    `/api/v1/assets/domains/${domainId}/verify`,
    { method: 'POST' },
    token,
    organizationId,
  )
}

export function deleteDomain(
  token: string,
  organizationId: string,
  domainId: string,
): Promise<void> {
  return tenantRequest<void>(
    `/api/v1/assets/domains/${domainId}`,
    { method: 'DELETE' },
    token,
    organizationId,
  )
}

// --------------------------------------------------------------------------- //
// Activos descubiertos
// --------------------------------------------------------------------------- //

export function listDiscoveredAssets(
  token: string,
  organizationId: string,
  filters: AssetFilters,
): Promise<AssetListResponse> {
  return tenantRequest<AssetListResponse>(
    `/api/v1/assets/discovery${query({
      domain_id: filters.domain_id,
      asset_type: filters.asset_type,
      limit: filters.limit,
      offset: filters.offset,
    })}`,
    { method: 'GET' },
    token,
    organizationId,
  )
}

export function startDiscovery(
  token: string,
  organizationId: string,
  domainId: string,
): Promise<DiscoveryEnqueuedResponse> {
  return tenantRequest<DiscoveryEnqueuedResponse>(
    `/api/v1/assets/domains/${domainId}/discover`,
    { method: 'POST' },
    token,
    organizationId,
  )
}
