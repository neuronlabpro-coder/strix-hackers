import { useCallback, useEffect, useMemo, useState } from 'react'

/**
 * Clave que identifica a una petición concreta.
 *
 * No es un adorno: es lo que permite **derivar** el estado de carga en vez de marcarlo. Con
 * una clave guardada junto a los datos, la pregunta que decide si hay que pintar un spinner
 * es "la respuesta que tengo, ¿es la que pedí?", y esa se responde durante el render sin
 * escribir nada.
 */
export type RequestKey = string

export interface AsyncResource<T> {
  /** Los datos de la petición cuya clave es `key`. `null` si aún no hay ninguno válido. */
  data: T | null
  /** `true` mientras los datos que hay no son los de la petición en curso. */
  isLoading: boolean
  loadFailed: boolean
  /** Vuelve a pedir. Comparte clave con la petición actual, así que no reinicia el estado. */
  reload: () => void
}

/**
 * Carga asíncrona con la clave de la petición, sin marcar el estado a mano.
 *
 * ## Por qué este hook y no `useState` + `useEffect`
 *
 * Es el mismo patrón que ya usan `useAdminPage` y `useIssues` en este proyecto, y está
 * prohibido por el lint (`react/set-state-in-effect`) por una razón concreta: escribir
 * estado de forma síncrona dentro de un efecto arranca un render más que normalmente no
 * hace falta, y con la limpieza del efecto se puede dejar la vista colgada en «cargando»
 * para siempre cuando la petición se resuelve después de que el efecto se haya limpiado.
 *
 * Con este hook no hay nada que sincronizar. El efecto solo escribe estado **al
 * resolverse** la promesa, y todo lo demás se deriva:
 *
 * ```
 * isLoading = key !== null && keyDeLosDatos !== key
 * ```
 *
 * ## Por qué se guarda también el error
 *
 * Para no perder los datos anteriores cuando un refresco falla. Vaciarlos convertiría un
 * corte de red en «esta lista no tiene nada», que es un mensaje falso: el usuario borraría
 * su lectura anterior por un problema de conexión. Con `data` intacto y `loadFailed` a
 * `true`, los dos hechos conviven en pantalla.
 *
 * ## Por qué el efecto ignora las respuestas obsoletas
 *
 * Con el flag `vigente`. Sin él, cambiar de ticket antes de que resuelva el anterior deja
 * el detalle del ticket viejo montado sobre el nuevo, y el usuario lee una conversación
 * que no es la que ha abierto.
 */
export function useAsyncResource<T>(
  fetcher: (key: RequestKey) => Promise<T>,
  key: RequestKey | null,
): AsyncResource<T> {
  const [data, setData] = useState<T | null>(null)
  const [keyDeLosDatos, setKeyDeLosDatos] = useState<RequestKey | null>(null)
  const [keyQueFallo, setKeyQueFallo] = useState<RequestKey | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (key === null) {
      return
    }
    let vigente = true
    // Ninguna escritura sincrona aqui. El error se **deriva** abajo, comparando claves, y
    // los datos se escriben al resolverse la promesa, que es el unico momento en el que
    // hay algo que contar.
    fetcher(key)
      .then((resultado) => {
        if (!vigente) return
        setData(resultado)
        setKeyDeLosDatos(key)
        setKeyQueFallo(null)
      })
      .catch(() => {
        if (!vigente) return
        setKeyQueFallo(key)
      })
    return () => {
      vigente = false
    }
  }, [fetcher, key, tick])

  const reload = useCallback(() => {
    setTick((actual) => actual + 1)
  }, [])

  return useMemo(
    () => ({
      data,
      isLoading: key !== null && keyDeLosDatos !== key,
      // El fallo es de **esta** peticion, no de la anterior. Con un booleano suelto, un
      // cambio de filtro dejaba el error de la vista previa visible hasta que la nueva
      // respuesta llegaba, y el usuario leia «no se pudo cargar» sobre una lista que si se
      // estaba cargando.
      loadFailed: key !== null && keyQueFallo === key,
      reload,
    }),
    [data, key, keyDeLosDatos, keyQueFallo, reload],
  )
}
