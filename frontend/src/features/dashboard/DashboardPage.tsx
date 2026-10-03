/**
 * `/dashboard`: la postura del workspace de un vistazo.
 *
 * ## Por qué hay cuatro cifras **y** tres gráficos, y no unas en vez de otras
 *
 * Porque responden a preguntas distintas y ninguna se deduce de la otra. Un `24` de hallazgos
 * abiertos no dice si los tres críticos son de hoy o de hace seis meses; un `72%` de fix rate
 * no dice cuál es la severidad que se está ignorando. La cifra grande es un dato puntual y el
 * gráfico una distribución: se complementan, y quitar cualquiera de los dos deja media
 * pregunta sin respuesta.
 *
 * ## Por qué las cuatro cifras se quedan
 *
 * Porque son el ancla. Un gráfico sin un número al lado obliga a estimar el área para
 * contestarse «¿tengo mucho o poco?», y en riesgo eso no se estima: se lee. El error más caro
 * de un panel de seguridad es que alguien infiera su postura en lugar de leerla.
 *
 * ## Por qué la distribución por severidad son barras y no una torta
 *
 * Ver `distributionOption`. Con cinco porciones, un sector compara áreas —que el ojo no sabe
 * leer— y su área es un porcentaje de una circunferencia, no una longitud. Una barra con el
 * número escrito al lado compara longitudes **y** dice la cifra exacta, y su etiqueta de eje
 * es el nombre de la severidad, que es lo que hace que el color no tenga que ir en solitario.
 */

import { Suspense, lazy, useMemo, useState } from 'react'
import { ArrowRight, CodeXml } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import type { ChartOption } from '../../charts/EChart'
import {
  SEVERITY_KEYS,
  readChartPalette,
  type ChartPalette,
  type SeverityColorKey,
} from '../../charts/palette'
import { formatearEntero, opcionBarrasReparto, opcionLineaApilada, puntosDeSeveridad } from '../../charts/opciones'
import type {
  DashboardRepository,
  FindingsTrendPoint,
  VulnerabilitySeverity,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { ConnectedRepositoryToggle } from '../repositories/RepositoryReviewToggle'
import { OnboardingChecklist } from './OnboardingChecklist'
import { useDashboardSummary } from './useDashboardSummary'

// Apache ECharts entra en su propio chunk: el shell y el login no lo descargan.
const EChart = lazy(() =>
  import('../../charts/EChart').then((module) => ({ default: module.EChart })),
)

/**
 * La clave del token de cada severidad, en el orden de `SEVERITY_KEYS`.
 *
 * ## Por qué es `Record<SeverityColorKey, string>` y no `Record<string, SeverityColorKey>`
 *
 * Porque el dominio son cinco valores cerrados y el tipo lo dice: si mañana el servidor
 * devuelve `BLOCKER`, el acceso a `SEVERITY_OF_KEY['BLOCKER']` deja de compilar y se descubre
 * mirando este fichero, no leyendo un `undefined` en una captura. La decisión de qué clave del
 * modelo corresponde a qué token tiene que estar escrita en un solo sitio —si dos pantallas la
 * escriben distinto, el rojo de un gráfico puede ser el alto del de al lado— y por eso vive
 * aquí: el módulo de esquemas recibe los puntos ya coloreados, y quien los colorea es quien
 * sabe cómo se llaman las cosas en el modelo.
 */
const SEVERITY_OF_KEY: Record<SeverityColorKey, VulnerabilitySeverity> = {
  critical: 'CRITICAL',
  high: 'HIGH',
  medium: 'MEDIUM',
  low: 'LOW',
  info: 'INFO',
}

/**
 * El medidor de salud: un arco de 240 grados con el índice dentro.
 *
 * ## Por qué el fondo del arco usa `--color-surface-elevated` y no `--color-surface`
 *
 * Porque `surface` es **la tarjeta donde vive el gráfico**: un anillo del mismo color que el
 * fondo se ve como una barra vacía que se ha quedado a medias, no como el resto del medidor. Un
 * paso por encima —`surface-elevated`— se lee como «pista» y es el mismo paso que usan los
 * inputs, de modo que el objeto habla el mismo idioma que el resto del panel.
 */
function gaugeOption(score: number, scoreLabel: string, palette: ChartPalette): ChartOption {
  return {
    backgroundColor: 'transparent',
    series: [
      {
        type: 'gauge',
        startAngle: 210,
        endAngle: -30,
        min: 0,
        max: 100,
        radius: '82%',
        center: ['50%', '58%'],
        progress: {
          show: true,
          width: 14,
          roundCap: false,
          itemStyle: { color: palette.accent },
        },
        axisLine: {
          lineStyle: { width: 14, color: [[1, palette.surfaceElevated]] },
        },
        pointer: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        anchor: { show: false },
        title: { show: false },
        detail: {
          valueAnimation: false,
          offsetCenter: [0, '0%'],
          color: palette.primary,
          formatter: scoreLabel,
        },
        data: [{ value: score, name: '' }],
      },
    ],
  }
}

function formatPercent(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 }).format(value)
}

/** El eje de días de la serie, en `MM-DD`: el año no aporta en una ventana de un mes. */
function etiquetaDia(dia: string): string {
  return dia.slice(5)
}

export function DashboardPage() {
  const { t, i18n } = useTranslation('dashboard')
  const { t: tCommon } = useTranslation('common')
  const { t: tIssues } = useTranslation('issues')
  const { t: tRepositories } = useTranslation('repositories')
  const { organizations, selectedOrganizationId } = useAuth()
  const { summary, isLoading, loadFailed, refresh } = useDashboardSummary()
  const [toggleFailed, setToggleFailed] = useState(false)
  const locale = i18n.language

  const selectedOrganization = organizations.find(
    (organization) => organization.id === selectedOrganizationId,
  )
  const workspaceName = selectedOrganization?.name ?? tCommon('noWorkspace')

  const palette = useMemo(() => readChartPalette(), [])

  const gauge = useMemo(
    () =>
      gaugeOption(
        summary?.security_score ?? 0,
        t('charts.gaugeValue', { score: summary?.security_score ?? 0 }),
        palette,
      ),
    [summary?.security_score, t, palette],
  )

  /*
 * El `?? []` va **dentro** del `useMemo`, no en una constante intermedia.
 *
 * Porque una constante `SIN_HALLAZGOS` evaluada en el cuerpo del componente crearía un array
 * nuevo en cada render mientras el resumen aún no ha llegado, y el `useMemo` de abajo dependería
 * de él: se invalidaría en cada render y la memoización no memoizaría nada. Es el mismo motivo
 * por el que `NetworksPage` declara `SIN_TRABAJOS` como constante de **módulo** —una sola
 * vez— y no como literal. Aquí no hace falta constante porque el `??` está dentro del callback,
 * que solo corre cuando las dependencias han cambiado de verdad.
 */
const openFindings = summary?.severity_distribution

  const puntosSeveridad = useMemo(
    () =>
      puntosDeSeveridad(
        (openFindings ?? []).map((item) => ({
          severity: item.severity,
          total: item.total,
        })),
        palette,
        (severity) => tIssues(`severityCounts.${severity}`),
      ),
    [openFindings, palette, tIssues],
  )

  const totalOpen = useMemo(
    () => puntosSeveridad.reduce((suma, punto) => suma + punto.total, 0),
    [puntosSeveridad],
  )

  const opcionSeveridad = useMemo(
    () =>
      opcionBarrasReparto(puntosSeveridad, palette, {
        serie: t('charts.severityLegend'),
        total: t('charts.severityAria', { total: formatearEntero(totalOpen, locale) }),
        vacio: t('charts.distributionEmpty'),
      }) as ChartOption,
    [puntosSeveridad, palette, t, totalOpen, locale],
  )

  /**
   * La serie temporal se arma aquí y no en el esquema.
   *
   * El esquema recibe puntos y series ya listas porque decidir **qué** series se dibujan
   * (las cinco, aunque valgan cero) es una decisión de datos, y la de **cómo** se pintan es
   * una decisión de esquema. Meter la translation y el `Record` dentro de `opciones.ts`
   * obligaría a ese módulo a saber de i18next, que es lo que hace que un módulo de gráficos
   * deje de poder probarse sin montar un provider de traducción.
   */
  const serie = useMemo(() => {
    const trend: FindingsTrendPoint[] = summary?.findings_trend ?? []
    const puntos = trend.map((punto) => ({ dia: etiquetaDia(punto.dia), total: punto.total }))
    const series = SEVERITY_KEYS.map((clave) => ({
      clave,
      nombre: tIssues(`severityCounts.${SEVERITY_OF_KEY[clave]}`),
      valores: trend.map((punto) => punto.por_severidad[SEVERITY_OF_KEY[clave]] ?? 0),
    }))
    return { puntos, series, trend }
  }, [summary?.findings_trend, tIssues])

  const opcionSerie = useMemo(
    () =>
      opcionLineaApilada(serie.puntos, serie.series, palette, {
        total: t('charts.trendAria', {
          total: formatearEntero(
            serie.trend.reduce((suma, punto) => suma + punto.total, 0),
            locale,
          ),
        }),
        eje: t('charts.trendAxis'),
      }) as ChartOption,
    [serie, palette, t, locale],
  )

  const totalSerie = useMemo(
    () => serie.trend.reduce((suma, punto) => suma + punto.total, 0),
    [serie],
  )

  const hasFindings = totalOpen > 0

  return (
    <section className="page-section" aria-labelledby="dashboard-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="dashboard-title">{t('title')}</h1>
          <p className="page-description">{t('welcome', { workspace: workspaceName })}</p>
        </div>
        {summary ? (
          <p className="eyebrow">
            {t('generatedAt', {
              timestamp: new Intl.DateTimeFormat(locale, { timeStyle: 'short' }).format(
                new Date(summary.generated_at),
              ),
            })}
          </p>
        ) : null}
      </div>

      {isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : loadFailed || !summary ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : (
        <>
          {toggleFailed ? (
            <p className="inline-notice inline-notice-warning" role="alert">
              {tRepositories('prReviews.updateFailed')}
            </p>
          ) : null}

          <OnboardingChecklist />

          <div className="metric-grid">
            <article className="metric-card">
              <p className="eyebrow">{t('kpis.securityScore')}</p>
              <strong className="metric-value mono">{summary.security_score}</strong>
              <span className="metric-caption">{t('charts.healthCaption')}</span>
            </article>
            <article className="metric-card">
              <p className="eyebrow">{t('kpis.openIssues')}</p>
              <strong className="metric-value mono">{summary.open_issues}</strong>
              <span className="metric-caption">
                {t('kpis.totalIssues')}: {summary.total_issues}
              </span>
            </article>
            <article className="metric-card">
              <p className="eyebrow">{t('kpis.fixRate')}</p>
              <strong className="metric-value mono">{formatPercent(summary.fix_rate, locale)}</strong>
              <span className="metric-caption">
                {t('kpis.monitoredRepositories')}: {summary.repositories_monitored}
              </span>
            </article>
            <article className="metric-card">
              <p className="eyebrow">{t('kpis.prsReviewed')}</p>
              <strong className="metric-value mono">{summary.prs_reviewed}</strong>
              <span className="metric-caption">
                {t('kpis.prsReviewedHint')} · {t('kpis.pentests')}: {summary.pentests_total}
              </span>
            </article>
          </div>

          <div className="chart-grid">
            <section className="content-card" aria-labelledby="health-title">
              <p className="eyebrow">{t('charts.healthTitle')}</p>
              <h2 id="health-title">{t('charts.healthCaption')}</h2>
              <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
                <EChart
                  option={gauge}
                  height={220}
                  ariaLabel={t('charts.gaugeValue', { score: summary.security_score })}
                />
              </Suspense>
              <p className="visually-hidden">
                {t('charts.healthCaption')}: {t('charts.gaugeValue', { score: summary.security_score })}
              </p>
            </section>

            <section className="content-card" aria-labelledby="distribution-title">
              <p className="eyebrow">{t('charts.distributionTitle')}</p>
              <h2 id="distribution-title">{t('charts.distributionCaption')}</h2>
              {hasFindings ? (
                <>
                  <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
                    <EChart
                      option={opcionSeveridad}
                      height={220}
                      ariaLabel={t('charts.severityAria', {
                        total: formatearEntero(totalOpen, locale),
                      })}
                    />
                  </Suspense>
                  <ul className="visually-hidden">
                    {puntosSeveridad
                      .filter((punto) => punto.total > 0)
                      .map((punto) => (
                        <li key={punto.clave}>
                          {punto.etiqueta}: {formatearEntero(punto.total, locale)}
                        </li>
                      ))}
                  </ul>
                </>
              ) : (
                <p className="chart-empty">{t('charts.distributionEmpty')}</p>
              )}
            </section>

            <section className="content-card" aria-labelledby="trend-title">
              <p className="eyebrow">{t('charts.trendEyebrow')}</p>
              <h2 id="trend-title">{t('charts.trendTitle')}</h2>
              {totalSerie > 0 ? (
                <>
                  <Suspense fallback={<div className="chart-placeholder" style={{ height: 220 }} />}>
                    <EChart
                      option={opcionSerie}
                      height={220}
                      ariaLabel={t('charts.trendAria', {
                        total: formatearEntero(totalSerie, locale),
                      })}
                    />
                  </Suspense>
                  <ul className="visually-hidden">
                    {serie.series
                      .filter((linea) => linea.valores.some((valor) => valor > 0))
                      .map((linea) => (
                        <li key={linea.clave}>
                          {linea.nombre}:{' '}
                          {formatearEntero(
                            linea.valores.reduce((suma, valor) => suma + valor, 0),
                            locale,
                          )}
                        </li>
                      ))}
                  </ul>
                </>
              ) : (
                <p className="chart-empty">{t('charts.trendEmpty')}</p>
              )}
            </section>
          </div>

          <section className="content-card" aria-labelledby="repositories-widget-title">
            <div className="section-header">
              <div>
                <p className="eyebrow">{t('repositoriesWidget.title')}</p>
                <h2 id="repositories-widget-title">{t('repositoriesWidget.caption')}</h2>
              </div>
              <Link className="secondary-button" to="/repositories">
                <span>{t('repositoriesWidget.viewAll')}</span>
                <ArrowRight size={16} aria-hidden="true" />
              </Link>
            </div>
            {summary.repositories.length === 0 ? (
              <p className="chart-empty">{t('repositoriesWidget.empty')}</p>
            ) : (
              <ul className="repository-widget-list">
                {summary.repositories.slice(0, 5).map((repository) => (
                  <RepositoryWidgetRow
                    key={repository.id}
                    repository={repository}
                    onToggleFailure={() => setToggleFailed(true)}
                  />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </section>
  )
}

function RepositoryWidgetRow({
  repository,
  onToggleFailure,
}: {
  repository: DashboardRepository
  onToggleFailure: () => void
}) {
  return (
    <li className="repository-widget-item">
      <span className="provider-cell">
        <CodeXml size={16} aria-hidden="true" />
        <span className="mono">{repository.provider}</span>
      </span>
      <span className="repository-widget-name mono">{repository.full_name}</span>
      <ConnectedRepositoryToggle
        repositoryId={repository.id}
        enabled={repository.pr_reviews_enabled}
        onFailure={onToggleFailure}
      />
    </li>
  )
}