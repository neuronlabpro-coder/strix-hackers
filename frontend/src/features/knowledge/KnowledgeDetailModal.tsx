import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getKnowledgeEntry } from '../../lib/api'
import type { KnowledgeDetail } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { CodeSample } from './CodeSample'

export interface KnowledgeDetailModalProps {
  entryId: string | null
  onClose: () => void
}

export function KnowledgeDetailModal({ entryId, onClose }: KnowledgeDetailModalProps) {
  const { t } = useTranslation('knowledge')
  const { token, selectedOrganizationId } = useAuth()
  const dialogRef = useRef<HTMLDivElement>(null)
  const [snapshot, setSnapshot] = useState<{
    key: string
    entry: KnowledgeDetail | null
    failed: boolean
  }>({ key: '', entry: null, failed: false })

  const requestKey = `${token}:${selectedOrganizationId}:${entryId ?? ''}`
  const isCurrent = snapshot.key === requestKey
  const entry = isCurrent ? snapshot.entry : null
  const isLoading =
    entryId !== null &&
    Boolean(token && selectedOrganizationId) &&
    !isCurrent &&
    !snapshot.failed
  const loadFailed = isCurrent && snapshot.failed

  useEffect(() => {
    if (!entryId || !token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getKnowledgeEntry(token, selectedOrganizationId, entryId)
      .then((payload) => {
        if (isActive) {
          setSnapshot({ key: requestKey, entry: payload, failed: false })
        }
      })
      .catch(() => {
        if (isActive) {
          setSnapshot((current) => ({ ...current, key: requestKey, failed: true }))
        }
      })
    return () => {
      isActive = false
    }
  }, [entryId, requestKey, selectedOrganizationId, token])

  useEffect(() => {
    if (entryId === null) {
      return
    }
    const previouslyFocused = document.activeElement as HTMLElement | null
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    )
    focusable?.[0]?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab' || !focusable || focusable.length === 0) {
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      previouslyFocused?.focus()
    }
  }, [entryId, onClose])

  if (entryId === null) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal modal-wide"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="knowledge-detail-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow">{entry ? entry.reference_code : t('states.loading')}</p>
            <h2 id="knowledge-detail-title">{entry?.title ?? t('states.loading')}</h2>
            {entry ? (
              <p className="page-description">
                <span className={`badge badge-status-${entry.severity.toLowerCase()}`}>
                  {t(`filters.severity`)}: {entry.severity}
                </span>{' '}
                <span className="mono">{entry.owasp_category}</span>
              </p>
            ) : null}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label={t('detail.close')}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <section className="modal-section">
          {isLoading ? <p className="chart-empty">{t('states.loading')}</p> : null}
          {loadFailed ? <p className="inline-notice inline-notice-warning">{t('states.error')}</p> : null}
          {entry ? (
            <>
              <div className="metadata-item">
                <dt>{t('detail.risk')}</dt>
                <dd>{entry.risk_summary}</dd>
              </div>
              <CodeSample
                title={t('detail.vulnerable')}
                code={entry.vulnerable_example}
                tone="vulnerable"
                copyLabel={t('detail.copy')}
                copiedLabel={t('detail.copied')}
              />
              <CodeSample
                title={t('detail.secure')}
                code={entry.secure_example}
                tone="secure"
                copyLabel={t('detail.copy')}
                copiedLabel={t('detail.copied')}
              />
              <div className="metadata-item">
                <dt>{t('detail.mitigation')}</dt>
                <dd>{entry.mitigation}</dd>
              </div>
            </>
          ) : null}
        </section>
      </div>
    </div>
  )
}
