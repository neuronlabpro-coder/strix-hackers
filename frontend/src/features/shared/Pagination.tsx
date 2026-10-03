/**
 * Paginación compartida por todas las tablas del panel.
 *
 * ## Por qué no se reutiliza `PaginationBar` de la consola
 *
 * Porque aquel componente recibe `AdminPageState<T>`, que es un tipo de la consola: trae `range` ya
 * calculado y vive en `features/admin/`. Importarlo desde el panel de cliente metería una dependencia
 * de la consola en el panel, que es justo la dirección que rompe el aislamiento de Administrative
 * navigation. Además aquel `range` lo calcula `useAdminPage`, y aquí el estado es otro.
 *
 * Por eso este componente habla el lenguaje mínimo que todos los hooks entienden: `offset`, `limit`
 * y `total`. Los tres vienen en la respuesta de cualquier endpoint paginado del proyecto, así que
 * no hace falta que cada uno adapte nada.
 *
 * ## Por qué los botones se desactivan en vez de desaparecer
 *
 * Porque un botón que desaparece cambia el ancho de la barra entre páginas y empuja el contenido
 * lateralmente. Desactivado mantiene el ancho fijo y comunica lo mismo: no hay más hacia ese lado.
 *
 * ## Por qué el resumen se delega en i18n
 *
 * Porque «1–25 de 340» tiene dos separadores y un glifo cuyo orden cambia según el idioma. Se usa
 * la plantilla, que es donde ya viven las reglas de cada idioma, en vez de componerlo concatenando.
 */
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export interface PaginationProps {
  /** Filas ya cargadas en total. El servidor lo manda en `total`. */
  total: number
  /** Tamaño de página en uso. El servidor lo devuelve en `limit`. */
  limit: number
  /** Índice de la primera fila de la página actual. El servidor lo devuelve en `offset`. */
  offset: number
  /** Se llama con el nuevo `offset`. Quien lo recibe es dueño de su estado. */
  onOffsetChange: (offset: number) => void
  /** Namespace de i18n donde viven `pagination.previous`, `next` y `summary`. */
  namespace?: string
}

export function Pagination({
  total,
  limit,
  offset,
  onOffsetChange,
  namespace = 'common',
}: PaginationProps) {
  const { t } = useTranslation(namespace)

  // Con una sola página no hay nada que paginar: la barra sería ruido. Y `limit <= 0` es
  // imposible con los endpoints del proyecto, pero si algún día alguien devuelve `limit: 0` esta
  // división daría `Infinity` y el bucle de arriba no terminaría nunca.
  if (total <= limit || limit <= 0) {
    return null
  }

  const desde = offset + 1
  const hasta = Math.min(offset + limit, total)
  const hayAnterior = offset > 0
  const haySiguiente = hasta < total

  return (
    <nav className="pagination" aria-label={t('pagination.summary', { from: desde, to: hasta, total })}>
      <button
        className="secondary-button"
        type="button"
        disabled={!hayAnterior}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
      >
        <ChevronLeft size={16} aria-hidden="true" />
        <span>{t('pagination.previous')}</span>
      </button>

      <span className="pagination-summary">{t('pagination.summary', { from: desde, to: hasta, total })}</span>

      <button
        className="secondary-button"
        type="button"
        disabled={!haySiguiente}
        onClick={() => onOffsetChange(offset + limit)}
      >
        <span>{t('pagination.next')}</span>
        <ChevronRight size={16} aria-hidden="true" />
      </button>
    </nav>
  )
}
