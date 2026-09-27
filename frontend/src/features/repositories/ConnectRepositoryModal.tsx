import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { KeyRound, Link2, LoaderCircle, RefreshCw, Search, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  ApiError,
  connectPersonalToken,
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

/**
 * Vías de conexión, en el orden en que se ofrecen.
 *
 * OAuth va primero porque es el camino de producción: no exige que el usuario tenga
 * que fabricar un token. El PAT va después y no como alternativa escondida, porque sin
 * una OAuth App registrada en GitHub y en GitLab no hay forma de probar la
 * sincronización en local, y eso convierte un trámite externo en un bloqueo.
 */
type ConnectMethod = 'oauth' | 'token'

function providerName(provider: GitProvider): string {
  return provider === 'GITHUB' ? 'GitHub' : 'GitLab'
}

type Notice = { kind: 'success' | 'warning' | 'error'; text: string } | null

export interface ConnectRepositoryModalProps {
  isOpen: boolean
  onClose: () => void
  onConnected: () => void
  /** Resultado de la importacion multiple. Lo muestra la pagina, no este modal. */
  onBulkImported: (resultado: { imported: number; failed: string[] }) => void
}

export function ConnectRepositoryModal({
  isOpen,
  onClose,
  onConnected,
  onBulkImported,
}: ConnectRepositoryModalProps) {
  const { t } = useTranslation('repositories')
  const { token, selectedOrganizationId } = useAuth()
  const dialogRef = useRef<HTMLDivElement>(null)
  const [provider, setProvider] = useState<GitProvider>('GITHUB')
  const [remoteRepositories, setRemoteRepositories] = useState<RemoteRepository[]>([])
  const [isLoadingInventory, setIsLoadingInventory] = useState(false)
  const [pendingRemoteId, setPendingRemoteId] = useState<string | null>(null)
  const [isStartingOAuth, setIsStartingOAuth] = useState<GitProvider | null>(null)
  const [connectMethod, setConnectMethod] = useState<ConnectMethod>('oauth')
  const [personalToken, setPersonalToken] = useState('')
  const [credentialName, setCredentialName] = useState('')
  const [isSavingToken, setIsSavingToken] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [busqueda, setBusqueda] = useState('')
  const [seleccionados, setSeleccionados] = useState<Set<string>>(() => new Set())
  const [isImporting, setIsImporting] = useState(false)

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

  const savePersonalToken = async () => {
    if (!token || !selectedOrganizationId) {
      return
    }
    const trimmed = personalToken.trim()
    if (trimmed.length < 20) {
      // Se comprueba aquí y no solo en el backend para no gastar una llamada con un
      // campo vacío. El backend vuelve a validarlo: esta es cortesía, no la garantía.
      setNotice({ kind: 'error', text: t('modal.patFailed') })
      return
    }
    setIsSavingToken(true)
    setNotice(null)
    try {
      const result = await connectPersonalToken(token, selectedOrganizationId, {
        provider,
        token: trimmed,
        name: credentialName.trim() || providerName(provider),
      })
      // El secreto se borra de memoria en cuanto el backend responde. Dejarlo en el
      // campo haría que siga en el DOM, en el historial de autocompletado del
      // navegador y en cualquier captura del modal mientras el usuario eligiese repositorio.
      setPersonalToken('')
      setConnectMethod('oauth')
      setNotice({
        kind: 'success',
        text: result.replaced_existing
          ? t('modal.patReplaced', { login: result.account_login })
          : t('modal.patConnected', { login: result.account_login }),
      })
      // El paso 2 se recarga con la credencial nueva para que el listado real aparezca
      // sin que el usuario tenga que pedirlo: conectar es el gesto, no recargar después.
      loadInventory(provider)
    } catch {
      setNotice({ kind: 'error', text: t('modal.patFailed') })
    } finally {
      setIsSavingToken(false)
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

  /**
   * Los repositorios que casan con el buscador.
   *
   * El filtro es **local** y no una peticion: el inventario ya esta descargado entero y
   * filtrarlo aqui da resultados al instante. Pedirlo al servidor en cada tecla seria una
   * ida y vuelta por caracter, y con ellista de cientos de repositorios se notaria.
   *
   * Se comparan nombre, ruta y rama, para que buscar `api` encuentre tambien
   * `acme/api-gateway` aunque el usuario haya escrito otra cosa. La busqueda ignora mayusculas
   * porque el nombre de un repositorio no es un dato con mayusculas que importen.
   */
  const reposVisibles = useMemo(() => {
    const consulta = busqueda.trim().toLowerCase()
    if (consulta === '') {
      return remoteRepositories
    }
    return remoteRepositories.filter((remote) =>
      [remote.full_name, remote.name, remote.default_branch]
        .join(' ')
        .toLowerCase()
        .includes(consulta),
    )
  }, [remoteRepositories, busqueda])

  /**
   * Los importables de lo que se ve ahora mismo.
   *
   * Se recalcula sobre `reposVisibles` y no sobre el inventario entero, a proposito: si el
   * buscador oculta repositorios ya conectados, "seleccionar todos" debe ofrecer los que se
   * ven, no los que no. Marcar de golpe algo que el usuario no ve para luego tener que
   * desmarcarlo es la forma segura de que termine importando lo que no queria.
   */
  const importablesVisibles = useMemo(
    () => reposVisibles.filter((remote) => !remote.already_connected),
    [reposVisibles],
  )

  const todosSeleccionados =
    importablesVisibles.length > 0 &&
    importablesVisibles.every((remote) => seleccionados.has(remote.remote_repo_id))

  const alternarSeleccion = useCallback((remoteRepoId: string) => {
    setSeleccionados((actual) => {
      const siguiente = new Set(actual)
      if (siguiente.has(remoteRepoId)) {
        siguiente.delete(remoteRepoId)
      } else {
        siguiente.add(remoteRepoId)
      }
      return siguiente
    })
  }, [])

  const alternarTodos = useCallback(() => {
    setSeleccionados((actual) => {
      const siguiente = new Set(actual)
      if (todosSeleccionados) {
        for (const remote of importablesVisibles) {
          siguiente.delete(remote.remote_repo_id)
        }
      } else {
        for (const remote of importablesVisibles) {
          siguiente.add(remote.remote_repo_id)
        }
      }
      return siguiente
    })
  }, [todosSeleccionados, importablesVisibles])

  /**
   * Importa la seleccion y cierra.
   *
   * ## Por qué el modal se cierra **antes** de que terminen las peticiones
   *
   * Porque cada importacion es una peticion independiente y puede tardar varios segundos.
   * Mantener el modal abierto con un boton en «importando 3 de 7» obliga al usuario a
   * esperar a que termine para poder seguir trabajando, y si una de las peticiones falla se
   * queda mirando un modal que ya no le sirve de nada.
   *
   * Cerrar primero y avisar despues invierte el orden a favor de quien esta usando el panel:
   * ve la lista actualizandose mientras se importa. El aviso lleva el numero de fallos
   * reales, no un «algo ha ido mal», porque el resultado que importa es cuantos quedaron
   * fuera.
   */
  const importarSeleccionados = useCallback(async () => {
    if (!token || !selectedOrganizationId) {
      return
    }
    const objetivos = remoteRepositories.filter(
      (remote) => seleccionados.has(remote.remote_repo_id) && !remote.already_connected,
    )
    if (objetivos.length === 0) {
      return
    }
    setIsImporting(true)
    const fallidos: string[] = []
    let ok = 0
    /*
      Secuencial y no en paralelo, a proposito.
      *
      * Cada `connectRepository` da de alta un webhook en el proveedor. Lanzar veinte a la vez
      * contra la misma credencial es justo el patron que hace que el proveedor devuelva `403`
      * por tasa de peticiones, y entonces el usuario ve quince repositorios importados y cinco
      * fallidos sin motivo aparente. En serie tarda mas y es fiable, que es lo que hace falta
      * en una operacion que crea credenciales.
      */
    for (const remote of objetivos) {
      try {
        await connectRepository(token, selectedOrganizationId, {
          provider,
          remote_repo_id: remote.remote_repo_id,
          pr_reviews_enabled: true,
        })
        ok += 1
      } catch {
        fallidos.push(remote.full_name)
      }
    }
    setIsImporting(false)
    /*
      El aviso lo pone la pagina y no este modal. En cuanto se cierra, el componente deja de
      * existir y un `setState` aqui no lo veria nadie; por eso el resultado se devuelve por
      * parametro en vez de pintarse dentro. El orden importa: primero el aviso, despues el
      * refresco de la lista, y el cierre al final.
    */
    onBulkImported({ imported: ok, failed: fallidos })
    onConnected()
    onClose()
  }, [
    token,
    selectedOrganizationId,
    remoteRepositories,
    seleccionados,
    provider,
    onBulkImported,
    onConnected,
    onClose,
  ])


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

          {/*
            Selector de vía. Se usa `role="group"` con `aria-pressed` en lugar de un
            `<select>` porque son dos opciones excluyentes con texto explicativo
            propio, y un desplegable escondería precisamente la nota que dice que OAuth
            necesita una app registrada.
          */}
          <div className="provider-switch" role="group" aria-label={t('modal.patMethod')}>
            {(['oauth', 'token'] as const).map((method) => (
              <button
                key={method}
                className={
                  method === connectMethod
                    ? 'provider-option provider-option-active'
                    : 'provider-option'
                }
                type="button"
                aria-pressed={method === connectMethod}
                onClick={() => setConnectMethod(method)}
              >
                <span>
                  {t(
                    method === 'oauth' ? 'modal.patMethodOauth' : 'modal.patMethodToken',
                  )}
                </span>
              </button>
            ))}
          </div>
          <p className="modal-section-caption">
            {t(
              connectMethod === 'oauth'
                ? 'modal.patMethodOauthHint'
                : 'modal.patMethodTokenHint',
            )}
          </p>

          {connectMethod === 'oauth' ? (
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
          ) : (
            <form
              className="pat-form"
              onSubmit={(event) => {
                event.preventDefault()
                void savePersonalToken()
              }}
            >
              <p className="modal-section-caption">{t('modal.patDescription')}</p>
              <div className="form-field">
                {/*
                  `type="password"` con el texto oculto: es un secreto y su valor acaba en
                  el DOM de todas formas. `autoComplete="off"` evita que un gestor de
                  contraseñas lo sugiera en un sitio donde no tiene nada que ver, que es
                  como un token acaba guardado en el gestor equivocado.
                */}
                <label htmlFor="pat-token">{t('modal.patLabel')}</label>
                <input
                  id="pat-token"
                  name="pat-token"
                  type="password"
                  value={personalToken}
                  placeholder={t('modal.patPlaceholder')}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => setPersonalToken(event.target.value)}
                />
              </div>
              <div className="form-field">
                <label htmlFor="pat-name">{t('modal.patName')}</label>
                <input
                  id="pat-name"
                  name="pat-name"
                  type="text"
                  value={credentialName}
                  placeholder={t('modal.patNamePlaceholder')}
                  onChange={(event) => setCredentialName(event.target.value)}
                />
              </div>
              <div className="inventory-controls">
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
                <button
                  className="primary-button"
                  type="submit"
                  disabled={isSavingToken}
                >
                  {isSavingToken ? (
                    <LoaderCircle size={16} className="spin" aria-hidden="true" />
                  ) : (
                    <KeyRound size={16} aria-hidden="true" />
                  )}
                  <span>
                    {t(isSavingToken ? 'modal.patSubmitting' : 'modal.patSubmit')}
                  </span>
                </button>
              </div>
            </form>
          )}
        </section>

        <section className="modal-section">
          <div>
            <h3 className="modal-section-title">{t('modal.inventoryTitle')}</h3>
            <p className="modal-section-caption">{t('modal.inventoryDescription')}</p>
          </div>

          {/*
            Pestañas de proveedor y recarga en una sola fila. Se usan juntas —cambiar de
            GitHub a GitLab obliga a recargar la lista—, y separadas obligaban a saltar
            la vista de un control al otro para ejecutar un gesto único.
          */}
          <div className="inventory-controls">
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
            <>
              <div className="remote-toolbar">
                <div className="remote-search">
                  <Search size={15} aria-hidden="true" />
                  {/*
                    El icono va dentro del recuadro y no al lado, con `pointer-events: none` en
                    CSS para que el clic le llegue al input. Puesto al lado, el hueco entre
                    icono y campo deja una zona muerta de unos pixeles donde el usuario hace
                    clic esperando escribir.
                  */}
                  <input
                    type="search"
                    className="remote-search-input"
                    value={busqueda}
                    placeholder={t('modal.searchPlaceholder')}
                    aria-label={t('modal.searchLabel')}
                    onChange={(event) => setBusqueda(event.target.value)}
                  />
                </div>
                <label className="remote-selectall">
                  <input
                    type="checkbox"
                    checked={todosSeleccionados}
                    disabled={importablesVisibles.length === 0}
                    onChange={alternarTodos}
                  />
                  <span>{t('modal.selectAll')}</span>
                </label>
              </div>

              {reposVisibles.length === 0 ? (
                <p className="modal-empty">{t('modal.searchNoResults')}</p>
              ) : (
                <ul className="remote-list">
                  {reposVisibles.map((remote) => (
                    <li key={remote.remote_repo_id} className="remote-item">
                      <label className="remote-select">
                        <input
                          type="checkbox"
                          checked={seleccionados.has(remote.remote_repo_id)}
                          disabled={remote.already_connected}
                          onChange={() => alternarSeleccion(remote.remote_repo_id)}
                        />
                      </label>
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
                          {remote.already_connected
                            ? t('modal.alreadyConnected')
                            : t('modal.import')}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              {/*
                El boton de importacion multiple es el cierre del modal, no un boton mas del
                formulario. Se ancla al final de la seccion y no sube con la lista, para que
                seguir funcionando cuando hay cien repositorios que recorrer.
              */}
              <div className="remote-bulk">
                <button
                  className="primary-button"
                  type="button"
                  disabled={seleccionados.size === 0 || isImporting}
                  onClick={() => void importarSeleccionados()}
                >
                  {isImporting ? (
                    <LoaderCircle size={16} className="spin" aria-hidden="true" />
                  ) : null}
                  <span>
                    {isImporting
                      ? t('modal.importingSelected')
                      : t('modal.importSelected', { count: seleccionados.size })}
                  </span>
                </button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
