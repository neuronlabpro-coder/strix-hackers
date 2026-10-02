/**
 * `/networks`: qué hay abierto en los segmentos de red del cliente, medido desde un agente que
 * corre dentro de su red.
 *
 * ## Por qué no comparte pantalla con `/containers`
 *
 * Porque lo que cuenta cada una no se suma. Una red tiene hosts, puertos y banners; una imagen
 * tiene paquetes y capas. En una tabla con las dos, cada fila viene con media pantalla vacía y el
 * filtro de tipo no sirve para ninguna de las dos. Aquí hay dos rutas, dos consultas de resumen
 * y dos tablas, y lo único que comparten son los agentes.
 *
 * ## Por qué el gráfico de puertos se corta a diez
 *
 * Porque un barrido de un `/16` puede encontrar ochenta puertos distintos, y ochenta barras en
 * una tarjeta de320 px no son un gráfico: son un histograma que no se lee. Se muestran los diez
 * más frecuentes y el resto se cuenta como «otros», que es un dato que además tiene sentido
 * propio: muchos puertos poco frecuentes es una señal distinta de muchos hosts con los mismos
 * puertos.
 *
 * ## Por qué el banner se enseña entero y sin interpretar
 *
 * Porque es lo que se leyó de la red. Un banner de PostgreSQL dice `PostgreSQL 13.4`; la versión
 * es un hecho, y si está desactualizada eso lo decide el razonamiento que ocurre en la
 * plataforma, con la clave del proveedor, no este agente. Recortarlo aquí para que la tabla
 * quede bonita sería quitar el dato y dejar solo la forma.
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Globe, Network, RefreshCw, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ChartOption } from '../../charts/EChart'
import { readChartPalette } from '../../charts/palette'
import { getNetworksSummary, listAgentJobs } from '../../lib/agentsApi'
import type { AgentJob, AgentJobStatus, AgentSummary, NetworkScanResult } from '../../types/agents'
import { useAuth } from '../auth/useAuth'
import { AgentPanel } from './AgentPanel'
import { JobDetailPanel } from './JobDetailPanel'
import { ScanForm } from './ScanForm'
import { ESTADOS, etiquetaDeEstado } from './estados'
import { StatusBadge } from './StatusBadge'

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

export function NetworksPage() {
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
    Igual que en `ContainersPage`, y por la misma razón: `cargando` se deriva de no tener
    todavía la respuesta del sondeo en curso. Con `setLoading(true)` al principio del efecto,
    un efecto que se limpia antes de resolver deja la vista colgada, y cambiar de filtro a
    mitad de una respuesta es lo normal aquí.
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
      tipo: 'NETWORK_SCAN',
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
    void getNetworksSummary(token, selectedOrganizationId)
      .then((cabecera) => {
        if (isActive) setSummary(cabecera)
      })
      .catch(() => {
        // Los KPI son un extra: si fallan, la tabla de escaneos sigue valiendo.
        if (isActive) setSummary(null)
      })
  }, [token, selectedOrganizationId, reload])

  const refrescar = useCallback(() => {
    setReload((valor) => valor + 1)
  }, [])

  const filtrados = useMemo(() => {
    const aguja = search.trim().toLowerCase()
    if (!aguja) return jobs
    return jobs.filter((trabajo) => {
      if (trabajo.target.toLowerCase().includes(aguja)) return true
      // Buscar por un host que aparece en el resultado es la pregunta que alguien hace cuando
      // sabe que algo abrio en una maquina concreta: "¿este barrido lo vio?". Sin esto, el
      // buscador solo encontraria los trabajos por su CIDR y la respuesta seria no.
      const resultado = trabajo.result as NetworkScanResult | null
      const hosts = resultado?.hosts
      if (Array.isArray(hosts)) {
        return hosts.some((host) => host.ip.toLowerCase().includes(aguja))
      }
      return false
    })
  }, [jobs, search])

  const hayFiltros = search.trim().length > 0 || status !== 'todos'
  const palette = useMemo(() => readChartPalette(), [])
  const puertos = useMemo(() => puertosOption(summary, palette), [summary, palette])
  const estado = useMemo(() => estadoOption(summary, palette), [summary, palette])
  const porDia = useMemo(() => serieOption(summary, palette), [summary, palette])

  if (!isAuthenticated) {
    return (
      <div className="page-section">
        <div className="page-header">
          <h1>{t('networks.title')}</h1>
          <p className="page-description">{t('networks.subtitle')}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="page-section">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('networks.eyebrow')}</p>
          <h1>{t('networks.title')}</h1>
          <p className="page-description">{t('networks.subtitle')}</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={refrescar}
            aria-label={t('networks.refresh')}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('networks.refresh')}</span>
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => setScanOpen(true)}
            disabled={!summary || summary.vivos === 0}
            title={summary && summary.vivos === 0 ? t('networks.noAgent') : undefined}
          >
            {t('networks.newScan')}
          </button>
        </div>
      </div>

      {summary && (
        <div className="metric-grid">
          <article className="metric-card">
            <p className="eyebrow">{t('networks.kpis.networks')}</p>
            <strong className="metric-value mono">{summary.total_redes}</strong>
            <span className="metric-caption">
              {t('networks.kpis.addresses')}: {summary.direcciones_analizadas}
            </span>
          </article>
          <article className="metric-card">
            <p className="eyebrow">{t('networks.kpis.hosts')}</p>
            <strong className="metric-value mono">{summary.total_hosts}</strong>
            <span className="metric-caption">{t('networks.kpis.hostsCaption')}</span>
          </article>
          <article className="metric-card">
            <p className="eyebrow">{t('networks.kpis.ports')}</p>
            <strong className="metric-value mono">{summary.total_puertos}</strong>
            <span className="metric-caption">
              {t('networks.kpis.distinctPorts')}: {Object.keys(summary.puertos_por_numero).length}
            </span>
          </article>
          <article className="metric-card">
            <p className="eyebrow">{t('networks.kpis.agents')}</p>
            <strong className="metric-value mono">
              {summary.vivos}
              <span className="metric-suffix">/{summary.total_agentes}</span>
            </strong>
            <span className="metric-caption">
              {t('networks.kpis.connectedIn', {
                minutes: Math.round(summary.ventana_de_vida / 60),
              })}
            </span>
          </article>
        </div>
      )}

      <div className="chart-grid">
        <section className="content-card" aria-labelledby="networks-ports">
          <p className="eyebrow">{t('networks.charts.portsEyebrow')}</p>
          <h2 id="networks-ports">{t('networks.charts.portsTitle')}</h2>
          <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
            <EChart
              option={puertos}
              height={220}
              ariaLabel={t('networks.charts.portsAria', { total: summary?.total_puertos ?? 0 })}
            />
          </Suspense>
          <p className="visually-hidden">
            {t('networks.charts.portsTitle')}:{' '}
            {t('networks.charts.portsAria', { total: summary?.total_puertos ?? 0 })}
          </p>
        </section>

        <section className="content-card" aria-labelledby="networks-status">
          <p className="eyebrow">{t('networks.charts.statusEyebrow')}</p>
          <h2 id="networks-status">{t('networks.charts.statusTitle')}</h2>
          <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
            <EChart
              option={estado}
              height={220}
              ariaLabel={t('networks.charts.statusAria', {
                done: summary?.por_estado.COMPLETED ?? 0,
              })}
            />
          </Suspense>
        </section>

        <section className="content-card" aria-labelledby="networks-trend">
          <p className="eyebrow">{t('networks.charts.trendEyebrow')}</p>
          <h2 id="networks-trend">{t('networks.charts.trendTitle')}</h2>
          <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
            <EChart
              option={porDia}
              height={220}
              ariaLabel={t('networks.charts.trendAria', {
                total: summary?.por_dia.reduce((suma, dia) => suma + dia.escaneos, 0) ?? 0,
              })}
            />
          </Suspense>
        </section>
      </div>

      <AgentPanel onRegistered={refrescar} onRevoked={refrescar} resumen={summary} />

      <div className="content-card">
        <div className="section-header">
          <h2>{t('networks.scans.title')}</h2>
          <span className="badge">{total}</span>
        </div>

        <div className="filter-bar">
          <div className="filter-field filter-field-search">
            <label htmlFor="networks-search">{t('networks.filters.search')}</label>
            <div className="search-field">
              <Search size={16} aria-hidden="true" />
              <input
                id="networks-search"
                type="search"
                value={search}
                onChange={(evento) => setSearch(evento.target.value)}
                placeholder={t('networks.filters.searchPlaceholder')}
              />
            </div>
          </div>
          <div className="filter-field">
            <label htmlFor="networks-status-filter">{t('networks.filters.status')}</label>
            <select
              id="networks-status-filter"
              value={status}
              onChange={(evento) => setStatus(evento.target.value as AgentJobStatus | 'todos')}
            >
              <option value="todos">{t('networks.filters.allStatuses')}</option>
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
              <Globe size={20} />
            </span>
            <h3>{t('networks.errors.loadFailed')}</h3>
            <button type="button" className="secondary-button" onClick={refrescar}>
              {t('networks.refresh')}
            </button>
          </div>
        ) : filtrados.length === 0 && !loading ? (
          <div className="empty-card">
            <span className="empty-card-mark" aria-hidden="true">
              <Network size={20} />
            </span>
            <h2>{hayFiltros ? t('networks.empty.filtered') : t('networks.empty.title')}</h2>
            <p>{hayFiltros ? t('networks.empty.filteredBody') : t('networks.empty.body')}</p>
          </div>
        ) : (
          <>
            <div className="table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('networks.table.cidr')}</th>
                    <th>{t('networks.table.hosts')}</th>
                    <th>{t('networks.table.topPorts')}</th>
                    <th>{t('networks.table.addresses')}</th>
                    <th>{t('networks.table.status')}</th>
                    <th>{t('networks.table.date')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filtrados.map((trabajo) => {
                    const resultado = trabajo.result as NetworkScanResult | null
                    const hosts = Array.isArray(resultado?.hosts) ? resultado.hosts : []
                    // Los puertos mas repetidos de la fila, no todos: una celda con treinta
                    // numeros no se lee y la que importa, la repetida, se pierde en ella.
                    const cuenta = new Map<number, number>()
                    for (const host of hosts) {
                      for (const puerto of host.puertos) {
                        cuenta.set(puerto.puerto, (cuenta.get(puerto.puerto) ?? 0) + 1)
                      }
                    }
                    const principales = [...cuenta.entries()]
                      .sort((a, b) => b[1] - a[1] || a[0] - b[0])
                      .slice(0, 4)
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
                          {typeof resultado?.hosts_con_puertos === 'number'
                            ? resultado.hosts_con_puertos
                            : '—'}
                        </td>
                        <td>
                          {principales.length > 0 ? (
                            <span className="package-chips">
                              {principales.map(([puerto, veces]) => (
                                <span key={puerto} className="chip mono">
                                  {puerto}
                                  {veces > 1 && <span className="chip-count">×{veces}</span>}
                                </span>
                              ))}
                            </span>
                          ) : (
                            <span className="cell-muted">—</span>
                          )}
                        </td>
                        <td className="mono">
                          {typeof resultado?.direcciones_analizadas === 'number'
                            ? resultado.direcciones_analizadas
                            : '—'}
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
              {t('networks.table.count', { shown: filtrados.length, total })}
            </p>
          </>
        )}
      </div>

      {scanOpen && (
        <ScanForm
          kind="NETWORK_SCAN"
          onClose={() => setScanOpen(false)}
          onCreated={() => {
            setScanOpen(false)
            refrescar()
          }}
        />
      )}

      {detail && <JobDetailPanel job={detail} kind="NETWORK_SCAN" onClose={() => setDetail(null)} />}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Los gráficos
// --------------------------------------------------------------------------- //

/** Puertos abiertos, de más a menos frecuente. */
function puertosOption(
  summary: AgentSummary | null,
  palette: ReturnType<typeof readChartPalette>
): ChartOption {
  const entradas = Object.entries(summary?.puertos_por_numero ?? {})
    .map(([puerto, cuenta]) => ({ puerto, cuenta }))
    .sort((a, b) => b.cuenta - a.cuenta || a.puerto.localeCompare(b.puerto))
    .slice(0, 10)
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: palette.surface,
      borderColor: palette.secondary,
      textStyle: { color: palette.primary },
    },
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category',
      data: entradas.map((entrada) => entrada.puerto),
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
        type: 'bar',
        data: entradas.map((entrada) => ({
          value: entrada.cuenta,
          itemStyle: { color: entrada.puerto === 'otros' ? palette.secondary : palette.accent },
        })),
        barMaxWidth: 28,
        label: {
          show: true,
          position: 'top',
          color: palette.secondary,
          fontSize: 10,
        },
      },
    ],
  }
}

/** Reparto por estado, en torta. */
function estadoOption(
  summary: AgentSummary | null,
  palette: ReturnType<typeof readChartPalette>
): ChartOption {
  const porEstado = summary?.por_estado ?? {}
  const colores: Record<string, string> = {
    COMPLETED: palette.accent,
    FAILED: palette.critical,
    RUNNING: palette.low,
    CLAIMED: palette.medium,
    QUEUED: palette.secondary,
  }
  const datos = Object.entries(porEstado)
    .filter(([, valor]) => valor > 0)
    .map(([clave, valor]) => ({
      name: clave,
      value: valor,
      itemStyle: { color: colores[clave] ?? palette.secondary },
    }))
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
        label: { show: false },
        data: datos,
      },
    ],
  }
}

/** Escaneos por día. */
function serieOption(
  summary: AgentSummary | null,
  palette: ReturnType<typeof readChartPalette>
): ChartOption {
  const dias = summary?.por_dia ?? []
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: palette.surface,
      borderColor: palette.secondary,
      textStyle: { color: palette.primary },
    },
    legend: { show: true, bottom: 0, textStyle: { color: palette.secondary, fontSize: 11 } },
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
