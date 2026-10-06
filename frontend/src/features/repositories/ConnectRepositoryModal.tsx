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
import { claveDeErrorDeInventario } from './inventarioErrores'

type ProviderOption = { provider: GitProvider; labelKey: string }

const PROVIDER_OPTIONS: ProviderOption[] = [
  { provider: 'GITHUB', labelKey: 'modal.connectGitHub' },
  { provider: 'GITLAB', labelKey: 'modal.connectGitLab' },
]

/**
 * Cuántos repositorios se piden en cada página del inventario.
 *
 * ## Por qué 100 y no 500
 *
 * Porque `GET /repositories/remote` tiene `limit` topado a 100 en el backend, así que 100 es el
 * máximo que una sola petición puede traer. Y el tope del backend no es arbitrario: esa ruta
 * llama a `list_repositories` del proveedor, que devuelve **el inventario entero del cliente**
 * en cada petición. Pedir 500 no abarataría la llamada, solo construiría 500 objetos en el
 * navegador para pintar 50. El límite protege al navegador, no al servidor.
 *
 * ## Por qué esto **no** arregla el problema del cliente con 500 repositorios
 *
 * Y no lo arregla, y esa es la decisión que hay que tomar aquí. Con 500 repositorios, 100 por
 * página son cinco páginas, y una barra de paginación en un **modal** es mala interfaz: obliga
 * a cerrar el flujo de conexión para recorrer un inventario, y la conexión es lo que el usuario
 * vino a hacer. La alternativa —cargar más al llegar al final, como un scroll infinito— mete
 * cinco peticiones en una ruta con `repository_management_rate_limit`, que son 60 por minuto.
 *
 * Lo que sí se hace es que el **`total` sea honesto y la búsqueda llegue a todo**. Con eso, un
 * cliente con 500 repositorios no los ve todos en la lista, pero **sí** llega a cualquiera de
 * ellos escribiendo su nombre, y la pantalla le dice cuántas páginas hay. Ese es el compromiso:
 * el buscador es la vía para llegar a algo concreto, y la lista es para elegir entre lo que ya
 * sabes que hay. Lo contrario —una lista con 500 filas y paginación— es una tabla, y no es lo
 * que este modal es.
 */
const INVENTARIO_POR_PAGINA = 100

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
  /**
   * `true` cuando la **última** carga del inventario terminó en fallo.
   *
   * ## Por qué hace falta y no se deduce de `remoteRepositories`
   *
   * Porque `remoteRepositories` está a `[]` en los dos casos —no hay repositorios, o no se ha
   * podido preguntar— y el modal pintaba el mismo texto para los dos: «No hay repositorios
   * disponibles con esta credencial». Con la carga fallida, esa frase es una afirmación que el
   * panel no puede sostener: no es que no haya repositorios, es que no se ha enterado. Y puesta
   * al lado del aviso de error, las dos frases juntas son un diagnóstico que no lleva a ninguna
   * parte, que es justo el defecto que este estado viene a quitar.
   *
   * Con el valor a `false`, la lista vacía **sí** significa lista vacía y el texto vale.
   */
  const [inventarioFallido, setInventarioFallido] = useState(false)
  const [pendingRemoteId, setPendingRemoteId] = useState<string | null>(null)
  const [isStartingOAuth, setIsStartingOAuth] = useState<GitProvider | null>(null)
  const [connectMethod, setConnectMethod] = useState<ConnectMethod>('oauth')
  const [personalToken, setPersonalToken] = useState('')
  const [credentialName, setCredentialName] = useState('')
  const [isSavingToken, setIsSavingToken] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [busqueda, setBusqueda] = useState('')
  /**
   * El total **del servidor**, y no el tamaño de la lista.
   *
   * ## Por qué hace falta
   *
   * Porque sin él la pantalla no puede distinguir tres cosas que el usuario necesita separar:
   * «no hay más de estos» de «hay más pero no las has visto». Con 500 repositorios y una ventana
   * de 100, la lista se ve llena y no hay nada en pantalla que diga que faltan 400. El número
   * que las cuenta es el que decide si el buscador funciona.
   */
  const [totalInventario, setTotalInventario] = useState(0)
  /**
   * `true` cuando se ha escrito una búsqueda y el servidor **no** la ha aplicado.
   *
   * ## Por qué hay que mirar esto y no fiarse de la respuesta
   *
   * ## Por qué existe este aviso
   *
   * Porque un parámetro de consulta que el servidor no declara se ignora sin decir nada: ni
   * `422`, ni aviso, ni diferencia en el código de estado. Con un backend desactualizado, escribir
   * `shytai` devuelve el inventario entero y el panel dice «100 de 100 repositorios coinciden con
   * tu búsqueda». Los nombres que salen no la contienen, y no hay nada en la pantalla que lo
   * diga: el buscador parece roto, cuando lo que está desactualizado es el servidor.
   *
   * ## Por qué no se arregla haciendo la búsqueda en el cliente
   *
   * ## Por qué no se disimula filtrando también en el cliente
   *
   * Porque si el cliente filtrara también, el aviso no se pondría nunca y el servidor
   * desactualizado seguiría sirviendo inventarios enteros a quien|Programa la búsqueda solo en el cliente
   */
  const [filtroIgnorado, setFiltroIgnorado] = useState(false)
  const [seleccionados, setSeleccionados] = useState<Set<string>>(() => new Set())
  const [isImporting, setIsImporting] = useState(false)

  const loadInventory = useCallback(
    (selectedProvider: GitProvider, search: string) => {
      if (!token || !selectedOrganizationId) {
        return
      }
      setIsLoadingInventory(true)
      setNotice(null)
      void getRemoteRepositories(token, selectedOrganizationId, selectedProvider, {
        search,
        limit: INVENTARIO_POR_PAGINA,
        offset: 0,
      })
        .then((page) => {
          setRemoteRepositories(page.items)
          setTotalInventario(page.total)
          setInventarioFallido(false)
          // Se compara lo pedido con lo aplicado. Un backend viejo no trae el campo y
          // `busqueda` vacía lo cuenta como «no filtró», que es la lectura conservadora: ante
          // la duda, se avisa de más y no de menos.
          setFiltroIgnorado(
            search.trim() !== '' && (page.busqueda_aplicada ?? '') !== search.trim(),
          )
          // La búsqueda cambia lo que hay en pantalla, y lo que se había marcado deja de estar a
          // la vista. Sin esta línea, marcar dos repositorios, cambiar el buscador e importar
          // importa cuatro de los que ya no se ven.
          setSeleccionados(new Set())
        })
        .catch((error: unknown) => {
          /*
            El motivo lo decide el **código de estado**, no el tipo de excepción.
            *
            Antes esta rama era «si es `409`, credencial; en cualquier otro caso, no hay
            * conector». Eso convertía un `502` —que aquí es «el proveedor rechazó tu
            * credencial»— en «GitHub no tiene conector», que es lo que se vio en pantalla con un
            * token caducado: la ventana decía que no había repositorios de GitHub con la lista de
            * al lado que sí los tenía. La tabla de `inventarioErrores.ts` pone cada caso en su
            * sitio.
            *
            Y el nombre del proveedor va como lo ve el usuario («GitHub»), no como la constante
            del enum («GITHUB»): un identificador interno en un mensaje de una acción que el
            usuario tiene que hacer no le dice nada.
            */
          setRemoteRepositories([])
          setInventarioFallido(true)
          const estado = error instanceof ApiError ? error.status : null
          const nombre = providerName(selectedProvider)
          setNotice({
            kind: 'error',
            text: t(claveDeErrorDeInventario(estado), { provider: nombre }),
          })
        })
        .finally(() => {
          setIsLoadingInventory(false)
        })
    },
    [selectedOrganizationId, t, token],
  )

  /**
   * Recarga el inventario cuando cambia la búsqueda, con retardo.
   *
   * ## Por qué un retardo y no una petición por tecla
   *
   * ## Por qué 200 ms
   *
   * Porque la ruta lleva `repository_management_rate_limit`, que son 60 peticiones por minuto.
   * Buscar `microservicios` son 13 teclas, y sin retardo eso son 13 peticiones y se llega al
   * límite escribiendo dos palabras. El retardo no es para que se vea bonito: es para que haya
   * una petición por palabra y no una por letra.
   *
   * ## Por qué no menos de 200
   *
   * ## Por qué 200 y no más
   *
   * Porque a partir de unos 200 ms la espera empieza a notarse y el buscador parece lento; por
   * debajo, cada tecla lanza una petición que la siguiente cancela. La diferencia entre 200 y 300
   * no se nota, y 200 es la cifra más corta que aguanta el límite de tasa con holgura: escribir
   * doce letras seguidas da cuatro peticiones, no doce.
   *
   * Y **escribir nunca se bloquea**: el retardo retrasa la petición, no el tecleo. El campo
   * responde mientras tanto y la lista se actualiza al terminar.
   *
   * ## Por qué se cancela la anterior
   *
   * Porque sin cancelación, dos búsquedas seguidas pueden llegar al servidor en orden
   * inverso: la lenta de `sh` termina después de la rápida de `shy` y pisa el resultado con el
   * más viejo. Con el `clearTimeout` la anterior no llega a salir, y el `isActive` del efecto se
   * encarga del caso en el que ya había salido.
   */
  useEffect(() => {
    if (!isOpen || !token || !selectedOrganizationId) {
      return
    }
    const temporizador = window.setTimeout(() => {
      loadInventory(provider, busqueda)
    }, busqueda.trim() === '' ? 0 : 300)
    return () => window.clearTimeout(temporizador)
  }, [busqueda, isOpen, loadInventory, provider, selectedOrganizationId, token])

  useEffect(() => {
    if (!isOpen) {
      return
    }

    // La carga inicial se aplaza a la siguiente tarea para no escribir estado de
    // forma síncrona durante el montaje del diálogo.
    //
    // Y carga el inventario **entero**, no lo que hubiera en el buscador. Es deliberado que
    // `busqueda` no sea una dependencia: abrir el modal es empezar de cero, y arrastrar el texto
    // de la última búsqueda dejaría al usuario en una lista filtrada que no recuerda haber
    // pedido. Si se abriera con algo escrito, el botón «Recargar» —que sí usa `busqueda`— y
    // esta carga discreparían, y la lista cambiaría sola después de abrirse.
    const timer = window.setTimeout(() => {
      setBusqueda('')
      loadInventory(provider, '')
    }, 0)
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
      loadInventory(provider, busqueda)
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
      loadInventory(provider, busqueda)
    } catch {
      setNotice({ kind: 'error', text: t('modal.importFailed') })
    } finally {
      setPendingRemoteId(null)
    }
  }

  /**
   * Lo que se ve es lo que el servidor ha devuelto, sin filtrar otra vez aquí.
   *
   * ## Por qué se quita el filtro local
   *
   * ## Por qué el filtro del cliente estaba, y por qué ya no
   *
   * Porque estaba por una razón que era cierta cuando se escribió y dejó de serlo: el
   * comentario de arriba decía «el inventario ya está descargado entero». No lo está —la API
   * topa el `limit` a 100— y por eso buscar `shy` en un workspace de 500 enseñaba un resultado
   * de los doce que hay, sin ninguna pista de que faltaban once.
   *
   * ## Por qué no se deja como filtro **extra** encima del del servidor
   *
   * Porque es meter un conjunto ya estrecho por un criterio distinto, y si los dos no
   * coinciden exactamente aparece el peor caso: la lista se vacía sin motivo aparente y no hay
   * forma de saber si no hay resultados o si los dos filtros se están contradiciendo. Con el
   * filtro en un solo sitio, `total` y la lista cuentan lo mismo, que es lo que hace falta para
   * que el número de arriba sirva de algo.
   */
  const reposVisibles = remoteRepositories

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
              onClick={() => loadInventory(provider, busqueda)}
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

          {/*
            `role="alert"` y no `status`. El aviso de error es lo único que hay que leer cuando
            falla la carga, y `status` es una región live de cortesía: se anuncia, pero no
            interrumpe. `alert` sí interrumpe, que es lo que corresponde a un fallo que acaba de
            aparecer sin que nadie lo haya pedido.
          */}
          {notice ? (
            <p
              className={`modal-notice modal-notice-${notice.kind}`}
              role={notice.kind === 'error' ? 'alert' : 'status'}
            >
              {notice.text}
            </p>
          ) : null}

          {/*
            La barra con el buscador va **siempre**, incluso con la lista vacía.

            ## Por qué esto estaba dentro de la rama de la lista

            Porque la estructura era «si hay repositorios, pinta la barra y la lista; si no, pinta
            un texto». Con una búsqueda que no encuentra nada, `remoteRepositories` pasa a `[]`,
            se entra en la rama del texto, **y el buscador desaparece con ella**.

            ## Por qué eso es un fallo grave de flujo y no una molestia

            Es el peor tipo de defecto de UI que hay. El usuario escribe una letra, no hay
            coincidencias, y **el control con el que corregir lo que escribió ya no está**. No
            puede borrar la letra, porque el campo desapareció con ella; no puede escribir otra,
            porque no hay donde; no puede buscar nada, porque no hay campo. La única salida es
            **cerrar el modal y volver a abrirlo**, y si el modal recuerda la búsqueda —que la
            recuerda, porque está en el estado del padre—, la reopen sale igual y el usuario se
            queda sin salida del todo.

            Un campo de entrada no desaparece nunca. Como mucho dice «no hay coincidencias». Es
            el mismo criterio que en el resto del panel y por el mismo motivo: un control que
            puede desaparecer es un control que puede dejar al usuario atrapado.

            Por eso la condición de la barra es **solo** «no estamos cargando y la carga no ha
            fallado». La de la lista es la de siempre, y las dos están separadas.
          */}
          {!isLoadingInventory && !inventarioFallido ? (
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
          ) : null}

          {isLoadingInventory ? (
            <p className="modal-empty">{t('states.loading')}</p>
          ) : /*
            El texto de lista vacía solo se pinta si la carga **terminó bien**. Con la carga
            fallida ya hay un aviso arriba que dice por qué, y añadir debajo «no hay
            repositorios» convierte un fallo en una afirmación que el panel no puede hacer: no
            sabemos cuántos hay, sabemos que no lo hemos preguntado.
          */ inventarioFallido ? null : remoteRepositories.length === 0 ? (
            <p className="modal-empty">
              {/*
                Dos textos y no uno, porque las dos situaciones piden acciones distintas.
                «No hay repositorios» con la credencial conectada se resuelve reconectando;
                «Nada coincide con la búsqueda» se resuelve **borrando la búsqueda**, y ese es
                justo el gesto que el campo de arriba sigue ahí para poder hacer.

                Con un solo texto, el caso de la búsqueda —que es el frecuente— salía con un
                mensaje que no menciona la búsqueda, y el usuario leía «mi credencial está
                rota» cuando lo que había escrito era una letra de más.
              */}
              {busqueda.trim() === '' ? t('modal.empty') : t('modal.searchNoResults', {
                search: busqueda.trim(),
              })}
            </p>
          ) : (
            <>
              {/* El resto del bloque: avisos, recuento y lista. */}
              {filtroIgnorado && (
                <p className="remote-count remote-count-warning" role="alert">
                  {t('modal.searchNotApplied', { search: busqueda })}
                </p>
              )}

              {/*
                El recuento va **siempre**, no solo cuando hay búsqueda, y no es decoración.
                Es lo que convierte «veo una lista llena» en «he visto 100 de 500». Sin él, con
                el limit del servidor, la pantalla no dice en ningún sitio que queden 400
                repositorios sin mirar: es exactamente el fallo que se reportó al buscar `shy`.
              */}
              {!isLoadingInventory && reposVisibles.length > 0 && (
                <>
                  <p className="remote-count" role="status">
                    {busqueda.trim() === ''
                      ? t('modal.countAll', {
                          shown: reposVisibles.length,
                          total: totalInventario,
                        })
                      : t('modal.countFiltered', {
                          shown: reposVisibles.length,
                          total: totalInventario,
                        })}
                  </p>
                  {/*
                    El aviso de «no lo ves todo» sale **solo** sin búsqueda.

                    Es el otro lado del compromiso del tope, y solo tiene sentido en un caso:
                    con quinientos repositorios y una ventana de cien, la lista se ve llena y el
                    recuento dice «100 de 500», pero «500» es el número de lo que **hay**, no el
                    de lo que se puede elegir, y el usuario puede recorrer la lista entera
                    creyendo que la ha visto.

                    Con una búsqueda activa no sale, porque ahí el `total` ya es el de lo que
                    coincide y la búsqueda llega a todo el inventario: no falta nada, y un aviso
                    de «puede que falten» al lado de una lista ya filtrada sería miedo sin
                    motivo. Por eso la condición incluye `busqueda.trim() === ''`.
                  */}
                  {busqueda.trim() === '' && totalInventario > reposVisibles.length ? (
                    <p className="remote-count remote-count-warning" role="status">
                      {t('modal.truncated', {
                        missing: totalInventario - reposVisibles.length,
                      })}
                    </p>
                  ) : null}
                </>
              )}

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
