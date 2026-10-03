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
import { Radar, RefreshCw, ScanLine, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/useAuth'
import { listDiscoveredAssets, listDomains, startDiscovery } from '../../lib/assetsApi'
import type { AssetListResponse, DomainListResponse } from '../../types/assets'
import { formatDate } from '../../lib/format'
import { useAsyncResource } from '../shared/useAsyncResource'
import { useToast } from '../shared/toast-context'
import {
  EMPTY_QUERY,
  hayFiltrosPuestos,
  type InventarioQuery,
  type TypeFilter,
} from './filtrosInventario'

/** Página de la tabla. Coincide con el tope del servidor para que no haya recorte callado. */
const PAGE_SIZE = 50

export function AssetDiscoveryPage() {
  const { t } = useTranslation('assetDiscovery')
  const { token, selectedOrganizationId } = useAuth()
  const organizationId = selectedOrganizationId
  const { notify } = useToast()

  const [filters, setFilters] = useState<InventarioQuery>(EMPTY_QUERY)
  const [page, setPage] = useState(0)
  const [scanningId, setScanningId] = useState<string | null>(null)

  /**
   * Los filtros van en **un** estado y no en cuatro.
   *
   * Porque la clave del inventario se construye con ellos, y cuatro estados sueltos obligarían a
   * escribirla con cuatro campos que se pueden desincronizar. Con uno, `cambiar` es siempre la
   * misma operación y la vuelta a la primera página viene en el mismo sitio.
   */
  function cambiar(
    campo: 'type' | 'domainId' | 'search' | 'createdFrom' | 'createdTo',
    valor: string,
  ): void {
    setFilters((actual) => ({
      ...actual,
      // El `as TypeFilter` solo es un `as`: el valor viene del `<select>`, cuyas cuatro
      // opciones son exactamente los cuatro valores del tipo. Es el único `as` de la vista y no
      // es una puerta trasera —no cambia lo que se pide, solo le dice al compilador lo que el
      // DOM ya garantiza—. Para el resto de campos el valor es texto y no necesita nada.
      [campo]: campo === 'type' ? (valor as TypeFilter) : valor,
    }))
    // ## Por qué cambiar un filtro vuelve a la primera página
    //
    // Porque la página 4 del filtro anterior no significa nada en el nuevo. Sin este
    // `setPage(0)`, escribir tres letras en el buscador deja la tabla vacía —el `offset` 150 está
    // más allá del total del resultado nuevo— y el usuario ve «ningún resultado» con un filtro
    // que sí tiene resultados.
    setPage(0)
  }

  const hayFiltros = hayFiltrosPuestos(filters)


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
   *
   * Los filtros van **serializados** con `JSON.stringify` y no unidos con `|`, porque el texto
   * que teclea el usuario puede contener el separador. Con `|` un dominio llamado `a|b` partía
   * la clave en tres y el `fetcher` leía un tipo que no era el pedido.
   */
  const claveInventario = useMemo(
    () => JSON.stringify([organizationId, filters, page]),
    [organizationId, filters, page],
  )

  /**
   * ¿Hay algún dominio verificado del que se pueda haber detectado un activo?
   *
   * El descubrimiento exige un dominio verificado: es lo que autoriza a hacer las consultas
   * DNS. Sin ningún dominio verificado, la lista de activos **no puede** tener filas, y la
   * respuesta del servidor ya se conoce antes de preguntarla.
   *
   * ## Por qué esto se decide antes de la petición y no en el `catch`
   *
   * Por dos razones, y la segunda es la importante. La primera es que ahorra una ida y vuelta
   * en la primera carga de la pantalla. La segunda es que separa los dos casos que se confunden:
   * un workspace sin dominios **no es un error**, y esconderlo detrás de un estado de carga o
   * de un cartel de fallo hace que el usuario busque un problema donde no lo hay. La pantalla
   * que toca es la de vacío ilustrado, y dice qué hacer a continuación.
   */
  const hayDominiosVerificados =
    (dominios.data?.items ?? []).some((domain) => domain.is_verified)

  const assets = useAsyncResource<AssetListResponse>(
    useCallback(
      async (key: string): Promise<AssetListResponse> => {
        const activeToken = token
        if (activeToken === null) {
          throw new Error('sin token')
        }
        const [orgId, filtrosDeClave, pagina] = JSON.parse(key) as [
          string | null,
          InventarioQuery,
          number,
        ]
        return listDiscoveredAssets(activeToken, orgId ?? key, {
          asset_type: filtrosDeClave.type !== 'ALL' ? filtrosDeClave.type : undefined,
          domain_id: filtrosDeClave.domainId === '' ? undefined : filtrosDeClave.domainId,
          query: filtrosDeClave.search.trim() === '' ? undefined : filtrosDeClave.search.trim(),
          created_from: filtrosDeClave.createdFrom === '' ? undefined : filtrosDeClave.createdFrom,
          created_to: filtrosDeClave.createdTo === '' ? undefined : filtrosDeClave.createdTo,
          limit: PAGE_SIZE,
          offset: pagina * PAGE_SIZE,
        })
      },
      [token],
    ),
    // Sin dominio verificado no hay inventario que pedir: se anula la clave y `useAsyncResource`
    // devuelve `isLoading: false` y `data: null` sin lanzar nada. Ver la nota de
    // `hayDominiosVerificados`.
    organizationId === null || !hayDominiosVerificados ? null : claveInventario,
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
        {/* El buscador va primero porque es el filtro que más se usa, y va **con etiqueta**
            como los otros tres: los cuatro controles comparten línea y el botón de limpiar
            queda en la línea de control gracias al `align-self: end` de `.filter-bar-clear`. */}
        <div className="filter-field filter-field-search">
          <label htmlFor="asset-search-filter">{t('filters.search')}</label>
          <span className="search-field">
            <Search size={16} aria-hidden="true" />
            <input
              id="asset-search-filter"
              type="search"
              value={filters.search}
              placeholder={t('filters.searchPlaceholder')}
              onChange={(event) => cambiar('search', event.target.value)}
            />
          </span>
        </div>

        <div className="filter-field">
          <label htmlFor="asset-type-filter">{t('filters.type')}</label>
          <select
            id="asset-type-filter"
            value={filters.type}
            onChange={(event) => cambiar('type', event.target.value)}
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
            value={filters.domainId}
            onChange={(event) => cambiar('domainId', event.target.value)}
          >
            <option value="">{t('filters.allDomains')}</option>
            {(dominios.data?.items ?? []).map((domain) => (
              <option key={domain.id} value={domain.id}>
                {domain.domain_name}
              </option>
            ))}
          </select>
        </div>

        {/*
          El rango va sobre la fecha de **alta** del activo, no sobre la de última revisión que
          muestra la tabla. La razón está en el `title` de las dos etiquetas porque es donde cabe
          sin romper la alineación: una línea de ayuda dentro del `.filter-field` le añadiría
          altura a un solo campo y descuadraría la fila entera.
        */}
        <div className="filter-field">
          <label htmlFor="asset-created-from" title={t('filters.dateHint')}>
            {t('filters.dateFrom')}
          </label>
          <input
            id="asset-created-from"
            type="date"
            value={filters.createdFrom}
            onChange={(event) => cambiar('createdFrom', event.target.value)}
          />
        </div>
        <div className="filter-field">
          <label htmlFor="asset-created-to" title={t('filters.dateHint')}>
            {t('filters.dateTo')}
          </label>
          <input
            id="asset-created-to"
            type="date"
            value={filters.createdTo}
            onChange={(event) => cambiar('createdTo', event.target.value)}
          />
        </div>

        {hayFiltros ? (
          <button
            className="ghost-button filter-bar-clear"
            type="button"
            onClick={() => {
              setFilters(EMPTY_QUERY)
              setPage(0)
            }}
          >
            <span>{t('filters.clear')}</span>
          </button>
        ) : null}
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
          <span className="empty-card-mark" aria-hidden="true">
            <Radar size={20} />
          </span>
          <h2>
            {!hayDominiosVerificados && !dominios.isLoading
              ? t('states.emptyWithoutDomains')
              : hayFiltros
                ? t('states.emptyFilteredTitle')
                : t('states.emptyTitle')}
          </h2>
          <p>
            {!hayDominiosVerificados && !dominios.isLoading
              ? t('states.emptyWithoutDomainsDescription')
              : hayFiltros
                ? t('states.emptyFilteredDescription')
                : t('states.emptyDescription')}
          </p>
          {/*
            El enlace solo aparece cuando el bloqueo real es "no hay dominio verificado". Con
            dominios pero sin activos, la accion util es lanzar un escaneo, que ya esta en el
            panel de arriba; ofrecer ahi un enlace a anadir dominio seria una segundavia
            cuando el problema no son los dominios.
          */}
          {!hayDominiosVerificados && !dominios.isLoading ? (
            <a className="secondary-button" href="/domains">
              <span>{t('states.emptyWithoutDomainsAction')}</span>
            </a>
          ) : null}
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
