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
/**
 * Los tres estados de una petición, derivado de tres claves y **nada más**.
 *
 * ## Por qué es una función suelta y no está dentro del hook
 *
 * Porque es aritmética pura, y una aritmética pura se puede probar sin montar React. Este
 * proyecto no tiene `@testing-library/react` ni entorno de DOM: sus pruebas son de lógica, y
 * esta es la clase de cosa que se puede comprobar exactamente sin ellos.
 *
 * ## Por qué `isLoading` excluye el fallo de **esta** clave
 *
 * Porque sin esa exclusión `isLoading` y `loadFailed` son las dos ciertas cuando una petición
 * falla: nunca llegó una respuesta, así que `claveDeLosDatos` sigue siendo `null`, y
 * `claveQueFallo` ya es la clave pedida. Y como las pantallas pintan
 * `isLoading ? cargando : loadFailed ? error : datos`, la primera rama se come el error y la
 * pantalla queda en «Cargando…» **para siempre**.
 *
 * No es un caso raro: es exactamente lo que se ve cuando el backend no tiene la ruta, y es el
 * fallo que un operador no puede diagnosticar desde la pantalla porque la pantalla le está
 * diciendo que sigue cargando.
 *
 * ## Por qué se excluye solo la clave que falló
 *
 * Porque al entrar una petición **nueva**, `claveQueFallo !== clave` y `isLoading` vuelve a
 * activarse. Un `||` con «no ha fallado nunca» dejaría la pantalla clavada en el error del
 * filtro anterior mientras la petición nueva está en curso.
 */
export function estadoDeLaPeticion(
  clave: RequestKey | null,
  claveDeLosDatos: RequestKey | null,
  claveQueFallo: RequestKey | null,
): { cargando: boolean; fallo: boolean } {
  if (clave === null) {
    return { cargando: false, fallo: false }
  }
  const fallo = claveQueFallo === clave
  return {
    cargando: claveDeLosDatos !== clave && !fallo,
    fallo,
  }
}

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

  const estado = useMemo(
    () => estadoDeLaPeticion(key, keyDeLosDatos, keyQueFallo),
    [key, keyDeLosDatos, keyQueFallo],
  )

  return useMemo(
    () => ({
      data,
      isLoading: estado.cargando,
      // El fallo es de **esta** peticion, no de la anterior. Con un booleano suelto, un
      // cambio de filtro dejaba el error de la vista previa visible hasta que la nueva
      // respuesta llegaba, y el usuario leia «no se pudo cargar» sobre una lista que si se
      // estaba cargando.
      loadFailed: estado.fallo,
      reload,
    }),
    [data, estado, reload],
  )
}
