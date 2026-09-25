import { useCallback, useEffect, useRef, useState } from 'react'
import { Link2, LoaderCircle, RefreshCw, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  ApiError,
  connectRepository,
  getOAuthAuthorizationUrl,
  getRemoteRepositories,
} from '../../lib/api'
import type { GitProvider, RemoteRepository } from '../../types/api'
import { useAuth } from '../auth/useAuth'

type ProviderOption = { provider: GitProvider; labelKey: string }

const PROVIDER_OPTIONS: ProviderOption[] = [
  { provider: 'GITHUB', labelKey: 'modal.connectGitHub' },
  { provider: 'GITLAB', labelKey: 'modal.connectGitLab' },
]

function providerName(provider: GitProvider): string {
  return provider === 'GITHUB' ? 'GitHub' : 'GitLab'
}

type Notice = { kind: 'success' | 'warning' | 'error'; text: string } | null

export interface ConnectRepositoryModalProps {
  isOpen: boolean
  onClose: () => void
  onConnected: () => void
}

export function ConnectRepositoryModal({
  isOpen,
  onClose,
  onConnected,
}: ConnectRepositoryModalProps) {
  const { t } = useTranslation('repositories')
  const { token, selectedOrganizationId } = useAuth()
  const dialogRef = useRef<HTMLDivElement>(null)
  const [provider, setProvider] = useState<GitProvider>('GITHUB')
  const [remoteRepositories, setRemoteRepositories] = useState<RemoteRepository[]>([])
  const [isLoadingInventory, setIsLoadingInventory] = useState(false)
  const [pendingRemoteId, setPendingRemoteId] = useState<string | null>(null)
  const [isStartingOAuth, setIsStartingOAuth] = useState<GitProvider | null>(null)
  const [notice, setNotice] = useState<Notice>(null)

  const loadInventory = useCallback(
    (selectedProvider: GitProvider) => {
      if (!token || !selectedOrganizationId) {
        return
      }
      setIsLoadingInventory(true)
      setNotice(null)
      void getRemoteRepositories(token, selectedOrganizationId, selectedProvider)
        .then((page) => {
          setRemoteRepositories(page.items)
        })
        .catch((error: unknown) => {
          setRemoteRepositories([])
          setNotice({
            kind: 'error',
            text:
              error instanceof ApiError && error.status === 409
                ? t('modal.noCredential', { provider: selectedProvider })
                : t('modal.unsupportedProvider'),
          })
        })
        .finally(() => {
          setIsLoadingInventory(false)
        })
    },
    [selectedOrganizationId, t, token],
  )

  useEffect(() => {
    if (!isOpen) {
      return
    }

    // La carga inicial se aplaza a la siguiente tarea para no escribir estado de
    // forma síncrona durante el montaje del diálogo.
    const timer = window.setTimeout(() => loadInventory(provider), 0)
    return () => window.clearTimeout(timer)
  }, [isOpen, loadInventory, provider])

  useEffect(() => {
    if (!isOpen) {
      return
    }

    // El foco entra en el diálogo y queda atrapado dentro mientras está abierto,
    // para que la navegación por teclado no se escape al fondo (WCAG 2.4.3).
    const previouslyFocused = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    const focusable = dialog?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
    )
    focusable?.[0]?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab' || !dialog || !focusable || focusable.length === 0) {
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
  }, [isOpen, onClose])

  const startOAuth = async (oauthProvider: GitProvider) => {
    if (!token || !selectedOrganizationId) {
      return
    }
    setIsStartingOAuth(oauthProvider)
    setNotice(null)
    try {
      const response = await getOAuthAuthorizationUrl(
        token,
        selectedOrganizationId,
        oauthProvider,
      )
      window.location.assign(response.authorization_url)
    } catch {
      setIsStartingOAuth(null)
      setNotice({ kind: 'error', text: t('connected.missing', { provider: oauthProvider }) })
    }
  }

  const importRepository = async (remote: RemoteRepository) => {
    if (!token || !selectedOrganizationId) {
      return
    }
    setPendingRemoteId(remote.remote_repo_id)
    setNotice(null)
    try {
      const result = await connectRepository(token, selectedOrganizationId, {
        provider,
        remote_repo_id: remote.remote_repo_id,
        pr_reviews_enabled: true,
      })
      onConnected()
      setNotice(
        result.webhook_registered
          ? { kind: 'success', text: t('modal.imported') }
          : { kind: 'warning', text: t('modal.webhookMissing') },
      )
      loadInventory(provider)
    } catch {
      setNotice({ kind: 'error', text: t('modal.importFailed') })
    } finally {
      setPendingRemoteId(null)
    }
  }

  if (!isOpen) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="connect-repository-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow">{t('eyebrow')}</p>
            <h2 id="connect-repository-title">{t('modal.title')}</h2>
            <p className="page-description">{t('modal.description')}</p>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label={t('modal.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <section className="modal-section">
          <h3 className="modal-section-title">{t('modal.oauthTitle')}</h3>
          <p className="modal-section-caption">{t('modal.oauthDescription')}</p>
          <div className="oauth-grid">
            {PROVIDER_OPTIONS.map(({ provider: oauthProvider, labelKey }) => (
              <button
                key={oauthProvider}
                className="oauth-button"
                type="button"
                disabled={isStartingOAuth !== null}
                onClick={() => void startOAuth(oauthProvider)}
              >
                {isStartingOAuth === oauthProvider ? (
                  <LoaderCircle size={18} className="spin" aria-hidden="true" />
                ) : (
                  <Link2 size={18} aria-hidden="true" />
                )}
                <span>{t(labelKey)}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="modal-section">
          <div className="modal-section-header">
            <div>
              <h3 className="modal-section-title">{t('modal.inventoryTitle')}</h3>
              <p className="modal-section-caption">{t('modal.inventoryDescription')}</p>
            </div>
            <button
              className="secondary-button"
              type="button"
              onClick={() => loadInventory(provider)}
              disabled={isLoadingInventory}
            >
              {isLoadingInventory ? (
                <LoaderCircle size={16} className="spin" aria-hidden="true" />
              ) : (
                <RefreshCw size={16} aria-hidden="true" />
              )}
              <span>{t('modal.reload')}</span>
            </button>
          </div>

          <div className="provider-switch" role="group" aria-label={t('modal.selectProvider')}>
            {PROVIDER_OPTIONS.map((option) => (
              <button
                key={option.provider}
                className={
                  option.provider === provider
                    ? 'provider-option provider-option-active'
                    : 'provider-option'
                }
                type="button"
                aria-pressed={option.provider === provider}
                onClick={() => setProvider(option.provider)}
              >
                <span className="mono">{providerName(option.provider)}</span>
              </button>
            ))}
          </div>

          {notice ? (
            <p className={`modal-notice modal-notice-${notice.kind}`} role="status">
              {notice.text}
            </p>
          ) : null}

          {isLoadingInventory ? (
            <p className="modal-empty">{t('states.loading')}</p>
          ) : remoteRepositories.length === 0 ? (
            <p className="modal-empty">{t('modal.empty')}</p>
          ) : (
            <ul className="remote-list">
              {remoteRepositories.map((remote) => (
                <li key={remote.remote_repo_id} className="remote-item">
                  <div className="remote-copy">
                    <strong>{remote.full_name}</strong>
                    <span className="remote-meta">
                      {t('modal.defaultBranch')}:{' '}
                      <code className="mono">{remote.default_branch}</code>
                      {' · '}
                      {remote.is_private ? t('modal.private') : t('modal.public')}
                    </span>
                  </div>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={
                      remote.already_connected || pendingRemoteId === remote.remote_repo_id
                    }
                    onClick={() => void importRepository(remote)}
                  >
                    {pendingRemoteId === remote.remote_repo_id ? (
                      <LoaderCircle size={16} className="spin" aria-hidden="true" />
                    ) : null}
                    <span>
                      {remote.already_connected ? t('modal.alreadyConnected') : t('modal.import')}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}
