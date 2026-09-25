import { useMemo, useState } from 'react'
import { CodeXml, GitBranch, Plus, RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'

import { useDashboardSummary } from '../dashboard/useDashboardSummary'
import { ConnectRepositoryModal } from './ConnectRepositoryModal'
import { RepositoryReviewToggle } from './RepositoryReviewToggle'

function formatDateTime(value: string | null, locale: string, fallback: string): string {
  if (!value) {
    return fallback
  }
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) {
    return fallback
  }
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed)
}

export function RepositoriesPage() {
  const { t } = useTranslation('repositories')
  const { t: tCommon } = useTranslation('common')
  const { i18n } = useTranslation()
  const { summary, isLoading, loadFailed, patchRepositoryFlag, refresh } = useDashboardSummary()
  const [searchParams, setSearchParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [toggleFailed, setToggleFailed] = useState(false)

  const connectedProvider = searchParams.get('connected')
  const connectionDenied = searchParams.get('connection') === 'denied'

  const repositories = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()
    const allRepositories = summary?.repositories ?? []
    if (!normalizedQuery) {
      return allRepositories
    }
    return allRepositories.filter(
      (repository) =>
        repository.full_name.toLowerCase().includes(normalizedQuery) ||
        repository.provider.toLowerCase().includes(normalizedQuery),
    )
  }, [query, summary])

  const dismissConnectionBanner = () => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.delete('connected')
    nextParams.delete('connection')
    setSearchParams(nextParams, { replace: true })
  }

  return (
    <section className="page-section" aria-labelledby="repositories-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="repositories-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <button className="secondary-button" type="button" onClick={refresh}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('refresh')}</span>
          </button>
          <button
            className="primary-button"
            type="button"
            onClick={() => setIsModalOpen(true)}
          >
            <Plus size={17} aria-hidden="true" />
            <span>{t('addRepository')}</span>
          </button>
        </div>
      </div>

      {connectedProvider ? (
        <p
          className={
            connectionDenied ? 'inline-notice inline-notice-warning' : 'inline-notice'
          }
          role="status"
        >
          <span>
            {connectionDenied
              ? t('connected.denied', { provider: connectedProvider })
              : t('connected.success', { provider: connectedProvider })}
          </span>
          <button
            className="icon-button"
            type="button"
            onClick={dismissConnectionBanner}
            aria-label={tCommon('actions.close')}
          >
            ×
          </button>
        </p>
      ) : null}

      <div className="toolbar">
        <label className="search-field">
          <Search size={16} aria-hidden="true" />
          <span className="visually-hidden">{t('searchLabel')}</span>
          <input
            type="search"
            value={query}
            placeholder={t('searchPlaceholder')}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>

      {toggleFailed ? (
        <p className="inline-notice inline-notice-warning" role="alert">
          {t('prReviews.updateFailed')}
        </p>
      ) : null}

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
      ) : repositories.length === 0 ? (
        <div className="empty-card">
          <span className="empty-card-mark" aria-hidden="true">
            <GitBranch size={20} />
          </span>
          <h2>{query ? t('states.noResults') : t('states.empty')}</h2>
          <p>{t('states.emptyDescription')}</p>
          <button className="primary-button" type="button" onClick={() => setIsModalOpen(true)}>
            <Plus size={17} aria-hidden="true" />
            <span>{t('addRepository')}</span>
          </button>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="visually-hidden">{t('title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('columns.provider')}</th>
                <th scope="col">{t('columns.repository')}</th>
                <th scope="col">{t('columns.status')}</th>
                <th scope="col">{t('columns.issues')}</th>
                <th scope="col">{t('columns.prReviews')}</th>
                <th scope="col">{t('columns.supplyChain')}</th>
                <th scope="col">{t('columns.lastTested')}</th>
              </tr>
            </thead>
            <tbody>
              {repositories.map((repository) => (
                <tr key={repository.id}>
                  <td>
                    <span className="provider-cell">
                      <CodeXml size={16} aria-hidden="true" />
                      <span className="mono">{repository.provider}</span>
                    </span>
                  </td>
                  <td>
                    <div className="repository-cell">
                      <strong>{repository.name}</strong>
                      <span className="mono repository-path">{repository.full_name}</span>
                    </div>
                  </td>
                  <td>
                    <span className={`badge badge-${repository.status.toLowerCase()}`}>
                      {t(`status.${repository.status}`)}
                    </span>
                  </td>
                  <td>
                    <span className="mono">{repository.open_vulnerabilities}</span>
                  </td>
                  <td>
                    <RepositoryReviewToggle
                      repositoryId={repository.id}
                      enabled={repository.pr_reviews_enabled}
                      onOptimisticChange={(enabled) => {
                        setToggleFailed(false)
                        patchRepositoryFlag(repository.id, 'pr_reviews_enabled', enabled)
                      }}
                      onFailure={() => setToggleFailed(true)}
                    />
                  </td>
                  <td>
                    <span
                      className="badge badge-muted"
                      aria-describedby="supply-chain-caption"
                    >
                      {t('supplyChain.pending')}
                    </span>
                    <span className="visually-hidden" id="supply-chain-caption">
                      {t('supplyChain.caption')}
                    </span>
                  </td>
                  <td>
                    <span className="mono timestamp">
                      {formatDateTime(
                        repository.last_tested_at,
                        i18n.language,
                        tCommon('values.never'),
                      )}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ConnectRepositoryModal
        isOpen={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        onConnected={refresh}
      />
    </section>
  )
}
