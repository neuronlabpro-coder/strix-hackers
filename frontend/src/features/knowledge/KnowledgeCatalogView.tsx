import { useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { KnowledgeCategory, KnowledgeSeverity } from '../../types/api'
import { KnowledgeDetailModal } from './KnowledgeDetailModal'
import { CATEGORIES, SEVERITIES, useKnowledge } from './useKnowledge'

/**
 * El catálogo técnico de remediación: la guía de las familias de fallo.
 *
 * ## Por qué vive en su propio componente y no dentro de la página
 *
 * Porque `/knowledge` tiene ahora **dos** vistas con datos de naturaleza distinta: los documentos
 * que escribe el cliente y la guía que escribe el producto. Separarlas en dos componentes hace
 * que ninguna dependa del estado de la otra, y sin eso cualquier recarga de una deja a la otra
 * mostrando el `isLoading` de la que se recargó. La razón de por qué son dos datos distintos está
 * en el encabezado de `KnowledgePage`.
 *
 * Es el contenido que vivía en `KnowledgePage` antes de la Fase 6, sin cambios de
 * comportamiento: los mismos filtros, el mismo buscador y el mismo modal de detalle.
 */

const SEVERITY_SWATCH: Record<KnowledgeSeverity, string> = {
  CRITICAL: 'severity-swatch-critical',
  HIGH: 'severity-swatch-high',
  MEDIUM: 'severity-swatch-medium',
  LOW: 'severity-swatch-low',
}

export function KnowledgeCatalogView() {
  const { t } = useTranslation('knowledge')
  const { page, isLoading, loadFailed, query, setQuery, refresh } = useKnowledge()
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null)
  const hasFilters = Boolean(query.category || query.severity || query.search)

  return (
    <div className="content-card">
      <div className="filter-bar">
        <div className="search-field">
          <Search size={15} aria-hidden="true" />
          <input
            type="search"
            value={query.search}
            placeholder={t('catalog.searchPlaceholder')}
            aria-label={t('catalog.searchPlaceholder')}
            onChange={(event) => setQuery({ ...query, search: event.target.value })}
          />
        </div>

        <div className="filter-group" role="group" aria-label={t('catalog.severityLabel')}>
          {SEVERITIES.map((severity) => (
            <button
              className={
                query.severity === severity ? 'filter-chip filter-chip-active' : 'filter-chip'
              }
              type="button"
              key={severity}
              onClick={() =>
                setQuery({ ...query, severity: query.severity === severity ? null : severity })
              }
            >
              <span className={SEVERITY_SWATCH[severity]} aria-hidden="true" />
              {t(`catalog.severities.${severity}`)}
            </button>
          ))}
        </div>

        <div className="filter-group" role="group" aria-label={t('catalog.categoryLabel')}>
          <button
            className={query.category === null ? 'filter-chip filter-chip-active' : 'filter-chip'}
            type="button"
            onClick={() => setQuery({ ...query, category: null })}
          >
            {t('catalog.allCategories')}
          </button>
          {CATEGORIES.map((category: KnowledgeCategory) => (
            <button
              className={
                query.category === category ? 'filter-chip filter-chip-active' : 'filter-chip'
              }
              type="button"
              key={category}
              onClick={() =>
                setQuery({ ...query, category: query.category === category ? null : category })
              }
            >
              {t(`catalog.categories.${category}`)}
            </button>
          ))}
        </div>

        {hasFilters && (
          <button
            className="ghost-button"
            type="button"
            onClick={() => setQuery({ category: null, severity: null, search: '' })}
          >
            {t('catalog.clearFilters')}
          </button>
        )}

        <button className="secondary-button" type="button" onClick={refresh}>
          <RefreshCw size={15} aria-hidden="true" />
          {t('catalog.refresh')}
        </button>
      </div>

      {loadFailed && (
        <div className="empty-state">
          <p>{t('catalog.loadFailed')}</p>
          <button className="secondary-button" type="button" onClick={refresh}>
            {t('catalog.retry')}
          </button>
        </div>
      )}

      {isLoading && !loadFailed && <p className="table-caption">{t('catalog.loading')}</p>}

      {!isLoading && !loadFailed && page && page.items.length === 0 && (
        <div className="empty-state">
          <p>{hasFilters ? t('catalog.emptyFiltered') : t('catalog.empty')}</p>
        </div>
      )}

      {!isLoading && !loadFailed && page && page.items.length > 0 && (
        <>
          <div className="knowledge-grid">
            {page.items.map((entry) => (
              <button
                className="knowledge-card"
                type="button"
                key={entry.id}
                onClick={() => setSelectedEntryId(entry.id)}
              >
                <span className="knowledge-card-head">
                  <span className="knowledge-card-code">{entry.reference_code}</span>
                  <span
                    className={SEVERITY_SWATCH[entry.severity]}
                    aria-label={t(`catalog.severities.${entry.severity}`)}
                  />
                </span>
                <span className="knowledge-card-title">{entry.title}</span>
                <span className="knowledge-card-summary">{entry.risk_summary}</span>
              </button>
            ))}
          </div>
          <p className="table-caption">
            {t('catalog.count', { shown: page.items.length, total: page.total })}
          </p>
        </>
      )}

      {selectedEntryId !== null && (
        <KnowledgeDetailModal
          entryId={selectedEntryId}
          onClose={() => setSelectedEntryId(null)}
        />
      )}
    </div>
  )
}
