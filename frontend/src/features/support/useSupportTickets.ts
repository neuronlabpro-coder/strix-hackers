import { useCallback, useMemo, useState } from 'react'

import { getMyTickets, getSupportSummary } from '../../lib/supportApi'
import type { SupportSummary, TicketPage, TicketStatus } from '../../types/support'
import { useAuth } from '../auth/useAuth'
import { useAsyncResource, type RequestKey } from '../shared/useAsyncResource'

/**
 * Filas por página en la vista de tickets del cliente.
 *
 * ## Por qué la vista **sí** pagina, cuando antes no lo hacía
 *
 * Porque el endpoint ya aceptaba `limit` y `offset` y la vista no los mandaba: el backend
 * aplicaba su tope por defecto de veinticinco y el resto de los tickets del workspace no
 * existían para nadie, sin barra y sin aviso. El comentario que había en la página decía que
 * paginar «obligaría a un cliente a buscar su propio ticket en páginas», y la conclusión era
 * correcta solo mientras la lista no llegara a veinticinco. Con los filtros encima —que es lo
 * que hace falta para no perder un ticket antiguo entre veinte recientes— la paginación deja de
 * ser un castigo: es lo que hace que el buscador devuelva exactamente lo que se ha pedido.
 */
export const PAGE_SIZE = 25

/** Los cuatro estados del ticket, en el orden en que los ofrece el desplegable. */
export const TICKET_STATUSES: readonly TicketStatus[] = [
  'OPEN',
  'IN_PROGRESS',
  'RESOLVED',
  'CLOSED',
]

/**
 * Filtros de la lista de tickets, en el estado en que los pinta la pantalla.
 *
 * ## Por qué las fechas son cadenas `AAAA-MM-DD` y no `Date`
 *
 * Porque es lo que da el `<input type="date">` y lo que viaja en la query. Convertirlas a `Date`
 * en el cliente obligaría a decidir una zona horaria para un filtro por día natural, y esa
 * decisión no está en ningún sitio del proyecto. El backend corta el rango en UTC sobre la
 * fecha de **alta** —no sobre la de actualización que muestra la tabla— porque `updated_at` se
 * mueve con cada mensaje y el mismo rango daría resultados distintos cada diez minutos.
 */
export interface TicketsQuery {
  status: TicketStatus | null
  search: string
  createdFrom: string
  createdTo: string
}

export const EMPTY_QUERY: TicketsQuery = {
  status: null,
  search: '',
  createdFrom: '',
  createdTo: '',
}

export interface SupportTicketsState {
  page: TicketPage | null
  summary: SupportSummary | null
  isLoading: boolean
  loadFailed: boolean
  query: TicketsQuery
  setQuery: (query: TicketsQuery) => void
  setPage: (offset: number) => void
  reload: () => void
}

/** Lo que viaja dentro de la clave de la lista. El orden no importa; la forma, sí. */
type ClaveLista = [token: string, organizacion: string, offset: number, filtro: TicketsQuery, recarga: number]

/** Lo que viaja dentro de la clave del resumen, que no depende de los filtros. */
type ClaveResumen = [token: string, organizacion: string, recarga: number]

/**
 * Tickets del cliente: filtros, paginación y el resumen de las tarjetas.
 *
 * ## Por qué el resumen va en una petición aparte
 *
 * Porque las tres tarjetas cuentan **todos** los tickets del workspace y no dependen del filtro.
 * Si compartieran efecto con la tabla, cambiar de página o de estado las volvería a pedir, y un
 * `429` de la cabecera —que es solo un extra— dejaría la pantalla sin números justo en el momento
 * de paginar. Con su propia clave se quedan clavadas mientras la tabla cambia, que es lo que
 * dicen las etiquetas: los recuentos del workspace, no los del filtro.
 *
 * ## Por qué el estado de carga se usa **a secas**
 *
 * Porque `useAsyncResource` conserva el `data` anterior hasta que la petición nueva resuelve.
 * Con la forma `isLoading && page === null` —la que se corrigió en `AdminOperationsPage`— al
 * cambiar de filtro se pintarían las filas del filtro anterior bajo el título del nuevo, y eso
 * se lee como «el buscador no filtra». La pantalla usa `isLoading` a secas.
 *
 * ## Por qué la clave es un `JSON.stringify` y no el `offset` solo
 *
 * Porque la clave tiene que cambiar **exactamente** cuando el resultado cambia. Con solo el
 * `offset`, cambiar el buscador no pediría nada; con solo el `organizationId` —que fue lo que
 * había antes— cambiar de workspace dejaría los tickets del anterior en pantalla hasta que
 * llegara la nueva respuesta, que es la fuga entre tenants que R3 prohíbe. Con todo dentro,
 * cada cosa que cambia el resultado cambia la clave, y el token también.
 *
 * ## Por qué los `fetcher` leen la clave y no el cierre
 *
 * Porque si leyeran el `offset` del cierre, bastaría con que la clave cambiara por otra cosa
 * para que pidieran una página distinta de la que la clave dice. Sus dependencias van vacías a
 * propósito: la clave es lo que manda, y el efecto de `useAsyncResource` depende de ella.
 */
export function useSupportTickets(): SupportTicketsState {
  const { token, selectedOrganizationId } = useAuth()
  const [query, setQueryState] = useState<TicketsQuery>(EMPTY_QUERY)
  const [offset, setOffset] = useState(0)
  const [reloadToken, setReloadToken] = useState(0)

  const sesionValida = token !== null && selectedOrganizationId !== null
  const claveLista: RequestKey | null = sesionValida
    ? JSON.stringify([token, selectedOrganizationId, offset, query, reloadToken])
    : null
  const claveResumen: RequestKey | null = sesionValida
    ? JSON.stringify([token, selectedOrganizationId, reloadToken])
    : null

  const fetcherLista = useCallback(
    async (clave: RequestKey): Promise<TicketPage> => {
      const [claveToken, organizacion, pagina, filtro] = JSON.parse(clave) as ClaveLista
      return getMyTickets(claveToken, organizacion, {
        limit: PAGE_SIZE,
        offset: pagina,
        ...(filtro.status ? { status: filtro.status } : {}),
        ...(filtro.search.trim() ? { query: filtro.search.trim() } : {}),
        ...(filtro.createdFrom ? { created_from: filtro.createdFrom } : {}),
        ...(filtro.createdTo ? { created_to: filtro.createdTo } : {}),
      })
    },
    [],
  )

  const fetcherResumen = useCallback(
    async (clave: RequestKey): Promise<SupportSummary> => {
      const [claveToken, organizacion] = JSON.parse(clave) as ClaveResumen
      return getSupportSummary(claveToken, organizacion)
    },
    [],
  )

  const lista = useAsyncResource<TicketPage>(fetcherLista, claveLista)
  const resumen = useAsyncResource<SupportSummary>(fetcherResumen, claveResumen)

  const reload = useCallback(() => {
    setReloadToken((actual) => actual + 1)
  }, [])

  return useMemo(
    () => ({
      page: lista.data,
      summary: resumen.data,
      isLoading: lista.isLoading,
      loadFailed: lista.loadFailed,
      query,
      // ## Por qué cambiar un filtro vuelve a la primera página
      //
      // Porque la página 3 del filtro anterior no significa nada en el nuevo. Sin este
      // `setOffset(0)`, escribir tres letras en el buscador deja la tabla vacía —el `offset` 50
      // está más allá del total del resultado nuevo— y el usuario ve «ningún resultado» con un
      // filtro que sí tiene resultados.
      setQuery: (next: TicketsQuery) => {
        setQueryState(next)
        setOffset(0)
      },
      setPage: (nextOffset: number) => setOffset(Math.max(0, nextOffset)),
      reload,
    }),
    [lista.data, lista.isLoading, lista.loadFailed, query, reload, resumen.data],
  )
}