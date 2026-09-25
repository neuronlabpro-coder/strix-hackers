import { useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { KnowledgeCategory, KnowledgeSeverity } from '../../types/api'
import { KnowledgeDetailModal } from './KnowledgeDetailModal'
import { CATEGORIES, SEVERITIES, useKnowledge } from './useKnowledge'

const SEVERITY_SWATCH: Record<KnowledgeSeverity, string> = {
  CRITICAL: 'severity-swatch-critical',
  HIGH: 'severity-swatch-high',
  MEDIUM: 'severity-swatch-medium',
  LOW: 'severity-swatch-low',
}

export function KnowledgePage() {
  const { t } = useTranslation('knowledge')
  const { page, isLoading, loadFailed, query, setQuery, refresh } = useKnowledge()
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null)
  const hasFilters = Boolean(query.category || query.severity || query.search)

  return (
    <section className="page-section" aria-labelledby="knowledge-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="knowledge-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <button className="secondary-button" type="button" onClick={refresh}>
          <RefreshCw size={16} aria-hidden="true" />
          <span>{t('states.retry')}</span>
        </button>
      </div>

      <div className="filter-bar">
        <label className="search-field">
          <Search size={16} aria-hidden="true" />
          <span className="visually-hidden">{t('searchLabel')}</span>
          <input
            type="search"
            value={query.search}
            placeholder={t('searchPlaceholder')}
            onChange={(event) => setQuery({ ...query, search: event.target.value })}
          />
        </label>
        <div className="filter-field">
          <label htmlFor="knowledge-category">{t('filters.category')}</label>
          <select
            id="knowledge-category"
            value={query.category ?? ''}
            onChange={(event) =>
              setQuery({
                ...query,
                category: (event.target.value || null) as KnowledgeCategory | null,
              })
            }
          >
            <option value="">{t('filters.allCategories')}</option>
            {CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {t(`category.${category}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="knowledge-severity">{t('filters.severity')}</label>
          <select
            id="knowledge-severity"
            value={query.severity ?? ''}
            onChange={(event) =>
              setQuery({
                ...query,
                severity: (event.target.value || null) as KnowledgeSeverity | null,
              })
            }
          >
            <option value="">{t('filters.allSeverities')}</option>
            {SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>
                {severity}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : page && page.items.length === 0 ? (
        <div className="empty-card">
          <h2>{hasFilters ? t('states.noResults') : t('states.empty')}</h2>
          <p>{t('states.emptyDescription')}</p>
        </div>
      ) : page ? (
        <div className="knowledge-grid">
          {page.items.map((entry) => (
            <button
              key={entry.id}
              className="knowledge-card"
              type="button"
              onClick={() => setSelectedEntryId(entry.id)}
            >
              <div className="knowledge-card-head">
                <span className="mono knowledge-code">{entry.reference_code}</span>
                <span
                  className={`severity-swatch ${SEVERITY_SWATCH[entry.severity]}`}
                  aria-hidden="true"
                />
              </div>
              <h2 className="knowledge-card-title">{entry.title}</h2>
              <p className="knowledge-card-risk">{entry.risk_summary}</p>
              <div className="board-card-meta">
                <span className="badge">{t(`category.${entry.category}`)}</span>
                <span className="mono">{entry.owasp_category}</span>
              </div>
            </button>
          ))}
        </div>
      ) : null}

      <KnowledgeDetailModal entryId={selectedEntryId} onClose={() => setSelectedEntryId(null)} />
    </section>
  )
}
