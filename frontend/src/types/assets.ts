/**
 * Tipos de la superficie de ataque: dominios verificados y activos descubiertos.
 *
 * ## Por qué los nombres de campo van en `snake_case` sin renombrar
 *
 * Porque son los que manda el servidor. Convertirlos al montar los objetos añade una capa
 * que funciona con los campos de hoy y rompe en cuanto el backend añade uno, y el fallo es
 * silencioso: el campo llega `undefined` y la tabla pinta una celda vacía en lugar de
 * avisar. Con el tipo como contrato, TypeScript falla al compilar el día que el backend
 * cambia la forma.
 */

/** Cómo se demuestra que el workspace controla el dominio. */
export type DomainVerificationMethod = 'DNS_TXT' | 'HTTP_FILE'

/** Los seis estados de una comprobación de DNS. Todos son resultados, ninguno un fallo. */
export type DnsLookupOutcome = 'MATCH' | 'MISMATCH' | 'NO_TXT' | 'NXDOMAIN' | 'TIMEOUT' | 'ERROR'

/** La clase de activo descubierto. */
export type AssetType = 'SUBDOMAIN' | 'IP_ADDRESS' | 'API_ENDPOINT'

/** Por qué se rechaza un alta de dominio. */
export type DomainClaimConflict = 'OTRO_WORKSPACE' | 'ESTE_WORKSPACE' | 'CARRERA'

export interface VerifiedDomain {
  id: string
  domain_name: string
  is_verified: boolean
  verified_at: string | null
  verification_method: DomainVerificationMethod
  /** Nombre completo del registro TXT a consultar, ya con el dominio. */
  txt_record_name: string
  /** Valor TXT exacto que hay que publicar. */
  txt_record_value: string
  created_at: string
  updated_at: string
  /** Activos descubiertos bajo este dominio, contado en la misma consulta del listado. */
  asset_count: number
}

export interface DomainListResponse {
  items: VerifiedDomain[]
  total: number
}

export interface DomainCreatePayload {
  domain_name: string
  verification_method: DomainVerificationMethod
}

export interface VerifyDomainResponse {
  outcome: DnsLookupOutcome
  is_verified: boolean
  expected_value: string
  found_values: string[]
  /** Clave de i18n del mensaje. La decide el backend, la traduce el panel. */
  message_key: string
}

export interface DiscoveredAsset {
  id: string
  domain_id: string
  domain_name: string
  asset_type: AssetType
  value: string
  service_name: string | null
  technologies: string[]
  last_scanned_at: string | null
  created_at: string
}

export interface AssetListResponse {
  items: DiscoveredAsset[]
  total: number
  limit: number
  offset: number
}

export interface DiscoveryEnqueuedResponse {
  task_id: string
  domain_name: string
  existing_assets: number
}

export interface AssetFilters {
  domain_id?: string
  asset_type?: AssetType
  limit?: number
  offset?: number
}
