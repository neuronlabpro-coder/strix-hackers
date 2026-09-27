import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  createApiToken,
  getApiScopes,
  listApiTokens,
  revokeApiToken,
} from '../../lib/api'
import type { ApiScopeCatalog, ApiToken, ApiTokenCreated } from '../../types/api'
import { useAuth } from '../auth/useAuth'

/**
 * Estado de la pantalla de API Access.
 *
 * El catálogo de scopes y el listado de tokens se piden a la vez porque son
 * independientes: el selector necesita los 46 nombres para pintar y la tabla necesita las
 * filas, y esperarlos en serie retrasaría el primer pintado sin motivo.
 */
export interface ApiAccessState {
  catalog: ApiScopeCatalog
  tokens: ApiToken[]
  isLoading: boolean
  loadFailed: boolean
  includeRevoked: boolean
  setIncludeRevoked: (value: boolean) => void
  refresh: () => void
  create: (input: CreateTokenInput) => Promise<ApiTokenCreated>
  revoke: (tokenId: string) => Promise<void>
  isRevoking: boolean
  revokeFailed: boolean
}

/**
 * De quién es el token.
 *
 * Los valores van en minúscula porque son los que viajan por la API y los que lee el enum de
 * PostgreSQL. El panel podría haberlos escrito en mayúsculas por estilo, y entonces el token
 * creado sería rechazado con un `422` que no menciona el problema.
 */
export type ApiTokenType = 'personal' | 'service_key'

export interface CreateTokenInput {
  name: string
  scopes: string[]
  /** `0` significa sin caducidad; el backend lo traduce a `expires_at = null`. */
  expiresInDays: number
  tokenType: ApiTokenType
}

/**
 * Opciones de vigencia.
 *
 * ## Por qué "sin caducidad" ahora sí existe, y antes no
 *
 * Porque antes el backend la rechazaba: `expires_in_days` tenía `ge=1`, así que la opción
 * habría dado un `422` al guardar, y el panel no iba a ofrecer algo que no puede cumplir. Ese
 * era el motivo, y era correcto.
 *
 * Ahora el esquema acepta `None` y traduce el `0` a `expires_at = null`. Se ofrece con un
 * aviso al lado, no en silencio, porque una credencial permanente sobrevive a quien la creó y
 * aviso al lado, no en silencio, porque una credencial permanente sobrevive a quien la
 * creo y el unico mecanismo para desactivarla es la revocacion: es una decision
 * legitima para una integracion y una mala idea para un token personal, y por eso el
 * aviso nombra esa diferencia.
 *
 * ## Por qué el valor es `0` y no una cadena vacía
 *
 * Porque un `<select>` no puede tener `value={null}` sin que el navegador seleccione la
 * primera opción. `0` es la forma de que el selector funcione, y el backend lo traduce. Es el
 * mismo camino que el caso real de un HTML, y por eso su esquema tiene ese validador.
 */
export const EXPIRY_CHOICES = [
  { days: 30, key: 'days30' },
  { days: 90, key: 'days90' },
  { days: 365, key: 'year' },
  { days: 0, key: 'never' },
] as const

/** Vigencia por defecto: 90 días cubren un trimestre de integración. */
export const DEFAULT_EXPIRY_DAYS = 90

/**
 * Los dos tipos de token, en el orden en que se pintan.
 *
 * `personal` primero porque es el que se quiere en la mayoría de los casos: se revoca con
 * criterio al darle de baja a la persona. `service_key` es la excepción deliberada, para
 * integraciones que deben sobrevivir a la marcha de quien las creó.
 */
export const TOKEN_TYPE_CHOICES = [
  { value: 'personal', key: 'personal' },
  { value: 'service_key', key: 'serviceKey' },
] as const satisfies readonly { value: ApiTokenType; key: string }[]

const EMPTY_CATALOG: ApiScopeCatalog = { groups: [], total: 0 }

interface ApiAccessResult {
  key: string
  catalog: ApiScopeCatalog
  tokens: ApiToken[]
  failed: boolean
}

export function useApiAccess(): ApiAccessState {
  const { token } = useAuth()
  const [includeRevoked, setIncludeRevoked] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
  const [isRevoking, setIsRevoking] = useState(false)
  const [revokeFailed, setRevokeFailed] = useState(false)
  const [result, setResult] = useState<ApiAccessResult>({
    key: '',
    catalog: EMPTY_CATALOG,
    tokens: [],
    failed: false,
  })

  // `key` es la identidad de la petición en curso. `isCurrent` compara la respuesta
  // guardada con la petición que se está pidiendo ahora, y de ahí sale el estado de
  // carga **durante el render**, no escribiendo en el efecto.
  //
  // Escribir `setIsLoading(true)` al principio del efecto, que es lo obvio, provoca un
  // render en cascada: el efecto pinta, el estado cambia, React vuelve a pintar, y con
  // `includeRevoked` alternando eso se ve un parpadeo en cada cambio de filtro. El
  // catálogo de la vista de CVE ya usa esta forma y por eso la comparten.
  const requestKey = JSON.stringify([token, includeRevoked, reloadToken])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token) {
      return
    }
    let isActive = true

    void Promise.all([getApiScopes(token), listApiTokens(token, includeRevoked)])
      .then(([scopes, page]) => {
        if (!isActive) {
          return
        }
        setResult({ key: requestKey, catalog: scopes, tokens: page.items, failed: false })
      })
      .catch(() => {
        if (!isActive) {
          return
        }
        // Un fallo deja la pantalla vacía con su mensaje, y no con un estado a medias:
        // mezclar un catálogo vacío con una lista vacía hace que el usuario piense que
        // no tiene tokens cuando en realidad no se pudo preguntar.
        setResult({ key: requestKey, catalog: EMPTY_CATALOG, tokens: [], failed: true })
      })

    return () => {
      isActive = false
    }
  }, [includeRevoked, reloadToken, requestKey, token])

  const refresh = useCallback(() => {
    setReloadToken((current) => current + 1)
  }, [])

  const create = useCallback(
    async (input: CreateTokenInput): Promise<ApiTokenCreated> => {
      if (!token) {
        throw new ApiError(401)
      }
      const created = await createApiToken(token, {
        name: input.name,
        scopes: input.scopes,
        expires_in_days: input.expiresInDays,
        // Viaja en la peticion y no se deduce el backend: un token de servicio con un solo
        // scope de lectura sigue siendo de servicio, y deducirlo de los scopes daria personal.
        token_type: input.tokenType,
      })
      // La tabla se actualiza con la fila recién creada en vez de recargando todo. Un
      // refetch volvería a pedir el catálogo de 46 permisos que no ha cambiado, y
      // provocaría un parpadeo del selector mientras el usuario está leyendo el secreto.
      setResult((current) => ({ ...current, tokens: [created, ...current.tokens] }))
      return created
    },
    [token],
  )

  const revoke = useCallback(
    async (tokenId: string): Promise<void> => {
      if (!token) {
        throw new ApiError(401)
      }
      setIsRevoking(true)
      setRevokeFailed(false)
      try {
        const revoked = await revokeApiToken(token, tokenId)
        setResult((current) => ({
          ...current,
          // Al revocar desaparece del listado por defecto, así que se quita de la vista sin
          // refetch. La fila existe con `revoked_at` y sigue disponible con el interruptor.
          tokens: current.tokens
            .map((item) => (item.id === revoked.id ? revoked : item))
            .filter((item) => includeRevoked || item.revoked_at === null),
        }))
      } catch {
        setRevokeFailed(true)
        throw new ApiError(500)
      } finally {
        setIsRevoking(false)
      }
    },
    [includeRevoked, token],
  )

  return {
    catalog: isCurrent ? result.catalog : EMPTY_CATALOG,
    tokens: isCurrent ? result.tokens : [],
    isLoading: Boolean(token) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    includeRevoked,
    setIncludeRevoked,
    refresh,
    create,
    revoke,
    isRevoking,
    revokeFailed,
  }
}
