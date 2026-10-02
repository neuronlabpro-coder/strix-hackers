/**
 * `/containers`: el inventario de imágenes de contenedor, escaneado desde un agente en la red
 * del cliente.
 *
 * ## Por qué es una pantalla aparte y no una sección de redes
 *
 * Porque las dos miden cosas que no se suman. Una imagen tiene paquetes, sistema operativo y
 * capas; una red tiene hosts, puertos y banners. En una tabla con las dos, cada fila viene con
 * medio screening vacio, el filtro de estado sirve para las dos y el de tipo no sirve para
 * ninguna, y el gráfico acaba siendo una mezcla donde el número de paquetes y el de hosts se
 * suman sin que la suma signifique nada.
 *
 * Por eso hay dos rutas, dos consultas de resumen y dos tablas. Lo que comparten son los
 * agentes, y esa parte vive en un componente aparte.
 *
 * ## Por qué el buscador filtra en el cliente y no en el servidor
 *
 * Porque una imagen se busca por su **referencia** —`alpine:3.20`, `ghcr.io/org/app:1.4`— y
 * la lista cabe en una pagina. Mandar el texto al servidor para filtrar veinte filas es una
 * ida a la base por cada tecla; con la lista en memoria el filtro es inmediato y el servidor no
 * necesita un parametro de busqueda que solo se usaria aqui.
 *
 * ## Por qué el límite de la tabla dice lo que dice
 *
 * Un escaneo de imagen devuelve cientos de paquetes. Ponerlos en la tabla de la pantalla la
 * convierte en una lista interminable que hay que paginar para poder llegar al título, y el
 * título es lo que importa. Los que se ven son una muestra y el botón de abajo lleva el
 * inventario entero, que es donde se busca un paquete concreto.
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Package, RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ChartOption } from '../../charts/EChart'
import { readChartPalette } from '../../charts/palette'
import { getContainersSummary, listAgentJobs } from '../../lib/agentsApi'
import type { AgentJob, AgentJobStatus, AgentSummary } from '../../types/agents'
import { useAuth } from '../auth/useAuth'
import { AgentPanel } from './AgentPanel'
import { JobDetailPanel } from './JobDetailPanel'
import { ScanForm } from './ScanForm'
import { ESTADOS, etiquetaDeEstado } from './estados'
import { StatusBadge } from './StatusBadge'

// Apache ECharts entra en su propio chunk: el shell y el resto de pantallas no lo descargan.
const EChart = lazy(() =>
  import('../../charts/EChart').then((module) => ({ default: module.EChart }))
)

const PAGE_SIZE = 25

/**
 * La lista vacía, **una sola vez** en el módulo.
 *
 * Porque `jobs` se deriva así: `datos?.clave === clave ? datos.jobs : SIN_TRABAJOS`. Con un `[]`
 * literal en el código, cada render crea un array nuevo, y el `useMemo` del buscador depende de
 * `jobs`: se invalidaría en cada render y la memoización no memoizaría nada. Además
 * `react-hooks(exhaustive-deps)` lo avisa, que es la forma barata de que el fallo se vea en vez
 * de notarse en un profiler dentro de seis meses.
 */
const SIN_TRABAJOS: readonly AgentJob[] = []

/** Cuántos paquetes se ven en la tabla antes de que haya que abrir el inventario. */
const PAQUETES_VISIBLES = 6

export function ContainersPage() {
  const { t } = useTranslation('agents')
  const { token, selectedOrganizationId } = useAuth()

  const [summary, setSummary] = useState<AgentSummary | null>(null)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<AgentJobStatus | 'todos'>('todos')
  const [reload, setReload] = useState(0)
  const [scanOpen, setScanOpen] = useState(false)
  const [detail, setDetail] = useState<AgentJob | null>(null)

  const isAuthenticated = Boolean(token && selectedOrganizationId)

  /*
    Los datos se guardan **con la clave del sondeo que los produjo**, y `cargando` se deriva de
    "no tengo todavía la respuesta que pedí", que es la pregunta correcta.

    Es el patrón de `useAdminPage` y se copia por una razón concreta: con `setLoading(true)` al
    principio del efecto, un efecto que se limpia antes de resolver deja la vista colgada en
    «cargando» para siempre. No es un detalle: cambiar de filtro a mitad de una respuesta es la
    forma normal de usar esta pantalla, y es justo el caso en que el efecto se limpia.
  */
  const clave = `${token ?? ''}:${selectedOrganizationId ?? ''}:${status}:${reload}`
  const [datos, setDatos] = useState<{ clave: string; jobs: AgentJob[]; total: number } | null>(
    null
  )
  const [failed, setFailed] = useState(false)
  const loading = isAuthenticated && datos?.clave !== clave
  const jobs: readonly AgentJob[] = datos?.clave === clave ? datos.jobs : SIN_TRABAJOS
  const total = datos?.clave === clave ? datos.total : 0

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void listAgentJobs(token, selectedOrganizationId, {
      limit: PAGE_SIZE,
      offset: 0,
      tipo: 'CONTAINER_SCAN',
      ...(status === 'todos' ? {} : { estado: status }),
    })
      .then((respuesta) => {
        if (!isActive) return
        setDatos({ clave, jobs: respuesta.items, total: respuesta.total })
        setFailed(false)
      })
      .catch(() => {
        if (isActive) setFailed(true)
      })
    return () => {
      isActive = false
    }
  }, [clave, token, selectedOrganizationId, status])

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void getContainersSummary(token, selectedOrganizationId)
      .then((cabecera) => {
        if (isActive) setSummary(cabecera)
      })
      .catch(() => {
        // Los KPI son un extra de la cabecera. Si no se pueden leer, la tabla sigue valiendo y
        // no merece la pena tapar la pantalla con un error por cuatro numeros.
        if (isActive) setSummary(null)
      })
  }, [token, selectedOrganizationId, reload])

  const refrescar = useCallback(() => {
    setReload((valor) => valor + 1)
  }, [])

  // El buscador acota por la referencia, que es lo que el usuario tiene delante. Se comparan
  // en minusculas para que `ALPINE` encuentre `alpine`, que es lo que espera cualquiera.
  const filtrados = useMemo(() => {
    const aguja = search.trim().toLowerCase()
    if (!aguja) return jobs
    return jobs.filter((trabajo) => trabajo.target.toLowerCase().includes(aguja))
  }, [jobs, search])

  const hayFiltros = search.trim().length > 0 || status !== 'todos'
  const palette = useMemo(() => readChartPalette(), [])
  const porEstado = useMemo(() => estadoOption(summary, palette), [summary, palette])
  const porDia = useMemo(() => serieOption(summary, palette), [summary, palette])
  const porEcosistema = useMemo(
    () => ecosistemaOption(summary, palette),
    [summary, palette]
  )

  if (!isAuthenticated) {
    return (
      <div className="page-section">
        <div className="page-header">
          <h1>{t('containers.title')}</h1>
          <p className="page-description">{t('containers.subtitle')}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="page-section">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('containers.eyebrow')}</p>
          <h1>{t('containers.title')}</h1>
          <p className="page-description">{t('containers.subtitle')}</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={refrescar}
            aria-label={t('containers.refresh')}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('containers.refresh')}</span>
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => setScanOpen(true)}
            disabled={!summary || summary.vivos === 0}
            title={summary && summary.vivos === 0 ? t('containers.noAgent') : undefined}
          >
            {t('containers.newScan')}
          </button>
        </div>
      </div>

      {summary && (
        <div className="metric-grid">
          <article className="metric-card">
            <p className="eyebrow">{t('containers.kpis.images')}</p>
            <strong className="metric-value mono">{summary.total_imagenes}</strong>
            <span className="metric-caption">
              {t('containers.kpis.inventoried')}: {summary.imagenes_inventariadas}
            </span>
          </article>
          <article className="metric-card">
            <p className="eyebrow">{t('containers.kpis.packages')}</p>
            <strong className="metric-value mono">{summary.total_paquetes}</strong>
            <span className="metric-caption">
              {t('containers.kpis.ecosystems')}: {summary.paquetes_por_ecosistema['apk'] ?? 0}{' '}
              {summary.paquetes_por_ecosistema['dpkg'] ?? 0}
            </span>
          </article>
          <article className="metric-card">
            <p className="eyebrow">{t('containers.kpis.layers')}</p>
            <strong className="metric-value mono">{summary.total_capas}</strong>
            <span className="metric-caption">
              {t('containers.kpis.withoutInventory')}: {summary.imagenes_sin_inventario}
            </span>
          </article>
          <article className="metric-card">
            <p className="eyebrow">{t('containers.kpis.agents')}</p>
            <strong className="metric-value mono">
              {summary.vivos}
              <span className="metric-suffix">/{summary.total_agentes}</span>
            </strong>
            <span className="metric-caption">
              {t('containers.kpis.connectedIn', { minutes: Math.round(summary.ventana_de_vida / 60) })}
            </span>
          </article>
        </div>
      )}

      <div className="chart-grid">
        <section className="content-card" aria-labelledby="containers-trend">
          <p className="eyebrow">{t('containers.charts.trendEyebrow')}</p>
          <h2 id="containers-trend">{t('containers.charts.trendTitle')}</h2>
          <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
            <EChart
              option={porDia}
              height={220}
              ariaLabel={t('containers.charts.trendAria', {
                total: summary?.por_dia.reduce((suma, dia) => suma + dia.escaneos, 0) ?? 0,
              })}
            />
          </Suspense>
          <p className="visually-hidden">
            {t('containers.charts.trendTitle')}:{' '}
            {t('containers.charts.trendAria', {
              total: summary?.por_dia.reduce((suma, dia) => suma + dia.escaneos, 0) ?? 0,
            })}
          </p>
        </section>

        <section className="content-card" aria-labelledby="containers-status">
          <p className="eyebrow">{t('containers.charts.statusEyebrow')}</p>
          <h2 id="containers-status">{t('containers.charts.statusTitle')}</h2>
          <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
            <EChart
              option={porEstado}
              height={220}
              ariaLabel={t('containers.charts.statusAria', {
                done: summary?.por_estado.COMPLETED ?? 0,
              })}
            />
          </Suspense>
        </section>

        <section className="content-card" aria-labelledby="containers-ecosystem">
          <p className="eyebrow">{t('containers.charts.ecosystemEyebrow')}</p>
          <h2 id="containers-ecosystem">{t('containers.charts.ecosystemTitle')}</h2>
          <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
            <EChart
              option={porEcosistema}
              height={220}
              ariaLabel={t('containers.charts.ecosystemAria', {
                total: summary?.total_paquetes ?? 0,
              })}
            />
          </Suspense>
        </section>
      </div>

      <AgentPanel
        onRegistered={refrescar}
        onRevoked={refrescar}
        resumen={summary}
      />

      <div className="content-card">
        <div className="section-header">
          <h2>{t('containers.scans.title')}</h2>
          <span className="badge">{total}</span>
        </div>

        <div className="filter-bar">
          <div className="filter-field filter-field-search">
            <label htmlFor="containers-search">{t('containers.filters.search')}</label>
            <div className="search-field">
              <Search size={16} aria-hidden="true" />
              <input
                id="containers-search"
                type="search"
                value={search}
                onChange={(evento) => setSearch(evento.target.value)}
                placeholder={t('containers.filters.searchPlaceholder')}
              />
            </div>
          </div>
          <div className="filter-field">
            <label htmlFor="containers-status-filter">{t('containers.filters.status')}</label>
            <select
              id="containers-status-filter"
              value={status}
              onChange={(evento) => setStatus(evento.target.value as AgentJobStatus | 'todos')}
            >
              <option value="todos">{t('containers.filters.allStatuses')}</option>
              {ESTADOS.map((clave) => (
                <option key={clave} value={clave}>
                  {t(etiquetaDeEstado(clave))}
                </option>
              ))}
            </select>
          </div>
        </div>

        {failed ? (
          <div className="empty-card">
            <span className="empty-card-mark" aria-hidden="true">
              <Package size={20} />
            </span>
            <h3>{t('containers.errors.loadFailed')}</h3>
            <button type="button" className="secondary-button" onClick={refrescar}>
              {t('containers.refresh')}
            </button>
          </div>
        ) : filtrados.length === 0 && !loading ? (
          <div className="empty-card">
            <span className="empty-card-mark" aria-hidden="true">
              <Package size={20} />
            </span>
            <h2>{hayFiltros ? t('containers.empty.filtered') : t('containers.empty.title')}</h2>
            <p>{hayFiltros ? t('containers.empty.filteredBody') : t('containers.empty.body')}</p>
          </div>
        ) : (
          <>
            <div className="table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('containers.table.image')}</th>
                    <th>{t('containers.table.os')}</th>
                    <th>{t('containers.table.packages')}</th>
                    <th>{t('containers.table.layers')}</th>
                    <th>{t('containers.table.status')}</th>
                    <th>{t('containers.table.date')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filtrados.map((trabajo) => {
                    const resultado = (trabajo.result ?? {}) as Record<string, unknown>
                    const paquetes = Array.isArray(resultado.paquetes)
                      ? (resultado.paquetes as Array<Record<string, unknown>>)
                      : []
                    const sistema = typeof resultado.sistema_operativo === 'string'
                      ? resultado.sistema_operativo
                      : null
                    const version = typeof resultado.version_sistema_operativo === 'string'
                      ? resultado.version_sistema_operativo
                      : null
                    return (
                      <tr
                        key={trabajo.id}
                        className="row-clickable"
                        tabIndex={0}
                        onClick={() => setDetail(trabajo)}
                        onKeyDown={(evento) => {
                          if (evento.key === 'Enter') setDetail(trabajo)
                        }}
                      >
                        <td className="mono cell-target">{trabajo.target}</td>
                        <td className="mono">
                          {sistema ? (
                            <>
                              {sistema}
                              {version ? ` ${version}` : ''}
                            </>
                          ) : (
                            <span className="cell-muted">—</span>
                          )}
                        </td>
                        <td>
                          {paquetes.length > 0 ? (
                            <span className="package-chips">
                              {paquetes.slice(0, PAQUETES_VISIBLES).map((paquete) => (
                                <span key={String(paquete.name)} className="chip mono">
                                  {String(paquete.name)}
                                </span>
                              ))}
                              {paquetes.length > PAQUETES_VISIBLES && (
                                <span className="chip chip-more">
                                  {t('containers.table.morePackages', {
                                    count: paquetes.length - PAQUETES_VISIBLES,
                                  })}
                                </span>
                              )}
                            </span>
                          ) : (
                            <span className="cell-muted">—</span>
                          )}
                        </td>
                        <td className="mono">
                          {typeof resultado.total_capas === 'number' ? resultado.total_capas : '—'}
                        </td>
                        <td>
                          <StatusBadge job={trabajo} />
                        </td>
                        <td>{new Date(trabajo.created_at).toLocaleDateString()}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <p className="table-caption">
              {t('containers.table.count', { shown: filtrados.length, total })}
            </p>
          </>
        )}
      </div>

      {scanOpen && (
        <ScanForm
          kind="CONTAINER_SCAN"
          onClose={() => setScanOpen(false)}
          onCreated={() => {
            setScanOpen(false)
            refrescar()
          }}
        />
      )}

      {detail && <JobDetailPanel job={detail} kind="CONTAINER_SCAN" onClose={() => setDetail(null)} />}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Los gráficos
// --------------------------------------------------------------------------- //

/** Escaneos por día, en barras apiladas de terminados y fallidos. */
function serieOption(summary: AgentSummary | null, palette: ReturnType<typeof readChartPalette>): ChartOption {
  const dias = summary?.por_dia ?? []
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: palette.surface,
      borderColor: palette.secondary,
      textStyle: { color: palette.primary },
    },
    legend: {
      show: true,
      bottom: 0,
      textStyle: { color: palette.secondary, fontSize: 11 },
    },
    grid: { left: 8, right: 8, top: 16, bottom: 28, containLabel: true },
    xAxis: {
      type: 'category',
      data: dias.map((dia) => dia.dia.slice(5)),
      axisLine: { lineStyle: { color: palette.secondary } },
      axisLabel: { color: palette.secondary, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLine: { show: false },
      axisLabel: { color: palette.secondary, fontSize: 10 },
      splitLine: { lineStyle: { color: palette.surface } },
    },
    series: [
      {
        name: 'ok',
        type: 'bar',
        stack: 'total',
        data: dias.map((dia) => dia.terminados),
        itemStyle: { color: palette.accent },
        barMaxWidth: 18,
      },
      {
        name: 'ko',
        type: 'bar',
        stack: 'total',
        data: dias.map((dia) => dia.fallidos),
        itemStyle: { color: palette.critical },
        barMaxWidth: 18,
      },
    ],
  }
}

/** Reparto por estado, en torta. */
function estadoOption(summary: AgentSummary | null, palette: ReturnType<typeof readChartPalette>): ChartOption {
  const porEstado = summary?.por_estado ?? {}
  const claves = ['COMPLETED', 'FAILED', 'RUNNING', 'CLAIMED', 'QUEUED'] as const
  const colores: Record<string, string> = {
    COMPLETED: palette.accent,
    FAILED: palette.critical,
    RUNNING: palette.low,
    CLAIMED: palette.medium,
    QUEUED: palette.secondary,
  }
  const datos = claves
    .map((clave) => ({ nombre: clave, valor: porEstado[clave] ?? 0 }))
    .filter((entrada) => entrada.valor > 0)
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: palette.surface,
      borderColor: palette.secondary,
      textStyle: { color: palette.primary },
    },
    series: [
      {
        type: 'pie',
        radius: ['58%', '82%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: true,
        label: { show: false },
        data: datos.map((entrada) => ({
          name: entrada.nombre,
          value: entrada.valor,
          itemStyle: { color: colores[entrada.nombre] },
        })),
      },
    ],
  }
}

/** Paquetes por ecosistema. */
function ecosistemaOption(
  summary: AgentSummary | null,
  palette: ReturnType<typeof readChartPalette>
): ChartOption {
  const entrada = Object.entries(summary?.paquetes_por_ecosistema ?? {})
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: palette.surface,
      borderColor: palette.secondary,
      textStyle: { color: palette.primary },
    },
    series: [
      {
        type: 'pie',
        radius: '78%',
        center: ['50%', '50%'],
        label: { show: false },
        data: entrada.map(([ecosistema, cuenta], indice) => ({
          name: ecosistema,
          value: cuenta,
          itemStyle: { color: [palette.accent, palette.low, palette.medium][indice % 3] },
        })),
      },
    ],
  }
}
