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

export interface CreateTokenInput {
  name: string
  scopes: string[]
  expiresInDays: number
}

/**
 * Vigencia sin caducidad.
 *
 * El backend impone un techo de 365 días, así que el valor máximo que se puede enviar es
 * ese. "Sin expiración" **no** existe como opción y no se implementa aquí a propósito:
 * una credencial permanente sobrevive a quien la creó, y el mecanismo de revocación es la
 * red de seguridad, no el plan. Ofrecerla y que el servidor la rechace con un `422` sería
 * hacer que el panel prometa algo que no puede cumplir.
 */
export const EXPIRY_CHOICES = [
  { days: 30, key: 'days30' },
  { days: 90, key: 'days90' },
  { days: 365, key: 'year' },
] as const

/** Vigencia por defecto: 90 días cubren un trimestre de integración. */
export const DEFAULT_EXPIRY_DAYS = 90

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
