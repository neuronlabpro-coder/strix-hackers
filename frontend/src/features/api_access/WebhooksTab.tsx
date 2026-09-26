import { useCallback, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Copy,
  LoaderCircle,
  Plug,
  Plus,
  Radio,
  Trash2,
  Webhook,
  X,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../lib/api'
import type {
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEventCatalog,
  WebhookPingResult,
} from '../../types/api'
import { useDeliveryHistory, useWebhooks } from './useWebhooks'

/**
 * Pestaña de webhooks salientes.
 *
 * La pantalla tiene dos audiences distintos y cada uno mira una columna: el usuario
 * necesita ver la URL para reconocerla, y soporte necesita ver el contador de fallos para
 * saber si el problema es del receptor o de la red. Por eso la tabla enseña las dos y el
 * estado combina tres cosas —activo, pausado a mano, apagado por fallos— en vez de un
 * único booleano que no distinguiría "lo pausé yo" de "la plataforma lo apagó".
 */
export function WebhooksTab() {
  const { t } = useTranslation('apiAccess')
  const { catalog, endpoints, isLoading, loadFailed, refresh, create, remove, setActive, ping } =
    useWebhooks()
  const [isCreateOpen, setIsCreateOpen] = useState(false)
  const [revealed, setRevealed] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<WebhookEndpoint | null>(null)
  const [pingResult, setPingResult] = useState<{ id: string; result: WebhookPingResult } | null>(
    null,
  )
  const [pingingId, setPingingId] = useState<string | null>(null)
  const [historyFor, setHistoryFor] = useState<WebhookEndpoint | null>(null)

  const runPing = useCallback(
    async (endpoint: WebhookEndpoint) => {
      setPingingId(endpoint.id)
      try {
        const result = await ping(endpoint.id)
        setPingResult({ id: endpoint.id, result })
      } catch {
        setPingResult({
          id: endpoint.id,
          result: {
            delivered: false,
            status_code: null,
            execution_time_ms: null,
            error_message: t('webhooks.pingFailed'),
            delivery_id: '',
          },
        })
      } finally {
        setPingingId(null)
      }
    },
    [ping, t],
  )

  if (loadFailed) {
    return (
      <div className="empty-card" role="alert">
        <Webhook size={24} aria-hidden="true" />
        <h2>{t('webhooks.loadFailed')}</h2>
        <p>{t('webhooks.loadFailedHint')}</p>
        <button className="secondary-button" type="button" onClick={refresh}>
          {t('actions.refresh')}
        </button>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="empty-card">
        <LoaderCircle size={22} className="spin" aria-hidden="true" />
        <p>{t('webhooks.loading')}</p>
      </div>
    )
  }

  return (
    <>
      <div className="filter-bar">
        <p className="cve-count">
          {t('webhooks.count', { total: endpoints.length })}
        </p>
        <button
          className="primary-button"
          type="button"
          // Se deshabilita sin catálogo para que el modal no se abra con cero eventos y
          // un contador que diría "0 de 0 seleccionados".
          disabled={!catalog || catalog.total === 0}
          onClick={() => setIsCreateOpen(true)}
        >
          <Plus size={16} aria-hidden="true" />
          <span>{t('webhooks.addEndpoint')}</span>
        </button>
      </div>

      {endpoints.length === 0 ? (
        <div className="empty-card">
          <Webhook size={24} aria-hidden="true" />
          <h2>{t('webhooks.empty')}</h2>
          <p>{t('webhooks.emptyHint')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('webhooks.columns.url')}</th>
                <th>{t('webhooks.columns.events')}</th>
                <th>{t('webhooks.columns.status')}</th>
                <th>{t('webhooks.columns.failures')}</th>
                <th aria-label={t('webhooks.columns.actions')} />
              </tr>
            </thead>
            <tbody>
              {endpoints.map((endpoint) => (
                <WebhookRow
                  key={endpoint.id}
                  endpoint={endpoint}
                  pingResult={pingResult?.id === endpoint.id ? pingResult.result : null}
                  isPinging={pingingId === endpoint.id}
                  onPing={() => void runPing(endpoint)}
                  onToggle={(value) => void setActive(endpoint.id, value)}
                  onHistory={() => setHistoryFor(endpoint)}
                  onDelete={() => setPendingDelete(endpoint)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {catalog && (
        <CreateWebhookModal
          catalog={catalog}
          isOpen={isCreateOpen}
          onClose={() => setIsCreateOpen(false)}
          onCreate={async (input) => {
            const secret = await create(input)
            setIsCreateOpen(false)
            setRevealed(secret)
            return secret
          }}
        />
      )}

      {revealed !== null && (
        <SecretModal secret={revealed} onClose={() => setRevealed(null)} />
      )}

      <DeleteConfirmModal
        endpoint={pendingDelete}
        onCancel={() => setPendingDelete(null)}
        onConfirm={async () => {
          if (!pendingDelete) {
            return
          }
          await remove(pendingDelete.id)
          setPendingDelete(null)
        }}
      />

      {historyFor && (
        <DeliveryHistoryModal endpoint={historyFor} onClose={() => setHistoryFor(null)} />
      )}
    </>
  )
}

interface WebhookRowProps {
  endpoint: WebhookEndpoint
  pingResult: WebhookPingResult | null
  isPinging: boolean
  onPing: () => void
  onToggle: (value: boolean) => void
  onHistory: () => void
  onDelete: () => void
}

function WebhookRow({
  endpoint,
  pingResult,
  isPinging,
  onPing,
  onToggle,
  onHistory,
  onDelete,
}: WebhookRowProps) {
  const { t } = useTranslation('apiAccess')
  const isAutoDisabled = Boolean(endpoint.is_auto_disabled)
  const visibleEvents = endpoint.event_types.slice(0, 3)

  return (
    <tr className={endpoint.is_active ? undefined : 'row-dimmed'}>
      <td>
        <span className="cell-primary mono">{endpoint.url}</span>
        {endpoint.description && <span className="cell-muted">{endpoint.description}</span>}
        {isAutoDisabled && (
          <span className="badge badge-expired" title={t('webhooks.autoDisabledHint')}>
            {t('webhooks.autoDisabled')}
          </span>
        )}
      </td>
      <td>
        <span className="scope-chips">
          {visibleEvents.map((event) => (
            <code key={event} className="scope-chip">
              {event}
            </code>
          ))}
          {endpoint.event_types.length > visibleEvents.length && (
            <span className="scope-chip-overflow" title={endpoint.event_types.join(', ')}>
              {t('scopes.more', { count: endpoint.event_types.length - visibleEvents.length })}
            </span>
          )}
        </span>
      </td>
      <td>
        <label className="filter-field">
          <input
            type="checkbox"
            checked={endpoint.is_active}
            onChange={(event) => onToggle(event.target.checked)}
          />
          <span>
            {endpoint.is_active ? t('webhooks.active') : t('webhooks.paused')}
          </span>
        </label>
      </td>
      <td>
        <span className={failureClass(endpoint.consecutive_failures)}>
          {endpoint.consecutive_failures}
        </span>
        {pingResult && (
          <span className="cell-muted" role="status">
            {pingResult.delivered
              ? t('webhooks.pingOk', {
                  code: pingResult.status_code ?? 0,
                  ms: pingResult.execution_time_ms ?? 0,
                })
              : t('webhooks.pingFailed')}
          </span>
        )}
      </td>
      <td>
        <div className="webhook-actions">
          <button
            className="link-button"
            type="button"
            disabled={isPinging}
            onClick={onPing}
            title={t('webhooks.pingHint')}
          >
            {isPinging ? (
              <LoaderCircle size={14} className="spin" aria-hidden="true" />
            ) : (
              <Radio size={14} aria-hidden="true" />
            )}
            <span>{t('webhooks.ping')}</span>
          </button>
          <button className="link-button" type="button" onClick={onHistory}>
            <Plug size={14} aria-hidden="true" />
            <span>{t('webhooks.history')}</span>
          </button>
          <button className="link-button" type="button" onClick={onDelete}>
            <Trash2 size={14} aria-hidden="true" />
            <span>{t('actions.revoke')}</span>
          </button>
        </div>
      </td>
    </tr>
  )
}

/**
 * El contador de fallos se pinta en ámbar en cuanto supera cero y en rojo al llegar al
 * umbral de auto-desactivado. El número solo no dice nada: ocho fallos de un endpoint
 * pausable es distinto de ocho de uno que se apagará solo en dos entregas.
 */
function failureClass(failures: number): string {
  if (failures === 0) {
    return 'cell-failures-ok'
  }
  return failures >= 10 ? 'cell-failures-critical' : 'cell-failures-warn'
}

interface CreateWebhookModalProps {
  catalog: WebhookEventCatalog
  isOpen: boolean
  onClose: () => void
  onCreate: (input: {
    url: string
    description: string | null
    eventTypes: string[]
  }) => Promise<string>
}

function CreateWebhookModal({
  catalog,
  isOpen,
  onClose,
  onCreate,
}: CreateWebhookModalProps) {
  const { t } = useTranslation('apiAccess')
  const [url, setUrl] = useState('')
  const [description, setDescription] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [isSaving, setIsSaving] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  const trimmedUrl = url.trim()
  const canSubmit = trimmedUrl.length > 7 && selected.size > 0 && !isSaving

  const toggle = (event: string) => {
    const next = new Set(selected)
    if (next.has(event)) {
      next.delete(event)
    } else {
      next.add(event)
    }
    setSelected(next)
  }

  const toggleGroup = (events: string[]) => {
    const next = new Set(selected)
    const all = events.every((event) => next.has(event))
    for (const event of events) {
      if (all) {
        next.delete(event)
      } else {
        next.add(event)
      }
    }
    setSelected(next)
  }

  const allEvents = useMemo(
    () => catalog.groups.flatMap((group) => group.events.map((event) => event.event_type)),
    [catalog],
  )

  if (!isOpen) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-webhook-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h2 id="create-webhook-title">{t('webhooks.create.title')}</h2>
            <p className="modal-caption">{t('webhooks.create.description')}</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label={t('common:close')}
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="modal-section">
          <div className="form-field">
            <label htmlFor="webhook-url">{t('webhooks.create.url')}</label>
            <input
              id="webhook-url"
              type="url"
              value={url}
              placeholder={t('webhooks.create.urlPlaceholder')}
              onChange={(event) => setUrl(event.target.value)}
            />
            {/*
              El aviso de HTTPS y de redes privadas va antes del campo, no en el error de
              envío. Es la mitad de los `422` que va a devolver el servidor, y verlo antes
              de escribir evita el viaje de ida y vuelta.
            */}
            <p className="form-hint">{t('webhooks.create.urlHint')}</p>
          </div>

          <div className="form-field">
            <label htmlFor="webhook-description">{t('webhooks.create.descriptionLabel')}</label>
            <input
              id="webhook-description"
              type="text"
              maxLength={255}
              value={description}
              placeholder={t('webhooks.create.descriptionPlaceholder')}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>

          <div className="form-field">
            <span className="eyebrow">{t('webhooks.create.events')}</span>
            <div className="scope-selector">
              <header className="scope-selector-header">
                <div className="scope-counter">
                  <span className="scope-counter-value mono">
                    {selected.size} / {catalog.total}
                  </span>
                  <span className="scope-counter-label">
                    {t('scopes.selectedOf', {
                      chosen: selected.size,
                      total: catalog.total,
                    })}
                  </span>
                </div>
                <button
                  className="link-button"
                  type="button"
                  onClick={() =>
                    setSelected(
                      selected.size === allEvents.length ? new Set() : new Set(allEvents),
                    )
                  }
                >
                  {t(
                    selected.size === allEvents.length ? 'scopes.clearAll' : 'scopes.selectAll',
                  )}
                </button>
              </header>

              <ul className="scope-groups">
                {catalog.groups.map((group) => {
                  const events = group.events.map((event) => event.event_type)
                  const isCollapsed = collapsed.has(group.group)
                  return (
                    <li key={group.group} className="scope-group">
                      <div className="scope-group-header">
                        <button
                          className="scope-group-toggle"
                          type="button"
                          aria-expanded={!isCollapsed}
                          onClick={() => {
                            const next = new Set(collapsed)
                            if (next.has(group.group)) {
                              next.delete(group.group)
                            } else {
                              next.add(group.group)
                            }
                            setCollapsed(next)
                          }}
                        >
                          {isCollapsed ? (
                            <ChevronRight size={16} aria-hidden="true" />
                          ) : (
                            <ChevronDown size={16} aria-hidden="true" />
                          )}
                          <span>{group.group}</span>
                        </button>
                        <span className="scope-group-count mono">
                          {events.filter((event) => selected.has(event)).length}/{events.length}
                        </span>
                        <button
                          className="link-button"
                          type="button"
                          onClick={() => toggleGroup(events)}
                        >
                          {t(
                            events.every((event) => selected.has(event))
                              ? 'scopes.clearGroup'
                              : 'scopes.selectGroup',
                          )}
                        </button>
                      </div>
                      {!isCollapsed && (
                        <ul className="scope-list">
                          {group.events.map((event) => (
                            <li key={event.event_type}>
                              <label className="scope-item">
                                <input
                                  type="checkbox"
                                  checked={selected.has(event.event_type)}
                                  onChange={() => toggle(event.event_type)}
                                />
                                <span className="scope-item-text">
                                  <span className="scope-item-label">
                                    {/*
                                      Se usa el `label_key` que envía el backend y no una
                                      ruta construida con `group` y `action`. `ping` no
                                      lleva punto, así que la partición daría una acción
                                      vacía y la ruta quedaría `events.ping.` con un punto
                                      de más. Usar la clave que llega hace que añadir un
                                      evento con otra forma de nombre no rompa el panel.
                                    */}
                                    {t(event.label_key, { defaultValue: event.action })}
                                  </span>
                                  <span className="scope-item-value mono">
                                    {event.event_type}
                                  </span>
                                </span>
                              </label>
                            </li>
                          ))}
                        </ul>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          </div>

          {failed && (
            <p className="form-error" role="alert">
              {failed}
            </p>
          )}
        </div>

        <footer className="modal-footer">
          <button className="secondary-button" type="button" onClick={onClose}>
            {t('common:cancel')}
          </button>
          <button
            className="primary-button"
            type="button"
            disabled={!canSubmit}
            onClick={() => {
              setIsSaving(true)
              setFailed(null)
              void onCreate({
                url: trimmedUrl,
                description: description.trim() || null,
                eventTypes: [...selected].sort(),
              }).catch((error: unknown) => {
                setFailed(
                  error instanceof ApiError && error.status === 422
                    ? t('webhooks.create.urlRejected')
                    : t('webhooks.create.failed'),
                )
                setIsSaving(false)
              })
            }}
          >
            {isSaving ? (
              <LoaderCircle size={16} className="spin" aria-hidden="true" />
            ) : (
              <Webhook size={16} aria-hidden="true" />
            )}
            <span>{t('webhooks.create.submit')}</span>
          </button>
        </footer>
      </div>
    </div>
  )
}

function SecretModal({ secret, onClose }: { secret: string; onClose: () => void }) {
  const { t } = useTranslation('apiAccess')
  const [copied, setCopied] = useState(false)

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="webhook-secret-title"
        aria-describedby="webhook-secret-warning"
      >
        <header className="modal-header">
          <div>
            <h2 id="webhook-secret-title">{t('webhooks.secret.title')}</h2>
            <p className="modal-caption">{t('webhooks.secret.description')}</p>
          </div>
        </header>

        <div className="modal-section">
          <p className="reveal-warning" id="webhook-secret-warning" role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            {t('webhooks.secret.warning')}
          </p>

          <div className="form-field">
            <label htmlFor="webhook-secret-value">{t('webhooks.secret.fieldLabel')}</label>
            <div className="reveal-row">
              <input
                id="webhook-secret-value"
                className="text-input mono"
                type="text"
                readOnly
                value={secret}
                onFocus={(event) => event.currentTarget.select()}
              />
              <button
                className="secondary-button"
                type="button"
                onClick={() => {
                  void navigator.clipboard.writeText(secret).then(
                    () => setCopied(true),
                    () => setCopied(false),
                  )
                }}
              >
                <Copy size={16} aria-hidden="true" />
                <span>{t(copied ? 'reveal.copied' : 'reveal.copy')}</span>
              </button>
            </div>
          </div>

          <p className="form-hint">{t('webhooks.secret.verifyHint')}</p>
        </div>

        <footer className="modal-footer">
          <button className="primary-button" type="button" onClick={onClose}>
            {t('webhooks.secret.acknowledge')}
          </button>
        </footer>
      </div>
    </div>
  )
}

function DeleteConfirmModal({
  endpoint,
  onCancel,
  onConfirm,
}: {
  endpoint: WebhookEndpoint | null
  onCancel: () => void
  onConfirm: () => Promise<void>
}) {
  const { t } = useTranslation('apiAccess')
  const [isDeleting, setIsDeleting] = useState(false)
  if (!endpoint) {
    return null
  }
  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="delete-webhook-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h2 id="delete-webhook-title">{t('webhooks.delete.title')}</h2>
            <p className="modal-caption">
              {t('webhooks.delete.description', { url: endpoint.url })}
            </p>
          </div>
        </header>
        <div className="modal-section">
          {/*
            El aviso dice que el historial se borra con el endpoint. Es cierto y es
            distinto del rastro forense, que sobrevive: sin decirlo, soporte acabaría
            prometiendo un historial que ya no existe.
          */}
          <p className="revoke-note">{t('webhooks.delete.historyNote')}</p>
        </div>
        <footer className="modal-footer">
          <button className="secondary-button" type="button" onClick={onCancel}>
            {t('common:cancel')}
          </button>
          <button
            className="danger-button"
            type="button"
            disabled={isDeleting}
            onClick={() => {
              setIsDeleting(true)
              void onConfirm().catch(() => setIsDeleting(false))
            }}
          >
            {isDeleting && <LoaderCircle size={16} className="spin" aria-hidden="true" />}
            <span>{t('webhooks.delete.confirm')}</span>
          </button>
        </footer>
      </div>
    </div>
  )
}

function DeliveryHistoryModal({
  endpoint,
  onClose,
}: {
  endpoint: WebhookEndpoint
  onClose: () => void
}) {
  const { t } = useTranslation('apiAccess')
  const { deliveries, total, isLoading, failed, reload } = useDeliveryHistory(endpoint.id)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="webhook-history-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h2 id="webhook-history-title">{t('webhooks.historyTitle')}</h2>
            <p className="modal-caption mono">{endpoint.url}</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label={t('common:close')}
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="modal-section">
          {isLoading ? (
            <div className="empty-card">
              <LoaderCircle size={20} className="spin" aria-hidden="true" />
              <p>{t('webhooks.historyLoading')}</p>
            </div>
          ) : failed ? (
            <div className="empty-card" role="alert">
              <p>{t('webhooks.historyFailed')}</p>
              <button className="secondary-button" type="button" onClick={reload}>
                {t('actions.refresh')}
              </button>
            </div>
          ) : deliveries.length === 0 ? (
            <div className="empty-card">
              <Webhook size={20} aria-hidden="true" />
              <p>{t('webhooks.historyEmpty')}</p>
            </div>
          ) : (
            <>
              <p className="cve-count">{t('webhooks.historyCount', { total })}</p>
              <ul className="delivery-list">
                {deliveries.map((delivery) => (
                  <DeliveryRow
                    key={delivery.id}
                    delivery={delivery}
                    isExpanded={expanded.has(delivery.id)}
                    onToggle={() => {
                      const next = new Set(expanded)
                      if (next.has(delivery.id)) {
                        next.delete(delivery.id)
                      } else {
                        next.add(delivery.id)
                      }
                      setExpanded(next)
                    }}
                  />
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function DeliveryRow({
  delivery,
  isExpanded,
  onToggle,
}: {
  delivery: WebhookDelivery
  isExpanded: boolean
  onToggle: () => void
}) {
  const { t } = useTranslation('apiAccess')
  const codigo = delivery.status_code
  const clase =
    codigo === null
      ? 'badge badge-expired'
      : codigo >= 200 && codigo < 300
        ? 'badge badge-on'
        : 'badge badge-status-failed'

  return (
    <li className="delivery-item">
      <button className="delivery-summary" type="button" onClick={onToggle} aria-expanded={isExpanded}>
        {isExpanded ? (
          <ChevronDown size={16} aria-hidden="true" />
        ) : (
          <ChevronRight size={16} aria-hidden="true" />
        )}
        <span className={clase}>
          {codigo === null ? t('webhooks.noResponse') : codigo}
        </span>
        <span className="mono">{delivery.event_type}</span>
        <span className="cell-muted mono">
          {delivery.attempt > 1 && t('webhooks.attempt', { n: delivery.attempt })}
        </span>
        <span className="cell-muted">
          {delivery.execution_time_ms !== null
            ? t('webhooks.inMs', { ms: delivery.execution_time_ms })
            : ''}
        </span>
        <span className="cell-muted">{new Date(delivery.delivered_at).toLocaleString()}</span>
      </button>

      {isExpanded && (
        <div className="delivery-detail">
          {delivery.error_message && (
            <p className="form-error" role="note">
              {delivery.error_message}
            </p>
          )}
          {/*
            Los dos cuerpos se muestran en `<pre>` y no formateados como JSON con color.
            Un volcado sin colorear se puede copiar entero, y quien llega aquí viene a
            pegar el JSON en su propio log.
          */}
          <div>
            <span className="eyebrow">{t('webhooks.payloadLabel')}</span>
            <pre className="delivery-payload mono">
              {JSON.stringify(delivery.payload, null, 2)}
            </pre>
          </div>
          <div>
            <span className="eyebrow">{t('webhooks.responseLabel')}</span>
            <pre className="delivery-payload mono">
              {delivery.response_body ?? t('webhooks.noBody')}
            </pre>
          </div>
        </div>
      )}
    </li>
  )
}
