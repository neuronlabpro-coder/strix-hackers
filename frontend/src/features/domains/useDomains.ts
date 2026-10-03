import { useCallback, useMemo, useState } from 'react'

import { listDomains } from '../../lib/assetsApi'
import type { DomainListResponse, DomainVerificationFilter } from '../../types/assets'
import { useAuth } from '../auth/useAuth'
import { useAsyncResource, type RequestKey } from '../shared/useAsyncResource'

/**
 * Filas por página en el listado de dominios.
 *
 * ## Por qué 25 y no el 50 que usa el inventario de activos
 *
 * Porque 25 es el tamaño que ya usan las otras tablas del panel —tickets y revisiones de PR— y
 * tener un único número hace que la barra de paginación se parezca en todas. El nombre de
 * dominio es la fila más ancha de la pantalla porque lleva el registro TXT completo en la
 * columna de al lado, así que un página más ancha no aporta filas: aporta scroll.
 *
 * ## Por qué se exporta
 *
 * Porque para revisar la barra de paginación en una captura hay que bajarla a un valor bajo —
 * con menos filas que `PAGE_SIZE` el componente `Pagination` devuelve `null` y no hay nada que
 * mirar—, y ese ajuste tiene que ser un número en un sitio, no una constante repetida.
 */
export const PAGE_SIZE = 25

/** Los dos estados por los que se puede filtrar, en el orden en que los ofrece el desplegable. */
export const VERIFICATION_FILTERS: readonly DomainVerificationFilter[] = ['VERIFIED', 'PENDING']

/**
 * Filtros del listado, en el estado en que los pinta la pantalla.
 *
 * ## Por qué las fechas son cadenas `AAAA-MM-DD` y no `Date`
 *
 * Porque es lo que da el `<input type="date">` y lo que viaja en la query. Convertirlas a `Date`
 * en el cliente obligaría a decidir una zona horaria para un filtro por día natural, y esa
 * decisión no está en ningún sitio del proyecto. El backend corta el rango en UTC y ya se ha
 * explicado por qué allí.
 */
export interface DomainsQuery {
  status: DomainVerificationFilter | null
  search: string
  createdFrom: string
  createdTo: string
}

export const EMPTY_QUERY: DomainsQuery = {
  status: null,
  search: '',
  createdFrom: '',
  createdTo: '',
}

export interface DomainsState {
  page: DomainListResponse | null
  isLoading: boolean
  loadFailed: boolean
  query: DomainsQuery
  setQuery: (query: DomainsQuery) => void
  setPage: (offset: number) => void
  reload: () => void
}

/** Lo que viaja dentro de la clave. El orden no importa; la forma, sí. */
type ClaveListado = [
  token: string,
  organizacion: string,
  offset: number,
  filtro: DomainsQuery,
  recarga: number,
]

/**
 * Listado de dominios del workspace: filtros, paginación y recarga.
 *
 * ## Por qué el estado de carga se usa **a secas**
 *
 * Porque `useAsyncResource` conserva el `data` anterior hasta que la petición nueva resuelve.
 * Con la forma `isLoading && page === null` —que es la que se corrigió en
 * `AdminOperationsPage`— al cambiar de filtro se pintarían las filas del filtro anterior bajo
 * el título del nuevo, y eso se lee como «el buscador no filtra». La forma correcta es
 * `isLoading` a secas, y es la que usa la pantalla.
 *
 * ## Por qué la clave es un `JSON.stringify` y no el `offset` solo
 *
 * Porque la clave tiene que cambiar **exactamente** cuando el resultado cambia, ni antes ni
 * después. Si solo fuera el `offset`, cambiar el buscador no pediría nada nuevo y la tabla se
 * quedaría con las filas del filtro anterior para siempre. Y si fuera el `organizationId` —que
 * fue lo que había antes—, cambiar de workspace dejaría los dominios del anterior en pantalla
 * hasta que llegara la nueva respuesta, que es la fuga entre tenants que R3 prohíbe.
 *
 * Con los dos dentro de la clave, cada cosa que cambia el resultado cambia la clave, y el token
 * también: cerrar sesión y volver a entrar con otro es un cambio de datos aunque el workspace y
 * los filtros sean los mismos.
 */
export function useDomains(): DomainsState {
  const { token, selectedOrganizationId } = useAuth()
  const [query, setQueryState] = useState<DomainsQuery>(EMPTY_QUERY)
  const [offset, setOffset] = useState(0)
  const [reloadToken, setReloadToken] = useState(0)

  const key: RequestKey | null =
    token === null || selectedOrganizationId === null
      ? null
      : JSON.stringify([token, selectedOrganizationId, offset, query, reloadToken])

  // El `fetcher` **lee todo de la clave**, incluido el token, el workspace y el `offset`, y por
  // eso sus dependencias van vacías a propósito. Si leyera el `offset` del cierre, bastaría con
  // que la clave cambiara por otra cosa para que pidiera una página distinta de la que dice la
  // clave. La clave es lo que manda, y el efecto de `useAsyncResource` depende de ella.
  const fetcher = useCallback(
    async (clave: RequestKey): Promise<DomainListResponse> => {
      const [claveToken, organizacion, pagina, filtro] = JSON.parse(clave) as ClaveListado
      return listDomains(claveToken, organizacion, {
        limit: PAGE_SIZE,
        offset: pagina,
        ...(filtro.status ? { status: filtro.status } : {}),
        ...(filtro.search.trim() ? { search: filtro.search.trim() } : {}),
        ...(filtro.createdFrom ? { created_from: filtro.createdFrom } : {}),
        ...(filtro.createdTo ? { created_to: filtro.createdTo } : {}),
      })
    },
    [],
  )

  const recurso = useAsyncResource<DomainListResponse>(fetcher, key)

  const reload = useCallback(() => {
    setReloadToken((actual) => actual + 1)
  }, [])

  return useMemo(
    () => ({
      page: recurso.data,
      isLoading: recurso.isLoading,
      loadFailed: recurso.loadFailed,
      query,
      // ## Por qué cambiar un filtro vuelve a la primera página
      //
      // Porque la página 3 del filtro anterior no significa nada en el nuevo. Sin este
      // `setOffset(0)`, escribir tres letras en el buscador deja la tabla vacía —el `offset` 50
      // está más allá del total del resultado nuevo— y el usuario ve «ningún resultado» con un
      // filtro que sí tiene resultados.
      setQuery: (next: DomainsQuery) => {
        setQueryState(next)
        setOffset(0)
      },
      setPage: (nextOffset: number) => setOffset(Math.max(0, nextOffset)),
      reload,
    }),
    [query, reload, recurso.data, recurso.isLoading, recurso.loadFailed],
  )
}