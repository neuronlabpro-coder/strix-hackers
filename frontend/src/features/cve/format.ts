import { ApiError } from '../../lib/api'

/**
 * El backend devuelve el 404 de un identificador mal formado en lugar del 422 que
 * daría una petición inválida, porque desde la interfaz un `CVE-abc` es un enlace
 * roto y no una llamada mal construida. El modal distingue ambos: un 404 es un
 * resultado honesto de la búsqueda, cualquier otro fallo es un problema de red.
 */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

/** EPSS llega como `0.42000`; en porcentaje es como se lee una probabilidad. */
export function formatProbability(value: string | null): string | null {
  if (value === null) {
    return null
  }
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return value
  }
  return `${(parsed * 100).toFixed(2)}%`
}

export function formatDate(isoDate: string): string {
  const parsed = new Date(isoDate)
  if (Number.isNaN(parsed.getTime())) {
    return isoDate
  }
  return parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}
