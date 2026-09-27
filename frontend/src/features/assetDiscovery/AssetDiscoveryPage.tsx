/**
 * Vista de descubrimiento de activos.
 *
 * ## Por qué el botón de escaneo se ofrece por fila y no como acción global
 *
 * Porque el descubrimiento se hace **contra un dominio verificado**, y el resultado se
 * guarda bajo ese dominio. Un "escanear todo" tendría necesita decidir por qué dominio empieza
 * cada subdominio, y un mismo host puede pertenecer a dos dominios de la misma organización
 * —son dos activos distintos, con distinto contexto—. Ofrecerlo por fila hace que esa
 * decisión la tome la persona que conoce la infraestructura, que es la única que puede.
 *
 * ## Por qué la respuesta `202` no se pinta como "terminado"
 *
 * Porque el trabajo no está hecho cuando llega. Decir "36 nuevos" sería mentir: lo que se ha
 * guardado son los que ya había. Se dice lo que se sabe —cuántos había— y se recarga la
 * tabla, que es lo que permite al usuario ver el progreso sin un sistema de progreso que
 * aquí no existe.
 */

import { useCallback, useMemo, useState } from 'react'
import { Radar, RefreshCw, ScanLine } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/useAuth'
import { listDiscoveredAssets, listDomains, startDiscovery } from '../../lib/assetsApi'
import type { AssetListResponse, AssetType, DomainListResponse } from '../../types/assets'
import { formatDate } from '../../lib/format'
import { useAsyncResource } from '../shared/useAsyncResource'
import { useToast } from '../shared/toast-context'

/** Página de la tabla. Coincide con el tope del servidor para que no haya recorte callado. */
const PAGE_SIZE = 50

/** Filtro de tipo. `'ALL'` no es un valor de la API: se traduce a "sin filtro". */
type TypeFilter = AssetType | 'ALL'

export function AssetDiscoveryPage() {
  const { t } = useTranslation('assetDiscovery')
  const { token, selectedOrganizationId } = useAuth()
  const organizationId = selectedOrganizationId
  const { notify } = useToast()

  const [typeFilter, setTypeFilter] = useState<TypeFilter>('ALL')
  const [domainFilter, setDomainFilter] = useState('')
  const [page, setPage] = useState(0)
  const [scanningId, setScanningId] = useState<string | null>(null)

  /**
   * Token y workspace ya estrechados, o `null` si aún no los hay.
   *
   * Los manejadores de acción de esta vista solo existen dentro de `ProtectedShell`, donde
   * los dos están siempre. Estrechar aquí en vez de repetir la comprobación en cada uno
   * deja que el tipo lo diga **una** vez, y convierte un `undefined` en un retorno
   * temprano en vez de una petición con cabeceras vacías que el servidor rechaza con un
   * `403` que no explica nada.
   *
   * El `fetcher` **no** usa esto: para leer, la clave **es** el identificador del
   * workspace, y eso garantiza que los datos en pantalla son los del workspace de la
   * cabecera, no el que hubiera en memoria al escribirse el manejador.
   */
  const sesion = useMemo(() => {
    if (token === null || organizationId === null) {
      return null
    }
    return { token, organizationId }
  }, [token, organizationId])

  // Aquí la clave sí es solo el workspace: el selector de dominio no se filtra a sí
  // mismo. Ver la nota equivalente en `DomainsPage`.
  const dominios = useAsyncResource<DomainListResponse>(
    useCallback(
      async (key: string): Promise<DomainListResponse> => {
        const activeToken = token
        if (activeToken === null) {
          throw new Error('sin token')
        }
        return listDomains(activeToken, key)
      },
      [token],
    ),
    organizationId,
  )

  /**
   * Clave del inventario. **Describe la petición entera**, no solo el workspace.
   *
   * Es lo que hace que `useAsyncResource` recargue: al cambiar de filtro la clave cambia,
   * y con ella la petición. Por eso los filtros van aquí y no dentro del `fetcher`: si
   * estuvieran en el closure, el `useCallback` devolvería una función nueva en cada render
   * y el hook pediría los datos en bucle. Y por eso se **parsean** de la clave en lugar de
   * leerse de un `ref`: la clave es la única fuente, de modo que no puede quedar
   * desincronizada con lo que realmente se pidió.
   */
  const claveInventario = useMemo(
    () =>
      [organizationId, typeFilter, domainFilter, page]
        .map((parte) => (parte === null ? '' : String(parte)))
        .join('|'),
    [organizationId, typeFilter, domainFilter, page],
  )

  const assets = useAsyncResource<AssetListResponse>(
    useCallback(
      async (key: string): Promise<AssetListResponse> => {
        const activeToken = token
        if (activeToken === null) {
          throw new Error('sin token')
        }
        const [, tipo, dominio, pagina] = key.split('|')
        return listDiscoveredAssets(activeToken, key.split('|')[0], {
          asset_type: tipo !== 'ALL' ? (tipo as AssetType) : undefined,
          domain_id: dominio === '' ? undefined : dominio,
          limit: PAGE_SIZE,
          offset: Number(pagina) * PAGE_SIZE,
        })
      },
      [token],
    ),
    organizationId === null ? null : claveInventario,
  )

  const items = useMemo(() => assets.data?.items ?? [], [assets.data])
  const total = assets.data?.total ?? 0
  const totalPaginas = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const scan = async (domainId: string) => {
    if (sesion === null) {
      return
    }
    setScanningId(domainId)
    try {
      const encolado = await startDiscovery(sesion.token, sesion.organizationId, domainId)
      notify(
        'info',
        t('toasts.enqueued', {
          domain: encolado.domain_name,
          existing: encolado.existing_assets,
        }),
      )
      assets.reload()
    } catch {
      notify('error', t('errors.scan'))
    } finally {
      setScanningId(null)
    }
  }

  return (
    <section className="page-section" aria-labelledby="assets-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="assets-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <button className="secondary-button" type="button" onClick={assets.reload}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('actions.refresh')}</span>
          </button>
        </div>
      </div>

      <form className="filter-bar" role="search" onSubmit={(event) => event.preventDefault()}>
        <div className="filter-field">
          <label htmlFor="asset-type-filter">{t('filters.type')}</label>
          <select
            id="asset-type-filter"
            value={typeFilter}
            onChange={(event) => {
              setTypeFilter(event.target.value as TypeFilter)
              setPage(0)
            }}
          >
            <option value="ALL">{t('filters.allTypes')}</option>
            <option value="SUBDOMAIN">{t('types.SUBDOMAIN')}</option>
            <option value="IP_ADDRESS">{t('types.IP_ADDRESS')}</option>
            <option value="API_ENDPOINT">{t('types.API_ENDPOINT')}</option>
          </select>
        </div>

        <div className="filter-field">
          <label htmlFor="asset-domain-filter">{t('filters.domain')}</label>
          <select
            id="asset-domain-filter"
            value={domainFilter}
            onChange={(event) => {
              setDomainFilter(event.target.value)
              setPage(0)
            }}
          >
            <option value="">{t('filters.allDomains')}</option>
            {(dominios.data?.items ?? []).map((domain) => (
              <option key={domain.id} value={domain.id}>
                {domain.domain_name}
              </option>
            ))}
          </select>
        </div>
      </form>

      <section className="panel" aria-labelledby="assets-scan-title">
        <h2 className="panel-title" id="assets-scan-title">
          {t('scan.title')}
        </h2>
        <p className="assets-subtext">{t('scan.description')}</p>
        {dominios.data?.items.filter((domain) => domain.is_verified).length ? (
          <ul className="assets-scan-list">
            {dominios.data.items
              .filter((domain) => domain.is_verified)
              .map((domain) => (
                <li key={domain.id} className="assets-scan-item">
                  <span className="mono">{domain.domain_name}</span>
                  <span className="assets-subtext">
                    {t('scan.assets', { count: domain.asset_count })}
                  </span>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void scan(domain.id)}
                    disabled={scanningId === domain.id}
                  >
                    <ScanLine size={16} aria-hidden="true" />
                    <span>{scanningId === domain.id ? t('scan.starting') : t('scan.start')}</span>
                  </button>
                </li>
              ))}
          </ul>
        ) : (
          <p className="assets-subtext">{t('scan.noVerifiedDomains')}</p>
        )}
      </section>

      {assets.isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : assets.loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={assets.reload}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="empty-card">
          <Radar size={24} aria-hidden="true" />
          <h2>{t('states.emptyTitle')}</h2>
          <p>{t('states.emptyDescription')}</p>
        </div>
      ) : (
        <>
          <p className="cve-count" aria-live="polite">
            {t('states.count', { count: total })}
          </p>
          <div className="table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('table.asset')}</th>
                  <th scope="col">{t('table.domain')}</th>
                  <th scope="col">{t('table.type')}</th>
                  <th scope="col">{t('table.technologies')}</th>
                  <th scope="col">{t('table.lastScanned')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((asset) => (
                  <tr key={asset.id}>
                    <th scope="row" className="mono assets-wrap">
                      {asset.value}
                      {asset.service_name !== null ? (
                        <span className="assets-subtext">{asset.service_name}</span>
                      ) : null}
                    </th>
                    <td className="mono assets-wrap">{asset.domain_name}</td>
                    <td>
                      <span className={`badge badge-status-${asset.asset_type.toLowerCase()}`}>
                        {t(`types.${asset.asset_type}`)}
                      </span>
                    </td>
                    <td>
                      {asset.technologies.length === 0 ? (
                        <span className="assets-subtext">—</span>
                      ) : (
                        <span className="assets-pill-row">
                          {asset.technologies.map((tech) => (
                            <span className="pill" key={tech}>
                              {tech}
                            </span>
                          ))}
                        </span>
                      )}
                    </td>
                    <td>
                      {asset.last_scanned_at === null ? (
                        <span className="assets-subtext">{t('table.never')}</span>
                      ) : (
                        formatDate(asset.last_scanned_at)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {totalPaginas > 1 ? (
            <nav className="pagination" aria-label={t('pagination.label')}>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPage((actual) => Math.max(0, actual - 1))}
                disabled={page === 0}
              >
                <span>{t('pagination.previous')}</span>
              </button>
              <span className="assets-pagination-position" aria-live="polite">
                {t('pagination.position', { page: page + 1, total: totalPaginas })}
              </span>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPage((actual) => Math.min(totalPaginas - 1, actual + 1))}
                disabled={page >= totalPaginas - 1}
              >
                <span>{t('pagination.next')}</span>
              </button>
            </nav>
          ) : null}
        </>
      )}
    </section>
  )
}
