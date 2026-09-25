import { useEffect, useState } from 'react'
import { History } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getAuditLog } from '../../lib/api'
import type { AuditLogEntry } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const VULNERABILITY_ENTITY = 'vulnerability'

/**
 * Historial de cambios de estado de un hallazgo (MENU-MAP §3.4).
 *
 * Lee `GET /api/v1/audit-log/` filtrado por entidad. La tabla es append-only
 * (R4), así que lo que se muestra aquí no puede haber sido reescrito después del
 * hecho; por eso el componente no ofrece ninguna acción de edición.
 */
interface HistorySnapshot {
  key: string
  entries: AuditLogEntry[]
  failed: boolean
}

export function AuditHistory({ entityId }: { entityId: string }) {
  const { t } = useTranslation('triage')
  const { t: tCommon } = useTranslation('common')
  const { i18n } = useTranslation()
  const { token, selectedOrganizationId } = useAuth()
  const [snapshot, setSnapshot] = useState<HistorySnapshot>({
    key: '',
    entries: [],
    failed: false,
  })
  const locale = i18n.language

  const requestKey = `${token}:${selectedOrganizationId}:${entityId}`
  const isCurrent = snapshot.key === requestKey
  const entries = isCurrent ? snapshot.entries : []
  const isLoading = Boolean(token && selectedOrganizationId) && !isCurrent
  const loadFailed = isCurrent && snapshot.failed

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getAuditLog(token, selectedOrganizationId, {
      limit: 50,
      offset: 0,
      entityType: VULNERABILITY_ENTITY,
      entityId,
    })
      .then((page) => {
        if (isActive) {
          setSnapshot({ key: requestKey, entries: page.items, failed: false })
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
  }, [entityId, requestKey, selectedOrganizationId, token])

  return (
    <section className="content-card" aria-labelledby="audit-history-title">
      <p className="eyebrow">{t('history.title')}</p>
      <h2 id="audit-history-title">{t('history.caption')}</h2>
      {isLoading ? (
        <p className="chart-empty">{tCommon('loading')}</p>
      ) : loadFailed ? null : entries.length === 0 ? (
        <p className="chart-empty">{t('history.empty')}</p>
      ) : (
        <ol className="audit-list">
          {entries.map((entry) => (
            <li key={entry.id} className="audit-item">
              <span className="audit-marker" aria-hidden="true">
                <History size={13} />
              </span>
              <div className="audit-body">
                <p className="audit-transition">
                  <span className="mono audit-state">{entry.from_state ?? '-'}</span>
                  <span className="audit-arrow" aria-hidden="true">
                    →
                  </span>
                  <span className="mono audit-state">{entry.to_state ?? '-'}</span>
                </p>
                <p className="chart-empty">
                  {new Intl.DateTimeFormat(locale, {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                  }).format(new Date(entry.created_at))}
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
