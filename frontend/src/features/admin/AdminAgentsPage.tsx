/**
 * `/admin/agents`: los agentes de escaneo de **todos** los clientes, desde la consola de
 * plataforma.
 *
 * ## Por qué esta vista existe y por qué no hay nada en el panel que la haga innecesaria
 *
 * Porque las dos responden a preguntas distintas. Un cliente ve sus agentes porque necesita
 * escanear, y un cliente no puede ver los de otro —eso es R3, y la vista del panel lo
 * comprueba con la cabecera de organización—. Un operador de plataforma tiene una pregunta que
 * **ningún cliente puede contestar**: si hay diez clientes con un agente dado de alta y ninguno
 * conectado nunca, eso es un problema de despliegue, no de cliente, y solo se ve desde fuera.
 *
 * ## Por qué aquí no hay alta de agentes
 *
 * Porque el alta es del cliente. Un token de agente es una credencial con acceso a la red de
 * ese cliente, y que el operador de la plataforma se lleve una credencial de su red es
 * exactamente el tipo de cosa que un superusuario no debería poder hacer por la vía de un
 * botón. El operador puede ver y puede dar de baja; el alta la hace el cliente.
 *
 * ## Por qué la ventana de vida llega del servidor
 *
 * Porque "conectado" es un criterio con un plazo, y un plazo evaluado en dos sitios con dos
 * relojes es un plazo que un día no coincide. El servidor manda la ventana y el `connected`, y
 * la insignia y el texto usan los dos: la misma cifra que ve el cliente en su panel.
 */

import { useCallback, useState } from 'react'
import { Cpu, RefreshCw, ShieldOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getAdminAgents, revokeAgentAsAdmin } from '../../lib/adminApi'
import { formatCount } from '../../lib/format'
import type { AdminAgent } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { PaginationBar } from './PaginationBar'
import { useAdminPage } from './useAdminPage'

export function AdminAgentsPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()
  const { notify } = useToast()

  const [search, setSearch] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)
  const [pending, setPending] = useState<AdminAgent | null>(null)

  const load = useCallback(
    (limit: number, offset: number) =>
      // Igual que en el resto de la consola: sin sesion no se pide nada, y se devuelve una
      // pagina vacia en vez de lanzar una peticion con un token inexistente.
      token === null
        ? Promise.resolve({ items: [], total: 0, ventana_de_vida: 300 })
        : getAdminAgents(token, { limit, offset }),
    [token],
  )

  const page = useAdminPage<AdminAgent, { ventana_de_vida: number }>(load, {
    pageSize: 25,
    filterKey: `${token}:${search}`,
    disabled: !token || user?.is_superuser !== true,
  })

  // El buscador es **del cliente** y no un parametro de la consulta, a diferencia de usuarios y
  // tenants. La razon es que esta lista no crece como las otras: un agente es un despliegue, y
  // hay uno por máquina o por segmento, no uno por fila de una tabla. Mandar cada tecla a la
  // base para filtrar treinta filas seria una ida a la base por pulsación.
  const visibles = search.trim() === '' ? page.items : page.items.filter((agente) => {
    const aguja = search.trim().toLowerCase()
    return (
      agente.name.toLowerCase().includes(aguja) ||
      agente.organization_name.toLowerCase().includes(aguja) ||
      (agente.platform_hint ?? '').toLowerCase().includes(aguja)
    )
  })

  const ventana = page.extra?.ventana_de_vida ?? 300
  const conectados = page.items.filter((agente) => agente.connected).length

  async function revocar(agente: AdminAgent, motivo: string) {
    if (token === null) return
    setBusyId(agente.id)
    try {
      await revokeAgentAsAdmin(token, agente.id, motivo)
      notify('success', t('agents.revoked', { name: agente.name }))
      page.refresh()
    } catch {
      notify('error', t('agents.revokeFailed'))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="page-section">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('agents.eyebrow')}</p>
          <h1>{t('agents.title')}</h1>
          <p className="page-description">{t('agents.subtitle')}</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={page.refresh}
            aria-label={t('agents.refresh')}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('agents.refresh')}</span>
          </button>
        </div>
      </div>

      <div className="metric-grid">
        <article className="metric-card">
          <p className="eyebrow">{t('agents.kpis.total')}</p>
          <strong className="metric-value mono">{formatCount(page.total)}</strong>
          <span className="metric-caption">{t('agents.kpis.totalCaption')}</span>
        </article>
        <article className="metric-card">
          <p className="eyebrow">{t('agents.kpis.connected')}</p>
          <strong className="metric-value mono">
            {formatCount(conectados)}
            <span className="metric-suffix">/{formatCount(page.items.length)}</span>
          </strong>
          <span className="metric-caption">
            {t('agents.kpis.connectedIn', { minutes: Math.max(1, Math.round(ventana / 60)) })}
          </span>
        </article>
      </div>

      <div className="content-card">
        <div className="section-header">
          <h2>{t('agents.listTitle')}</h2>
          <span className="badge">{formatCount(page.total)}</span>
        </div>

        <div className="filter-bar">
          <div className="filter-field filter-field-search">
            <label htmlFor="admin-agents-search">{t('agents.filters.search')}</label>
            <div className="search-field">
              <input
                id="admin-agents-search"
                type="search"
                value={search}
                onChange={(evento) => setSearch(evento.target.value)}
                placeholder={t('agents.filters.searchPlaceholder')}
              />
            </div>
          </div>
        </div>

        {page.loadFailed ? (
          <div className="empty-card">
            <Cpu size={22} aria-hidden="true" />
            <h3>{t('agents.loadFailed')}</h3>
            <button type="button" className="secondary-button" onClick={page.refresh}>
              {t('agents.refresh')}
            </button>
          </div>
        ) : visibles.length === 0 && !page.isLoading ? (
          <div className="empty-card">
            <Cpu size={22} aria-hidden="true" />
            <h3>{t('agents.emptyTitle')}</h3>
            <p>{t('agents.emptyBody')}</p>
          </div>
        ) : (
          <>
            <div className="table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('agents.table.agent')}</th>
                    <th>{t('agents.table.tenant')}</th>
                    <th>{t('agents.table.platform')}</th>
                    <th>{t('agents.table.enrolled')}</th>
                    <th>{t('agents.table.lastSeen')}</th>
                    <th aria-label={t('agents.table.actions')} />
                  </tr>
                </thead>
                <tbody>
                  {visibles.map((agente) => (
                    <tr key={agente.id}>
                      <td>
                        <span className="mono">{agente.name}</span>{' '}
                        <span className="cell-muted mono">{agente.token_prefix}…</span>
                      </td>
                      <td>{agente.organization_name}</td>
                      <td className="mono">
                        {agente.platform_hint ?? <span className="cell-muted">—</span>}
                      </td>
                      <td>{new Date(agente.enrolled_at).toLocaleDateString()}</td>
                      <td>
                        <AgentBadge agente={agente} />
                      </td>
                      <td className="cell-acciones">
                        {agente.status === 'ACTIVE' && (
                          <button
                            type="button"
                            className="ghost-button"
                            disabled={busyId === agente.id}
                            onClick={() => setPending(agente)}
                          >
                            <ShieldOff size={14} aria-hidden="true" />
                            <span>{t('agents.revoke')}</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {search.trim() !== '' && (
              <p className="table-caption">
                {t('agents.filtered', {
                  shown: visibles.length,
                  total: page.items.length,
                })}
              </p>
            )}
          </>
        )}

        <PaginationBar page={page} />
      </div>

      {pending && (
        <RevocarAgenteDialog
          agente={pending}
          onClose={() => setPending(null)}
          onConfirm={(motivo) => {
            const objetivo = pending
            setPending(null)
            void revocar(objetivo, motivo)
          }}
        />
      )}
    </div>
  )
}

/**
 * La insignia de un agente: dado de baja, conectado, o sin conexión.
 *
 * Los tres casos se distinguen porque son tres cosas distintas y la diferencia es la que le
 * importa al operador. «Nunca ha conectado» no es «sin conexión»: la primera significa que el
 * despliegue no llegó a levantarse y la segunda que se cayó. Arreglarlas es distinto.
 */
function AgentBadge({ agente }: { agente: AdminAgent }) {
  const { t } = useTranslation('admin')
  if (agente.status === 'REVOKED') {
    return <span className="badge badge-muted">{t('agents.status.REVOKED')}</span>
  }
  if (agente.last_seen_at === null) {
    return <span className="badge badge-warning">{t('agents.never')}</span>
  }
  return agente.connected ? (
    <span className="badge badge-success">
      {new Date(agente.last_seen_at).toLocaleString()}
    </span>
  ) : (
    <span className="badge badge-muted">{t('agents.stale')}</span>
  )
}

/**
 * La confirmación de la baja.
 *
 * ## Por qué es un diálogo y no un `prompt`
 *
 * Porque la baja **corta el token**: el agente deja poder reclamar trabajos en el momento en
 * que se confirma, y un escaneo que ya estaba en curso se queda sin a quién REPORTar. Eso no se
 * deshace, y por eso la pantalla tiene que decir qué pasa **antes** de que el operador pulse.
 *
 * Y el motivo no es decorativo: va al registro de auditoría, y es la única forma de saber
 * dentro de seis meses por qué un cliente dejó de escanear.
 */
function RevocarAgenteDialog({
  agente,
  onClose,
  onConfirm,
}: {
  agente: AdminAgent
  onClose: () => void
  onConfirm: (motivo: string) => void
}) {
  const { t } = useTranslation('admin')
  const [motivo, setMotivo] = useState('')

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="revocar-agente">
        <div className="modal-header">
          <h2 id="revocar-agente">{t('agents.revokeTitle', { name: agente.name })}</h2>
        </div>
        <div className="modal-body">
          <p className="modal-intro">{t('agents.revokeWarning')}</p>
          <p className="inline-notice inline-notice-warning" role="note">
            {t('agents.revokeInFlight', { tenant: agente.organization_name })}
          </p>
          <label className="field">
            <span>{t('agents.revokeReason')}</span>
            <input
              type="text"
              value={motivo}
              onChange={(evento) => setMotivo(evento.target.value)}
              placeholder={t('agents.revokeReasonPlaceholder')}
              autoFocus
            />
          </label>
          <p className="field-hint">{t('agents.revokeReasonHint')}</p>
        </div>
        <div className="modal-footer">
          <button type="button" className="ghost-button" onClick={onClose}>
            {t('common:cancel')}
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => onConfirm(motivo.trim())}
          >
            {t('agents.revokeConfirm')}
          </button>
        </div>
      </div>
    </div>
  )
}
