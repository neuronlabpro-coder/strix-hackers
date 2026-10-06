import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleHelp, Package,
  RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  getRepositories,
  getSupplyChainPackages,
  getSupplyChainSummary,
  syncSupplyChainRepository,
  type SupplyChainQuery,
} from '../../lib/api'
import type {
  Ecosystem,
  Repository,
  SupplyChainPackage,
  SupplyChainSummary,
  SupplyChainSyncResult,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'

/**
 * Inventario de dependencias de los repositorios del workspace.
 *
 * ## Por qué el estado de vulnerabilidad tiene **tres** valores y no dos
 *
 * Porque `has_vulnerabilities` es `boolean | null` en el backend, y los tres significan cosas
 * distintas: `true` es "comprobado y vulnerable", `false` es "comprobado y limpio", y **`null`
 * es "nadie lo ha comprobado"**.
 *
 * El proyecto no tiene hoy una fuente de vulnerabilidades por paquete, así que **todo** lo
 * indexado está en `null`. Un panel con dos estados dibujaría un verde sobre cada fila, y un
 * usuario que ve cincuenta verdes entiende que sus dependencias están revisadas. No lo están, y
 * esa es la diferencia entre una herramienta que informa y una que tranquiliza.
 *
 * Por eso el tercero se ve como lo que es —"sin comprobar"—, y por eso el número de filas sin
 * comprobar va en la cabecera antes que el de limpias.
 */

/** Los siete valores del enum, en el orden en que se pintan. */
const ECOSYSTEMS: Ecosystem[] = ['NPM', 'PYPI', 'GO', 'CARGO', 'MAVEN', 'COMPOSER', 'OTHER']

/** ecosystem → clase de badge. El color es por tecnologia, no por gravedad. */
const BADGE_DE_ECOSYSTEM: Record<Ecosystem, string> = {
  NPM: 'supply-eco-npm',
  PYPI: 'supply-eco-pypi',
  GO: 'supply-eco-go',
  CARGO: 'supply-eco-cargo',
  MAVEN: 'supply-eco-maven',
  COMPOSER: 'supply-eco-composer',
  OTHER: 'supply-eco-other',
}

const PAGE_SIZE = 50

/**
 * Cuántos repositorios se ofrecen como candidatos a sincronizar.
 *
 * ## Por qué es una constante y no la página del inventario
 *
 * Porque el `limit` de `/repositories/` tiene tope de 100 en el backend, y porque una lista de
 * candidatos de un desplegable se acota con el buscador del desplegable, no con paginación. Con
 * cien salen todos los casos reales de un cliente de tamaño mediano, y si un cliente llega a
 * quinientos, el buscador del desplegable es lo que reduce la lista —que es exactamente lo que
 * se hace con cualquier desplegable de doscientas opciones.
 *
 * No se pone un aviso de «100 de 500» porque la lista no es un inventario que el usuario esté
 * recorriendo: es el catálogo de qué se puede sincronizar, y el buscador lo cubre. Un contador
 * de «mostrando 100 de 500» en un desplegable sería ruido que además sugiere que faltan
 * repositorios sincronizables, cuando lo que faltan son de la **paginación de este selector**.
 */
const REPOS_POR_PAGINA = 100

type FiltroVulnerabilidad = 'todos' | 'vulnerable' | 'clean' | 'unchecked'

const FILTRO_A_QUERY: Record<FiltroVulnerabilidad, boolean | null | undefined> = {
  todos: undefined,
  vulnerable: true,
  clean: false,
  unchecked: null,
}

export function SupplyChainPage() {
  const { t } = useTranslation('supplyChain')
  const { token, selectedOrganizationId } = useAuth()
  const [page, setPage] = useState<{
    key: string
    items: SupplyChainPackage[]
    total: number
  } | null>(null)
  const [summary, setSummary] = useState<SupplyChainSummary | null>(null)
  const [search, setSearch] = useState('')
  const [ecosystem, setEcosystem] = useState<Ecosystem | null>(null)
  const [repositoryId, setRepositoryId] = useState<string | null>(null)
  const [filtro, setFiltro] = useState<FiltroVulnerabilidad>('todos')
  const [reloadToken, setReloadToken] = useState(0)
  const [listFailed, setListFailed] = useState(false)
  // El estado de la sincronizacion vive aqui y no en el componente del boton, porque el boton
  // tambien tiene que poder **refrescar** la tabla y el resumen, y eso es estado de la pagina.
  const [isSyncing, setIsSyncing] = useState(false)
  const [lastSync, setLastSync] = useState<SupplyChainSyncResult | null>(null)
  const [syncFailed, setSyncFailed] = useState(false)
  /**
   * Los repositorios conectados, que son los candidatos a sincronizar.
   *
   * ## Por qué se piden aquí y no vienen del resumen
   *
   * Porque el resumen cuenta paquetes y no sabe qué repositorios hay. Y porque **este estado no
   * tenía ninguna fuente**: `repositoryId` solo lo ponía `reiniciar()`, con el valor `null`, de
   * modo que el botón de sincronizar —que sale cuando hay repositorio seleccionado— no se
   * llegaba a ver nunca. El inventario salía vacío siempre y la pantalla lo auditaba como
   * correcto: cero dependencias es una respuesta válida para una organización sin repositorios
   * y para una que nunca le dio al botón.
   */
  const [repositories, setRepositories] = useState<Repository[]>([])

  const isAuthenticated = Boolean(token && selectedOrganizationId)

  const query: SupplyChainQuery = {
    limit: PAGE_SIZE,
    offset: 0,
    ...(ecosystem ? { ecosystem } : {}),
    ...(repositoryId ? { repository_id: repositoryId } : {}),
    ...(search ? { search } : {}),
  }
  const clave = JSON.stringify([token, selectedOrganizationId, query, filtro, reloadToken])

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    const params: SupplyChainQuery = { ...query }
    const valor = FILTRO_A_QUERY[filtro]
    if (valor !== undefined) params.has_vulnerabilities = valor
    void getSupplyChainPackages(token, selectedOrganizationId, params)
      .then((respuesta) => {
        if (!isActive) return
        setPage({ key: clave, items: respuesta.items, total: respuesta.total })
        setListFailed(false)
      })
      .catch(() => {
        if (isActive) setListFailed(true)
      })
    return () => {
      isActive = false
    }
    // `query` se serializa dentro de `clave`, que es lo que compara React; depender del objeto
    // en si haria que cada render del objeto fuera un cambio.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clave, token, selectedOrganizationId, filtro])

  /**
   * Los repositorios se piden una vez al entrar, y **no** se recargan con `reloadToken`.
   *
   * Porque la lista de candidatos cambia cuando alguien conecta un repositorio, que se hace en
   * otra pantalla, y volver a pedirla después de cada sincronización sería una petición por
   * cada botón pulsado para obtener exactamente la misma lista. Es un dato del workspace, no
   * del inventario: el inventario cambia con la sincronización, la lista de repos no.
   */
  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void getRepositories(token, selectedOrganizationId, REPOS_POR_PAGINA)
      .then((pagina) => {
        if (isActive) setRepositories(pagina.items)
      })
      .catch(() => {
        // Un fallo aquí no se muestra: sin lista de repositorios no hay nada que sincronizar,
        // y el inventario que ya se ve sigue siendo cierto. Se registra el vacío y el resto de
        // la pantalla funciona; un error aquí taparía una tabla que sí tiene datos.
        if (isActive) setRepositories([])
      })
    return () => {
      isActive = false
    }
  }, [token, selectedOrganizationId])

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void getSupplyChainSummary(token, selectedOrganizationId)
      .then((datos) => {
        if (isActive) setSummary(datos)
      })
      .catch(() => {
        // El resumen es un extra de la cabecera. Si falla, la tabla sigue siendo util y no
        // merece la pena tapar la pantalla con un error por cuatro numeros.
        if (isActive) setSummary(null)
      })
    return () => {
      isActive = false
    }
  }, [token, selectedOrganizationId, reloadToken])

  const isCurrent = page?.key === clave
  const items = isCurrent ? page.items : []
  const total = isCurrent ? page.total : 0
  const isLoading = isAuthenticated && !isCurrent && !listFailed
  /**
   * Sincroniza el repositorio filtrado y recarga el inventario.
   *
   * ## Por qué recarga aunque la respuesta ya diga cuántos paquetes hay
   *
   * Porque la respuesta cuenta filas **afectadas**, y el listado está paginado, ordenado y
   * filtrado. Si la sincronización actualiza cuarenta paquetes de una página que no es la que el
   * usuario está mirando, el contador de la cabecera cambia y la tabla no, y el usuario ve un
   * panel que dice "40 paquetes" con la misma tabla de antes. Recargar cuesta una petición y
   * quita la duda.
   *
   * ## Por qué el error se guarda y no se lanza
   *
   * Porque el error de una sincronización es información sobre el estado del panel, no una
   * excepción. Se muestra como aviso y el inventario que ya había se queda, que es lo que el
   * usuario quiere: saber que la sincronización falló **y** seguir viendo lo que ya sabía.
   */
  async function sincronizar(): Promise<void> {
    if (isSyncing) return
    const objetivo = repositoryId
    if (!objetivo) return

    setIsSyncing(true)
    setSyncFailed(false)
    try {
      const resultado = await syncSupplyChainRepository(
        token as string,
        selectedOrganizationId as string,
        objetivo,
      )
      setLastSync(resultado)
      setReloadToken((current) => current + 1)
    } catch {
      // Se deja el aviso en el estado y el inventario intacto. Un error de red que vacía la
      // pantalla deja al usuario pensando que no tiene dependencias, que es peor que no
      // sincronizar.
      setLastSync(null)
      setSyncFailed(true)
    } finally {
      setIsSyncing(false)
    }
  }

  const hayFiltros = Boolean(search || ecosystem || repositoryId || filtro !== 'todos')

  function reiniciar(): void {
    setSearch('')
    setEcosystem(null)
    setRepositoryId(null)
    setFiltro('todos')
  }

  return (
    <section className="page-section" aria-labelledby="supply-chain-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="supply-chain-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => setReloadToken((current) => current + 1)}
        >
          {t('refresh')}
        </button>
      </div>

      {summary && summary.total_dependencies > 0 && (
        <div className="metric-grid supply-metrics">
          <article className="metric-card">
            <p className="metric-label">{t('metrics.total')}</p>
            <p className="metric-value">{summary.total_dependencies}</p>
            <p className="metric-caption">
              {t('metrics.repositories', { count: summary.repositories_indexed })}
            </p>
          </article>
          {/* El "sin comprobar" va **antes** que "limpias". Es el dato que decide si el
              usuario se fia de lo que ve, y esconderlo debajo de un verde tranquilizador es
              exactamente el fallo que este estado de tres valores evita. */}
          <article className="metric-card supply-metric-unchecked">
            <p className="metric-label">{t('metrics.unchecked')}</p>
            <p className="metric-value">{summary.unchecked}</p>
            <p className="metric-caption">{t('metrics.uncheckedCaption')}</p>
          </article>
          <article className="metric-card">
            <p className="metric-label">{t('metrics.clean')}</p>
            <p className="metric-value">{summary.clean}</p>
            <p className="metric-caption">{t('metrics.cleanCaption')}</p>
          </article>
          <article className="metric-card">
            <p className="metric-label">{t('metrics.vulnerable')}</p>
            <p className="metric-value">{summary.vulnerable}</p>
            <p className="metric-caption">{t('metrics.vulnerableCaption')}</p>
          </article>
        </div>
      )}

      <div className="content-card">
        <div className="filter-bar">
          <div className="search-field">
            <Search size={15} aria-hidden="true" />
            <input
              type="search"
              value={search}
              placeholder={t('search.placeholder')}
              aria-label={t('search.placeholder')}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>

          <div className="filter-group" role="group" aria-label={t('filters.ecosystemLabel')}>
            <button
              className={ecosystem === null ? 'filter-chip filter-chip-active' : 'filter-chip'}
              type="button"
              onClick={() => setEcosystem(null)}
            >
              {t('filters.allEcosystems')}
            </button>
            {ECOSYSTEMS.map((valor) => (
              <button
                className={ecosystem === valor ? 'filter-chip filter-chip-active' : 'filter-chip'}
                type="button"
                key={valor}
                onClick={() => setEcosystem(ecosystem === valor ? null : valor)}
              >
                {t(`ecosystems.${valor}`)}
              </button>
            ))}
          </div>

          {/* El filtro de vulnerabilidad es el unico sitio donde se ven los tres estados, y
              por eso incluye "sin comprobar" como opcion propia. */}
          <div className="filter-group" role="group" aria-label={t('filters.statusLabel')}>
            {(['todos', 'unchecked', 'vulnerable', 'clean'] as FiltroVulnerabilidad[]).map(
              (valor) => (
                <button
                  className={filtro === valor ? 'filter-chip filter-chip-active' : 'filter-chip'}
                  type="button"
                  key={valor}
                  onClick={() => setFiltro(valor)}
                >
                  {t(`filters.${valor}`)}
                </button>
              ),
            )}
          </div>

          {hayFiltros && (
            <button className="ghost-button" type="button" onClick={reiniciar}>
              {t('filters.clear')}
            </button>
          )}

          {/*
            El selector de repositorio va en la barra de filtros y **no** dentro de la tabla.

            Antes no había selector: `repositoryId` solo lo ponía `reiniciar()`, con el valor
            `null`, así que el botón de sincronizar —que sale cuando hay repositorio
            seleccionado— no se veía nunca y el inventario quedaba vacío para siempre. Es el
            fallo más caro de una pantalla vacía: no da error, no avisa y su única explicación
            es un botón que no aparece.

            Y va aquí y no arriba porque es un filtro: filtra la tabla **y** decide a qué
            repositorio se sincroniza. Con el selector solo arriba, el botón de la barra
            dispararía cuatro peticiones al proveedor sin decir a cuál, que es justo lo que el
            comentario original del botón intentaba evitar.
          */}
          <div className="filter-field supply-repository-field">
            <label htmlFor="supply-repository">{t('filters.repositoryLabel')}</label>
            <select
              id="supply-repository"
              value={repositoryId ?? ''}
              disabled={repositories.length === 0}
              onChange={(event) => setRepositoryId(event.target.value || null)}
            >
              <option value="">{t('filters.allRepositories')}</option>
              {repositories.map((repositorio) => (
                <option key={repositorio.id} value={repositorio.id}>
                  {repositorio.full_name}
                </option>
              ))}
            </select>
          </div>

          {/*
            El botón sigue apareciendo solo con un repositorio seleccionado, por el motivo que
            dice su comentario: sincronizar sin saber **qué** se sincroniza sería disparar
            cuatro peticiones al proveedor sin decir a cuál. Lo que ha cambiado es que ahora
            **hay** forma de llegar a esa situación.
          */}
          {repositoryId !== null && (
            <button
              className="secondary-button"
              type="button"
              disabled={isSyncing}
              onClick={() => void sincronizar()}
            >
              <RefreshCw size={16} aria-hidden="true" />
              {isSyncing ? t('sync.syncing') : t('sync.button')}
            </button>
          )}
        </div>

        {/* El resultado de la última sincronización, o el fallo. Se queda en pantalla hasta la
            siguiente porque el usuario necesita leerlo: "se indexaron 12 paquetes" de un aviso
            que desaparece al segundo es un dato que no sirve para nada. */}
        {syncFailed && (
          <div className="sync-notice sync-notice-error" role="alert">
            <p>{t('sync.failed')}</p>
          </div>
        )}

        {lastSync !== null && (
          <div className="sync-notice" role="status">
            <p>
              {t('sync.result', {
                found: lastSync.manifests_found.length,
                total: lastSync.manifests_found.length + lastSync.manifests_missing.length,
                packages: lastSync.packages_inserted + lastSync.packages_updated,
              })}
            </p>
            {lastSync.manifests_missing.length > 0 && (
              <p className="sync-notice-detail">
                {t('sync.missing', { manifests: lastSync.manifests_missing.join(', ') })}
              </p>
            )}
            {lastSync.errors.length > 0 && (
              <ul className="sync-notice-errors">
                {lastSync.errors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {listFailed && (
          <div className="empty-card">
            <p>{t('loadFailed')}</p>
            <button
              className="secondary-button"
              type="button"
              onClick={() => setReloadToken((current) => current + 1)}
            >
              {t('retry')}
            </button>
          </div>
        )}

        {!listFailed && isLoading && <p className="table-caption">{t('loading')}</p>}

        {!listFailed && !isLoading && items.length === 0 && (
          <div className="empty-card">
            {/* El marco `empty-card-mark` es el que ya usan Repositorios y el panel de
                error del admin: un icono suelto de 26 px flotando sobre el fondo de la
                tarjeta se leía como texto grande, no como una marca. */}
            <span className="empty-card-mark" aria-hidden="true">
              <Package size={20} />
            </span>
            <h3>{hayFiltros ? t('empty.filteredTitle') : t('empty.title')}</h3>
            <p>{hayFiltros ? t('empty.filteredBody') : t('empty.body')}</p>
            {hayFiltros ? (
              <button className="secondary-button" type="button" onClick={reiniciar}>
                {t('filters.clear')}
              </button>
            ) : null}
            {/*
              El inventario vacío **sin filtros** ofrece la acción que lo llena. Es la mitad que
              faltaba: el estado vacío decía cómo se construye el inventario —«leyendo los
              manifiestos»— y no decía quién los lee ni desde dónde. Sin este botón, la pantalla
              sale vacía, dice que el inventario se construye así y se queda ahí.

              Y sale solo sin filtros: si hay un filtro activo y no sale nada, la respuesta es
              quitar el filtro, no sincronizar otra vez un repositorio que ya está indexado.
            */}
            {!hayFiltros && repositories.length > 0 ? (
              <p className="empty-card-action">{t('empty.syncHint')}</p>
            ) : null}
          </div>
        )}

        {!listFailed && !isLoading && items.length > 0 && (
          <>
            <div className="table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('columns.package')}</th>
                    <th>{t('columns.version')}</th>
                    <th>{t('columns.ecosystem')}</th>
                    <th>{t('columns.repository')}</th>
                    <th>{t('columns.license')}</th>
                    <th>{t('columns.status')}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((paquete) => (
                    <tr key={paquete.id}>
                      <td>
                        <span className="supply-package-name">{paquete.name}</span>
                        {/* La distincion de desarrollo va **en la fila** y no en un filtro
                            oculto: una vulnerabilidad en `devDependencies` no llega a
                            produccion, y sin esta marca el usuario tiene que abrir el manifiesto
                            para averiguarlo. */}
                        {paquete.is_dev_dependency && (
                          <span className="supply-dev-tag">{t('devDependency')}</span>
                        )}
                      </td>
                      <td className="mono">{paquete.version || t('versionUnpinned')}</td>
                      <td>
                        <span className={BADGE_DE_ECOSYSTEM[paquete.ecosystem]}>
                          {t(`ecosystems.${paquete.ecosystem}`)}
                        </span>
                      </td>
                      <td className="cell-muted">{paquete.repository_name}</td>
                      <td>
                        {paquete.license ? (
                          <span className="cell-muted">{paquete.license}</span>
                        ) : (
                          // Se dice "sin declarar" y no un guion: el punto es que el dato falta,
                          // y medir la cobertura de licencias es justamente lo que hace util
                          // este inventario.
                          <span className="cell-muted">{t('licenseUnknown')}</span>
                        )}
                      </td>
                      <td>
                        <EstadoDeVulnerabilidad paquete={paquete} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="table-caption">
              {t('tableCount', { shown: items.length, total })}
            </p>
          </>
        )}
      </div>
    </section>
  )
}

function EstadoDeVulnerabilidad({ paquete }: { paquete: SupplyChainPackage }) {
  const { t } = useTranslation('supplyChain')

  // El orden importa: `null` se comprueba **antes** que el booleano, porque un `if` sobre un
  // valor nullable cae en la rama falsa con `null` y acabaria pintando "limpio" lo que no se ha
  // comprobado. Es exactamente el error que este componente existe para no cometer.
  if (paquete.has_vulnerabilities === null) {
    return (
      <span
        className="supply-status supply-status-unchecked"
        title={t('status.uncheckedHelp')}
      >
        <CircleHelp size={13} aria-hidden="true" />
        {t('status.unchecked')}
      </span>
    )
  }

  if (paquete.has_vulnerabilities) {
    return (
      <span
        className="supply-status supply-status-vulnerable"
        title={paquete.cve_ids.join(', ')}
      >
        <AlertTriangle size={13} aria-hidden="true" />
        {t('status.vulnerable', { count: paquete.cve_ids.length })}
      </span>
    )
  }

  return (
    <span className="supply-status supply-status-clean" title={t('status.cleanHelp')}>
      <CheckCircle2 size={13} aria-hidden="true" />
      {t('status.clean')}
    </span>
  )
}
