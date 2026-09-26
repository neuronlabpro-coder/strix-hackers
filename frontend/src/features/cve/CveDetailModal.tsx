import { useEffect, useRef, useState } from 'react'
import { ShieldAlert, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getCVERecord } from '../../lib/api'
import type { CVEDetail } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { formatDate, formatProbability, isNotFound } from './format'
import { SeverityBadge } from './SeverityBadge'

export interface CveDetailModalProps {
  cveId: string | null
  onClose: () => void
}

export function CveDetailModal({ cveId, onClose }: CveDetailModalProps) {
  const { t } = useTranslation('cve')
  const { token, selectedOrganizationId } = useAuth()
  const dialogRef = useRef<HTMLDivElement>(null)
  const [snapshot, setSnapshot] = useState<{
    key: string
    record: CVEDetail | null
    failed: boolean
    missing: boolean
  }>({ key: '', record: null, failed: false, missing: false })

  const requestKey = `${token}:${selectedOrganizationId}:${cveId ?? ''}`
  const isCurrent = snapshot.key === requestKey
  const record = isCurrent ? snapshot.record : null
  const isLoading =
    cveId !== null &&
    Boolean(token && selectedOrganizationId) &&
    !isCurrent &&
    !snapshot.failed
  const loadFailed = isCurrent && snapshot.failed
  const notFound = isCurrent && snapshot.missing

  useEffect(() => {
    if (!cveId || !token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getCVERecord(token, selectedOrganizationId, cveId)
      .then((payload) => {
        if (isActive) {
          setSnapshot({ key: requestKey, record: payload, failed: false, missing: false })
        }
      })
      .catch((error: unknown) => {
        if (isActive) {
          setSnapshot({
            key: requestKey,
            record: null,
            failed: !isNotFound(error),
            missing: isNotFound(error),
          })
        }
      })
    return () => {
      isActive = false
    }
  }, [cveId, requestKey, selectedOrganizationId, token])

  useEffect(() => {
    if (cveId === null) {
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
  }, [cveId, onClose])

  if (cveId === null) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal modal-wide"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="cve-detail-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow mono">{cveId}</p>
            <h2 id="cve-detail-title">{record?.cve_id ?? t('detail.notFound')}</h2>
            {record ? (
              <p className="page-description">
                <SeverityBadge severity={record.severity} />
                {record.is_kev ? (
                  <span className="badge badge-kev">
                    <ShieldAlert size={12} aria-hidden="true" />
                    {t('detail.kevBadge')}
                  </span>
                ) : null}
              </p>
            ) : null}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label={t('search.clear')}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <section className="modal-section">
          {isLoading ? <p className="chart-empty">{t('states.loading')}</p> : null}
          {loadFailed ? <p className="inline-notice inline-notice-warning">{t('states.error')}</p> : null}
          {notFound ? (
            <>
              <p className="inline-notice inline-notice-warning">{t('detail.notFound')}</p>
              <p className="chart-empty">{t('detail.notFoundDescription')}</p>
            </>
          ) : null}
          {record ? (
            <>
              <div className="metadata-item">
                <dt>{t('table.description')}</dt>
                <dd>{record.description}</dd>
              </div>
              <div className="metadata-item">
                <dt>{t('detail.cvss')}</dt>
                <dd className="mono">{record.cvss_score}</dd>
              </div>
              <div className="metadata-item">
                <dt>{t('detail.epss')}</dt>
                <dd className="mono">
                  {record.epss_score ? formatProbability(record.epss_score) : t('detail.epssUnknown')}
                </dd>
              </div>
              <div className="metadata-item">
                <dt>{t('detail.published')}</dt>
                <dd>{formatDate(record.published_at)}</dd>
              </div>
              <div className="metadata-item">
                <dt>{t('sidebar.kevTitle')}</dt>
                <dd>{record.is_kev ? t('detail.kevTitle') : t('detail.notKev')}</dd>
              </div>
              <button className="secondary-button" type="button" onClick={onClose}>
                <span>{t('detail.back')}</span>
              </button>
            </>
          ) : null}
        </section>
      </div>
    </div>
  )
}
