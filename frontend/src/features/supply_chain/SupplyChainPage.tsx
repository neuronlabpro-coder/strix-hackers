import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleHelp, Package, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  getSupplyChainPackages,
  getSupplyChainSummary,
  type SupplyChainQuery,
} from '../../lib/api'
import type { Ecosystem, SupplyChainPackage, SupplyChainSummary } from '../../types/api'
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
        </div>

        {listFailed && (
          <div className="empty-state">
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
          <div className="empty-state">
            <Package size={26} aria-hidden="true" />
            <h3>{hayFiltros ? t('empty.filteredTitle') : t('empty.title')}</h3>
            <p>{hayFiltros ? t('empty.filteredBody') : t('empty.body')}</p>
            {hayFiltros && (
              <button className="secondary-button" type="button" onClick={reiniciar}>
                {t('filters.clear')}
              </button>
            )}
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
