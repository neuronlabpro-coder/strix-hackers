import { useCallback, useEffect, useState } from 'react'

import { analyzePrReview, getPRReviewMetrics, getPRReviews } from '../../lib/api'
import type { PRReviewMetrics, PRReviewPage, PRReviewStatus } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const PAGE_SIZE = 25

/**
 * Estados que la tabla muestra, translated.
 *
 * El dominio guarda `QUEUED | SCANNING | PASSED | FAILED | ERROR` porque esos estados
 * son los que produce el worker y los que escriben los webhooks; renombrarlos a
 * `APPROVED | BLOCKED | PENDING` en la base significaría reinterpretar el historial ya
 * registrado. El cambio de vocabulario es de presentación, y por eso vive aquí y no
 * en el backend: la columna dice lo que le importa a quien lee la tabla, que es si el
 * merge está bloqueado.
 */
type RowStatus = 'APPROVED' | 'BLOCKED' | 'PENDING' | 'ERROR'

function rowStatus(review: {
  status: PRReviewStatus
  merge_blocked: boolean
}): RowStatus {
  // El orden importa: `merge_blocked` gana sobre el estado del escaneo. Un escaneo que
  // terminó en `PASSED` con un hallazgo de alta severidad sigue bloqueando el merge, y
  // un `FAILED` sin hallazgos relevantes no debería hacerlo. Lo que impide el merge es
  // la bandera, no el estado.
  if (review.merge_blocked) {
    return 'BLOCKED'
  }
  if (review.status === 'ERROR') {
    return 'ERROR'
  }
  if (review.status === 'PASSED') {
    return 'APPROVED'
  }
  if (review.status === 'FAILED') {
    return 'BLOCKED'
  }
  return 'PENDING'
}

const STATUSES: PRReviewStatus[] = ['PASSED', 'FAILED', 'SCANNING', 'QUEUED', 'ERROR']

/**
 * Filtros del historial, en el estado en que los pinta la pantalla.
 *
 * Las fechas son cadenas `AAAA-MM-DD` y no `Date`: es lo que da el `<input type="date">`
 * y lo que viaja en la query. Convertirlas a `Date` en el cliente obligaría a decidir una
 * zona horaria para un filtro por día natural, y esa decisión no está en ningún sitio del
 * proyecto. El backend corta el rango en UTC y ya se ha explicado por qué allí.
 */
export interface PrReviewsQuery {
  status: PRReviewStatus | null
  search: string
  createdFrom: string
  createdTo: string
}

export const EMPTY_QUERY: PrReviewsQuery = {
  status: null,
  search: '',
  createdFrom: '',
  createdTo: '',
}

/**
 * Por qué un escaneo de revisión **no** se puede pedir dos veces.
 *
 * Y por eso esto es un conjunto, y no una lista: el botón de la fila tiene que saber si puede
 * aparecer sin preguntar al backend. `QUEUED` y `SCANNING` quedan fuera porque relanzar algo que
 * ya está corriendo produce dos contenedores para el mismo commit, y dos escaneos del mismo
 * diff son la misma seguridad cobrada dos veces —con el mismo consumo de tokens—.
 *
 * `PASSED` y `FAILED` sí están: un PR al que se le añaden commits genera una revisión **nueva**,
 * así que relanzar una `PASSED` no es una forma de trayarse commits nuevos. Y un `FAILED` de
 *，原因 que nadie ve es justo el caso que este botón resuelve.
 */
export const ESTADOS_RELANZABLES = new Set<PRReviewStatus>(['PASSED', 'FAILED', 'ERROR'])

export type Lanzamiento =
  | { estado: 'inactivo' }
  | { estado: 'en_curso'; reviewId: string }
  | { estado: 'fallido'; reviewId: string; reintentable: boolean }

export interface PrReviewsState {
  page: PRReviewPage | null
  metrics: PRReviewMetrics | null
  isLoading: boolean
  loadFailed: boolean
  query: PrReviewsQuery
  setQuery: (query: PrReviewsQuery) => void
  setPage: (offset: number) => void
  refresh: () => void
  lanzamiento: Lanzamiento
  lanzarAnalisis: (reviewId: string) => void
  descartarAviso: () => void
}

/**
 * Historial de revisiones de pull request: filtros, paginación y KPI de cabecera.
 *
 * ## Por qué el estado de carga se **deriva** de una clave y no se marca a mano
 *
 * Porque la respuesta anterior se conserva mientras llega la nueva, y el error que se
 * pinta tiene que ser el de **esta** petición. Se guarda la clave de la petición junto a
 * sus datos: mientras la clave pedida no sea la clave de los datos que hay, la vista está
 * cargando, y eso se responde durante el render sin escribir nada. La pantalla usa
 * `isLoading` a secas —no `isLoading && page === null`— porque con la segunda forma, al
 * cambiar de filtro se pintarían las filas del filtro anterior bajo el título del nuevo.
 * Ese error ya se corrigió en `AdminOperationsPage` y aquí se evita por construcción.
 *
 * ## Por qué los KPI van en un efecto aparte
 *
 * Porque describen la actividad **total** del tenant y no dependen del filtro. Si compartieran
 * efecto con la tabla, cambiar de página o de estado los volvería a pedir, y un `429` de la
 * cabecera —que es solo un extra— dejaría la pantalla medio vacía en el momento de paginar.
 * Con su propio efecto los números se quedan clavados mientras la tabla cambia, que es lo que
 * dice el comentario de la tabla: los KPI del tenant, no los del filtro.
 */
export function usePrReviews(): PrReviewsState {
  const { token, selectedOrganizationId } = useAuth()
  const [query, setQueryState] = useState<PrReviewsQuery>(EMPTY_QUERY)
  const [offset, setOffset] = useState(0)
  const [reloadToken, setReloadToken] = useState(0)
  const [result, setResult] = useState<{
    key: string
    page: PRReviewPage | null
    failed: boolean
  }>({ key: '', page: null, failed: false })
  const [metrics, setMetrics] = useState<PRReviewMetrics | null>(null)
  const [lanzamiento, setLanzamiento] = useState<Lanzamiento>({ estado: 'inactivo' })

  const requestKey = JSON.stringify([
    token,
    selectedOrganizationId,
    offset,
    query,
    reloadToken,
  ])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getPRReviews(token, selectedOrganizationId, {
      limit: PAGE_SIZE,
      offset,
      ...(query.status ? { status: query.status } : {}),
      ...(query.search.trim() ? { query: query.search.trim() } : {}),
      ...(query.createdFrom ? { createdFrom: query.createdFrom } : {}),
      ...(query.createdTo ? { createdTo: query.createdTo } : {}),
    })
      .then((page) => {
        if (!isActive) {
          return
        }
        setResult({ key: requestKey, page, failed: false })
      })
      .catch(() => {
        // Se conserva la página anterior: un corte de red no es «esta lista no tiene nada».
        if (isActive) {
          setResult((current) => ({ ...current, key: requestKey, failed: true }))
        }
      })
    return () => {
      isActive = false
    }
  }, [offset, query, reloadToken, requestKey, selectedOrganizationId, token])

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getPRReviewMetrics(token, selectedOrganizationId)
      .then((value) => {
        if (isActive) {
          setMetrics(value)
        }
      })
      .catch(() => {
        // Los KPI son un extra de la cabecera. Si fallan, la tabla sigue siendo útil y no
        // merece la pena tapar la pantalla con un error por cuatro números.
        if (isActive) {
          setMetrics(null)
        }
      })
    return () => {
      isActive = false
    }
  }, [reloadToken, selectedOrganizationId, token])

  /**
   * Lanza el análisis de una revisión y recarga la tabla.
   *
   * ## Por qué recarga en vez de parchear la fila
   *
   * Porque el backend responde `202` con la revisión ya en `QUEUED`, y la fila que se pinta
   * después **no** es la que devuelve el `POST`: el `run` se crea después, en el worker, y el
   * pipeline va a cambiar el estado varias veces más. Recargar cuesta una petición y da la
   * fila que el servidor tiene de verdad, no una copia que se quedó vieja en cuanto el worker
   * empezó. Es lo mismo que hace la sincronización de Supply Chain y por el mismo motivo.
   *
   * ## Por qué el error distingue `reintentable`
   *
   * Porque un `503` de cola caída y un `404` de «esta revisión no se puede lanzar ahora» piden
   * cosas opuestas del usuario: el primero, «vuelve a intentarlo en un momento»; el segundo,
   * «no lo intentes otra vez, ya está en curso o el repositorio está desconectado». Un único
   * mensaje de error para los dos dejaría al usuario en la duda justo cuando la duda es lo
   * único que importa.
   *
   * ## Por qué el error se guarda en estado y no se lanza
   *
   * Porque es información sobre el estado de la pantalla, no una excepción. Se muestra como
   * aviso y la tabla se queda como estaba: un error de red que vacía la tabla hace que el
   * usuario piense que sus revisiones han desaparecido, que es peor que no poder lanzarlas.
   */
  const lanzarAnalisis = useCallback(
    (reviewId: string) => {
      if (!token || !selectedOrganizationId) {
        return
      }
      setLanzamiento({ estado: 'en_curso', reviewId })
      void analyzePrReview(token, selectedOrganizationId, reviewId)
        .then(() => {
          setLanzamiento({ estado: 'inactivo' })
          setReloadToken((current) => current + 1)
        })
        .catch((error: unknown) => {
          setLanzamiento({
            estado: 'fallido',
            reviewId,
            // `503` es infraestructura: la cola no pudo atender una petición válida y el
            // backend pone `Retry-After`. Cualquier otro status —un `404` o un `403`— no se
            // arregla reintentando, así que no se ofrece como reintentable.
            reintentable: esErrorDeServidor(error),
          })
        })
    },
    [selectedOrganizationId, token],
  )

  return {
    page: isCurrent ? result.page : null,
    metrics,
    isLoading: Boolean(token && selectedOrganizationId) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    query,
    // ## Por qué cambiar un filtro vuelve a la primera página
    //
    // Porque la página 4 del filtro anterior no significa nada en el nuevo. Sin este
    // `setOffset(0)`, escribir tres letras en el buscador deja la tabla vacía —el
    // `offset` 75 está más allá del total del resultado nuevo— y el usuario ve «ningún
    // resultado» con un filtro que sí tiene resultados.
    setQuery: (next: PrReviewsQuery) => {
      setQueryState(next)
      setOffset(0)
    },
    setPage: (nextOffset: number) => setOffset(Math.max(0, nextOffset)),
    refresh: () => setReloadToken((current) => current + 1),
    lanzamiento,
    lanzarAnalisis,
    descartarAviso: () => setLanzamiento({ estado: 'inactivo' }),
  }
}

/**
 * ¿El fallo fue del servidor y se puede reintentar?
 *
 * ## Por qué se mira el status y no el tipo del error
 *
 * Porque `request` ya envuelve el fallo en un error con el status dentro, y lo que el usuario
 * necesita saber no es «qué clase de excepción es» sino «¿tengo que esperar o tengo que
 * otra cosa». La distinción entre los dos casos es exactamente la del status: `503` es la cola
 * o el proveedor, y `404`/`403` son decisiones del servidor que ya no van a cambiar por
 * repetir la llamada.
 */
function esErrorDeServidor(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'status' in error &&
    (error as { status: unknown }).status === 503
  )
}

export { PAGE_SIZE, STATUSES, rowStatus }
export type { RowStatus }