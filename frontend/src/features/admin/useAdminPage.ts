import { useCallback, useEffect, useMemo, useState } from 'react'

/**
 * Envoltorio común de las páginas de la consola de SuperAdmin.
 *
 * ## Qué evita
 *
 * Las cinco secciones son tablas paginadas con filtros. Escribirlas por separado produce
 * cinco copias del mismo esqueleto de estados —cargando, error, sin resultados, página
 * anterior, página siguiente— y las cinco se desincronizan en cuanto una cambia: una tiene
 * la opción de reintentar y otra no, una recuerda el filtro al paginar y otra no.
 *
 * Este hook centraliza ese esqueleto y **no** el `fetch`. Cada página sigue llamando a su
 * propia función de `lib/adminApi`, porque cada sección tiene una consulta distinta y un
 * pegamento genérico sobre `fetch` acabaría haciendo cualquier cosa con el error.
 *
 * ## Por qué los datos se guardan con la clave del sondeo
 *
 * El estado es un único objeto que lleva dentro la `requestKey` que lo produjo, y `items`
 * solo se lee si esa clave coincide con la de la petición en curso. La consecuencia útil es
 * que **nada se escribe de forma síncrona dentro del efecto**: el estado de carga se deriva
 * de "la respuesta que tengo no es la que pedí", que es la pregunta correcta, en vez de
 * marcarse a mano antes de empezar y desmarcarse al terminar. Con la marca manual, un
 * efecto que se limpia antes de resolver deja la vista colgada en «cargando» para siempre.
 *
 * Es el mismo patrón que ya usa `useIssues`, con la misma variable: `key`.
 *
 * ## El fallo de carga no borra los datos anteriores
 *
 * Cuando un refresco falla se marca `failed` pero **no** se vacía la página. Vaciarla
 * convertiría un fallo de red en "la tabla no tiene nada", que es un mensaje falso: el
 * operador borraría su lectura anterior por un problema de conexión.
 */

export interface AdminPageSnapshot<T, TExtra> {
  /** Identifica a qué sondeo pertenecen estos datos. */
  key: string
  items: T[]
  total: number
  /**
   * El resto de la respuesta, sin procesar.
   *
   * Existe para que una vista no recalcule un agregado que la API **ya** devuelve. La
   * tabla de ventas trae `total_credits`; recalcularlo en el cliente crearía una segunda
   * fuente para la misma cifra, y las dos divergirían en la primera diferencia de
   * redondeo sin que nada avise.
   */
  extra: TExtra | null
  failed: boolean
}

export interface AdminPageState<T, TExtra = unknown> {
  items: T[]
  total: number
  extra: TExtra | null
  isLoading: boolean
  loadFailed: boolean
  page: number
  pageSize: number
  pageCount: number
  /** Rango visible, para el resumen "1–25 de 340". Es `null` si no hay nada que mostrar. */
  range: { from: number; to: number } | null
  setPage: (page: number) => void
  refresh: () => void
}

export interface UseAdminPageOptions {
  pageSize?: number
  /** Cambia cuando cambia un filtro: al filtrar se vuelve a la primera página. */
  filterKey: string
  /** `true` cuando falta sesión y no se debe pedir nada. */
  disabled?: boolean
}

export function useAdminPage<T, TExtra = unknown>(
  load: (limit: number, offset: number) => Promise<{ items: T[]; total: number } & TExtra>,
  options: UseAdminPageOptions,
): AdminPageState<T, TExtra> {
  const { pageSize = 25, filterKey, disabled = false } = options
  const [snapshot, setSnapshot] = useState<AdminPageSnapshot<T, TExtra> | null>(null)
  const [page, setPage] = useState(0)
  const [reloadToken, setReloadToken] = useState(0)
  const [appliedFilterKey, setAppliedFilterKey] = useState(filterKey)

  /*
    Cambiar de filtro vuelve a la primera página, y se ajusta **durante el render** en vez
    de en un efecto. Es el patrón que React documenta para "el estado depende de las
    props": el ajuste dentro del render se aplica antes de pintar, de modo que nunca llega
    a verse la página antigua con los datos nuevos.
  */
  if (appliedFilterKey !== filterKey) {
    setAppliedFilterKey(filterKey)
    setPage(0)
  }

  const requestKey = `${page}:${pageSize}:${filterKey}:${reloadToken}`

  useEffect(() => {
    if (disabled) {
      return
    }
    let isActive = true
    void load(pageSize, page * pageSize)
      .then((response) => {
        if (!isActive) {
          return
        }
        const { items, total, ...rest } = response
        setSnapshot({
          key: requestKey,
          items,
          total,
          extra: Object.keys(rest).length > 0 ? (rest as TExtra) : null,
          failed: false,
        })
      })
      .catch(() => {
        if (isActive) {
          // Se conservan los datos anteriores: solo cambia la marca de fallo. Vaciar la
          // página informaría de que no hay nada, que es una conclusión falsa.
          setSnapshot((current) => ({
            key: requestKey,
            items: current?.items ?? [],
            total: current?.total ?? 0,
            extra: (current?.extra ?? null) as TExtra | null,
            failed: true,
          }))
        }
      })
    return () => {
      isActive = false
    }
  }, [disabled, load, page, pageSize, requestKey])

  const isCurrent = snapshot !== null && snapshot.key === requestKey

  const refresh = useCallback(() => {
    setReloadToken((current) => current + 1)
  }, [])

  const items = isCurrent ? snapshot.items : []
  const total = isCurrent ? snapshot.total : 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const range = useMemo(
    () =>
      items.length === 0 ? null : { from: page * pageSize + 1, to: page * pageSize + items.length },
    [items.length, page, pageSize],
  )

  return {
    items,
    total,
    extra: isCurrent ? snapshot.extra : null,
    // "Cargando" es exactamente "no tengo todavía la respuesta que pedí". No es un estado
    // que alguien marque: si el efecto se limpia antes de resolver, esta expresión
    // seguiría diciendo que se está cargando en vez de quedarse colgada.
    isLoading: !disabled && !isCurrent,
    loadFailed: isCurrent && snapshot.failed,
    page,
    pageSize,
    pageCount,
    range,
    setPage,
    refresh,
  }
}
