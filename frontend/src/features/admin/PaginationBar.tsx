import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { AdminPageState } from './useAdminPage'

/**
 * Paginación compartida por las cinco secciones de la consola.
 *
 * ## Por qué no usa `Intl` para los rangos
 *
 * El resumen «1–25 de 340» tiene dos separadores y un glifo, y su orden cambia según el
 * idioma. Se delega en i18n con la plantilla, que es donde ya viven las reglas de cada
 * idioma, en vez de componerlo a mano con concatenación — que además no distinguiría
 * «1–25» de «1 a 25» y en inglés la abreviatura es distinta.
 *
 * ## Por qué los botones se desactivan en los extremos en vez de desaparecer
 *
 * Un botón que desaparece hace que la barra cambie de ancho entre páginas y empuje el
 * contenido lateralmente. Desactivado mantiene el ancho fijo y comunica lo mismo: no hay
 * más hacia ese lado.
 */
export function PaginationBar<T>({ page }: { page: AdminPageState<T> }) {
  const { t } = useTranslation('admin')

  if (page.range === null) {
    return null
  }

  return (
    <nav className="pagination" aria-label={t('pagination.summary', page.range)}>
      <p className="pagination-summary">
        {t('pagination.summary', {
          from: page.range.from,
          to: page.range.to,
          total: page.total,
        })}
      </p>
      <div className="pagination-actions">
        <button
          className="ghost-button"
          type="button"
          disabled={page.page === 0 || page.isLoading}
          onClick={() => page.setPage(page.page - 1)}
        >
          <ChevronLeft size={15} aria-hidden="true" />
          <span>{t('pagination.previous')}</span>
        </button>
        <button
          className="ghost-button"
          type="button"
          disabled={page.page + 1 >= page.pageCount || page.isLoading}
          onClick={() => page.setPage(page.page + 1)}
        >
          <span>{t('pagination.next')}</span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>
      </div>
    </nav>
  )
}
