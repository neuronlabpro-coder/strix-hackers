import { useMemo, useState } from 'react'
import {
  Boxes,
  KeyRound,
  LoaderCircle,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ApiToken, ApiTokenCreated } from '../../types/api'
import { CreateTokenModal, SecretRevealModal } from './CreateTokenModal'
import { useApiAccess } from './useApiAccess'
import { WebhooksTab } from './WebhooksTab'

type Tab = 'tokens' | 'webhooks' | 'mcp'

/**
 * Pantalla de API Access.
 *
 * Las tres pestañas comparten una misma decisión de fondo: el token es un artefacto
 * eterno. Un token robado no se ve en ninguna pantalla, y por eso la primera pestaña es
 * la que tiene el interruptor de revocados y la segunda y la tercera no existen aún.
 * Cuando existan, tendrán que poder provocar una revocación desde el propio aviso de
 * entrega fallida, o el usuario tendrá que adivinar que la pestaña de tokens es donde se corta el
 * acceso.
 */
export function ApiAccessPage() {
  const { t } = useTranslation('apiAccess')
  const [tab, setTab] = useState<Tab>('tokens')
  const [isCreateOpen, setIsCreateOpen] = useState(false)
  const [pendingRevoke, setPendingRevoke] = useState<ApiToken | null>(null)
  const [revealed, setRevealed] = useState<ApiTokenCreated | null>(null)

  const {
    catalog,
    tokens,
    isLoading,
    loadFailed,
    includeRevoked,
    setIncludeRevoked,
    refresh,
    create,
    revoke,
    isRevoking,
    revokeFailed,
  } = useApiAccess()

  const activeCount = useMemo(
    () => tokens.filter((item) => item.revoked_at === null).length,
    [tokens],
  )

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <button
            className="icon-button"
            type="button"
            aria-label={t('actions.refresh')}
            disabled={isLoading}
            onClick={refresh}
          >
            {isLoading ? (
              <LoaderCircle size={18} className="spin" aria-hidden="true" />
            ) : (
              <RefreshCw size={18} aria-hidden="true" />
            )}
          </button>
          <button
            className="primary-button"
            type="button"
            // Se deshabilita mientras carga para que el modal no se abra con un catálogo
            // vacío. Con el catálogo en cero, el contador diría "0 de 0 seleccionados" y
            // los grupos no existirían: el usuario vería un formulario de permisos
            // impossível de rellenar y sin explicación de por qué.
            disabled={isLoading || loadFailed || catalog.total === 0}
            onClick={() => setIsCreateOpen(true)}
          >
            <Plus size={16} aria-hidden="true" />
            <span>{t('actions.newToken')}</span>
          </button>
        </div>
      </header>

      <div className="tab-bar" role="tablist" aria-label={t('tabs.label')}>
        {(['tokens', 'webhooks', 'mcp'] as const).map((value) => (
          <button
            key={value}
            className={value === tab ? 'tab-button tab-button-active' : 'tab-button'}
            type="button"
            role="tab"
            aria-selected={value === tab}
            onClick={() => setTab(value)}
          >
            <span>{t(`tabs.${value}`)}</span>
          </button>
        ))}
      </div>

      {tab === 'tokens' && (
        <section role="tabpanel" aria-label={t('tabs.tokens')}>
          <TokensTab
            tokens={tokens}
            isLoading={isLoading}
            loadFailed={loadFailed}
            includeRevoked={includeRevoked}
            onIncludeRevoked={setIncludeRevoked}
            activeCount={activeCount}
            onRevoke={setPendingRevoke}
          />
        </section>
      )}

      {tab === 'webhooks' && <WebhooksTab />}
      {tab === 'mcp' && <McpTab />}

      <CreateTokenModal
        catalog={catalog}
        isOpen={isCreateOpen}
        onClose={() => setIsCreateOpen(false)}
        onCreate={async (input) => {
          const created = await create(input)
          setIsCreateOpen(false)
          setRevealed(created)
          return created
        }}
      />

      <SecretRevealModal token={revealed} onClose={() => setRevealed(null)} />

      <RevokeConfirmModal
        token={pendingRevoke}
        isRevoking={isRevoking}
        failed={revokeFailed}
        onCancel={() => setPendingRevoke(null)}
        onConfirm={async () => {
          if (!pendingRevoke) {
            return
          }
          await revoke(pendingRevoke.id)
          setPendingRevoke(null)
        }}
      />
    </div>
  )
}

interface TokensTabProps {
  tokens: ApiToken[]
  isLoading: boolean
  loadFailed: boolean
  includeRevoked: boolean
  onIncludeRevoked: (value: boolean) => void
  activeCount: number
  onRevoke: (token: ApiToken) => void
}

function TokensTab({
  tokens,
  isLoading,
  loadFailed,
  includeRevoked,
  onIncludeRevoked,
  activeCount,
  onRevoke,
}: TokensTabProps) {
  const { t } = useTranslation('apiAccess')

  if (loadFailed) {
    return (
      <div className="empty-card" role="alert">
        <h2>{t('tokens.loadFailed')}</h2>
        <p>{t('tokens.loadFailedHint')}</p>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="empty-card">
        <LoaderCircle size={22} className="spin" aria-hidden="true" />
        <p>{t('tokens.loading')}</p>
      </div>
    )
  }

  return (
    <>
      <div className="filter-bar">
        <p className="cve-count">
          {t('tokens.count', { active: activeCount, total: tokens.length })}
        </p>
        <label className="filter-field">
          <input
            type="checkbox"
            checked={includeRevoked}
            onChange={(event) => onIncludeRevoked(event.target.checked)}
          />
          <span>{t('tokens.includeRevoked')}</span>
        </label>
      </div>

      {tokens.length === 0 ? (
        <div className="empty-card">
          <KeyRound size={22} aria-hidden="true" />
          <h2>{t('tokens.empty')}</h2>
          <p>{t('tokens.emptyHint')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('tokens.columns.name')}</th>
                <th>{t('tokens.columns.prefix')}</th>
                <th>{t('tokens.columns.scopes')}</th>
                <th>{t('tokens.columns.created')}</th>
                <th>{t('tokens.columns.lastUsed')}</th>
                <th aria-label={t('tokens.columns.actions')} />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <TokenRow key={token.id} token={token} onRevoke={onRevoke} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function TokenRow({ token, onRevoke }: { token: ApiToken; onRevoke: (t: ApiToken) => void }) {
  const { t } = useTranslation('apiAccess')
  const isRevoked = token.revoked_at !== null
  const isExpired = token.expires_at !== null && new Date(token.expires_at) <= new Date()

  return (
    <tr className={isRevoked ? 'row-dimmed' : undefined}>
      <td>
        <span className="cell-primary">{token.name}</span>
        {isRevoked && <span className="badge badge-muted">{t('status.revoked')}</span>}
        {!isRevoked && isExpired && (
          <span className="badge badge-expired">{t('status.expired')}</span>
        )}
      </td>
      <td>
        <span className="mono">{token.token_prefix}</span>
      </td>
      <td>
        <ScopeList scopes={token.scopes} />
      </td>
      <td>{formatDate(token.created_at)}</td>
      <td>
        {token.last_used_at === null
          ? t('tokens.neverUsed')
          : t('tokens.usedAt', { value: formatRelative(token.last_used_at) })}
      </td>
      <td>
        {!isRevoked && (
          <button
            className="link-button"
            type="button"
            onClick={() => onRevoke(token)}
          >
            <Trash2 size={14} aria-hidden="true" />
            <span>{t('actions.revoke')}</span>
          </button>
        )}
      </td>
    </tr>
  )
}

/**
 * Los scopes de una fila se muestran como una muestra y su recuento.
 *
 * Un token puede llevar 46 permisos; volcarlos todos en la celda rompe el ancho de la
 * tabla y obliga a desplazamiento horizontal para leer el nombre del token, que es la
 * única columna que importa de un vistazo. La muestra deja claro que hay más y el
 * recuento dice cuántos.
 */
function ScopeList({ scopes }: { scopes: string[] }) {
  const { t } = useTranslation('apiAccess')
  const visible = scopes.slice(0, 3)

  if (scopes.length === 0) {
    return <span className="cell-muted">{t('scopes.noPermissions')}</span>
  }

  return (
    <span className="scope-chips">
      {visible.map((scope) => (
        <code key={scope} className="scope-chip">
          {scope}
        </code>
      ))}
      {scopes.length > visible.length && (
        <span className="scope-chip-overflow" title={scopes.join(', ')}>
          {t('scopes.more', { count: scopes.length - visible.length })}
        </span>
      )}
    </span>
  )
}

function RevokeConfirmModal({
  token,
  isRevoking,
  failed,
  onCancel,
  onConfirm,
}: {
  token: ApiToken | null
  isRevoking: boolean
  failed: boolean
  onCancel: () => void
  onConfirm: () => Promise<void>
}) {
  const { t } = useTranslation('apiAccess')
  if (!token) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="revoke-token-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h2 id="revoke-token-title">{t('revoke.title')}</h2>
            <p className="modal-section-caption">{t('revoke.description', { name: token.name })}</p>
          </div>
        </header>
        <div className="modal-section">
          {/*
            El aviso dice que la fila **no se borra**, y no es un matiz. El backend marca
            `revoked_at` y conserva la fila porque el rastro de qué credenciales
            existieron es parte del valor de auditoría. Un texto que dijera "se eliminará"
            haría que soporte prometiera algo que el sistema no cumple.
          */}
          <p className="revoke-note">{t('revoke.auditNote')}</p>
          {failed && (
            <p className="form-error" role="alert">
              {t('revoke.failed')}
            </p>
          )}
        </div>
        <footer className="modal-footer">
          <button className="secondary-button" type="button" onClick={onCancel}>
            {t('common:cancel')}
          </button>
          <button
            className="danger-button"
            type="button"
            disabled={isRevoking}
            onClick={() => void onConfirm()}
          >
            {isRevoking && <LoaderCircle size={16} className="spin" aria-hidden="true" />}
            <span>{t('revoke.confirm')}</span>
          </button>
        </footer>
      </div>
    </div>
  )
}

function McpTab() {
  const { t } = useTranslation('apiAccess')
  return (
    <section role="tabpanel" aria-label={t('tabs.mcp')}>
      <div className="mcp-card">
        <div className="mcp-card-header">
          <Boxes size={22} aria-hidden="true" />
          <div>
            <h2>{t('mcp.title')}</h2>
            <p>{t('mcp.description')}</p>
          </div>
        </div>

        <div className="form-field">
          <span className="eyebrow">{t('mcp.quickstartLabel')}</span>
          <code className="mcp-command mono">{t('mcp.quickstart')}</code>
        </div>

        <div className="form-field">
          <span className="eyebrow">{t('mcp.configLabel')}</span>
          <pre className="mcp-config">
            <code className="mono">
              {JSON.stringify(
                {
                  mcpServers: {
                    'mind-guard': {
                      type: 'http',
                      url: `${mcpUrl()}/mcp`,
                      headers: { Authorization: 'Bearer <API_TOKEN>' },
                    },
                  },
                },
                null,
                2,
              )}
            </code>
          </pre>
        </div>

        {/*
          Se muestra un marcador de autenticación, no un token real. Poner un token de
          ejemplo que pareciese utilizable invitaría a copiarlo sin saber que no sirve, y
          el `401` llegaría después, en el agente del usuario.
        */}
        <p className="mcp-note">{t('mcp.tokenHint')}</p>
      </div>
    </section>
  )
}

function mcpUrl(): string {
  return typeof window === 'undefined' ? 'https://api.fenix.local' : window.location.origin
}

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString()
}

/**
 * Tiempo relativo en la unidad mayor que quepa en dos cifras.
 *
 * Se elige la unidad grande porque "hace 45 días" se lee de un vistazo y "hace 2.450.000
 * segundos" obliga a hacer la división. Por debajo del minuto se dice "ahora mismo", que
 * es más honesto que "hace 0 s" para un token que se acaba de usar.
 */
function formatRelative(value: string): string {
  const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000)
  if (seconds < 60) {
    return '0 min'
  }
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    return `${minutes} min`
  }
  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    return `${hours} h`
  }
  return `${Math.floor(hours / 24)} d`
}
