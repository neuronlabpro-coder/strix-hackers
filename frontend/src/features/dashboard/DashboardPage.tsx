import { Suspense, lazy, useMemo, useState } from 'react'
import { ArrowRight, CodeXml } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import type { ChartOption } from '../../charts/EChart'
import { readChartPalette, type ChartPalette, type SeverityColorKey } from '../../charts/palette'
import type { DashboardRepository, VulnerabilitySeverity } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { ConnectedRepositoryToggle } from '../repositories/RepositoryReviewToggle'
import { OnboardingChecklist } from './OnboardingChecklist'
import { useDashboardSummary } from './useDashboardSummary'

// Apache ECharts entra en su propio chunk: el shell y el login no lo descargan.
const EChart = lazy(() =>
  import('../../charts/EChart').then((module) => ({ default: module.EChart })),
)

const SEVERITY_COLORS: Record<VulnerabilitySeverity, SeverityColorKey> = {
  CRITICAL: 'critical',
  HIGH: 'high',
  MEDIUM: 'medium',
  LOW: 'low',
  INFO: 'info',
}

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
          lineStyle: { width: 14, color: [[1, palette.surface]] },
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

function distributionOption(
  distribution: { severity: VulnerabilitySeverity; total: number }[],
  palette: ChartPalette,
): ChartOption {
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: palette.surface,
      borderColor: palette.secondary,
      textStyle: { color: palette.primary, fontFamily: 'JetBrains Mono, monospace' },
    },
    legend: {
      bottom: 0,
      icon: 'square',
      textStyle: { color: palette.secondary, fontSize: 11 },
    },
    series: [
      {
        type: 'pie',
        radius: ['58%', '86%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: palette.surface, borderWidth: 2 },
        label: { show: false },
        data: distribution
          .filter((item) => item.total > 0)
          .map((item) => ({
            name: item.severity,
            value: item.total,
            itemStyle: { color: palette[SEVERITY_COLORS[item.severity]] },
          })),
      },
    ],
  }
}

function formatPercent(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 }).format(value)
}

export function DashboardPage() {
  const { t, i18n } = useTranslation('dashboard')
  const { t: tCommon } = useTranslation('common')
  const { t: tRepositories } = useTranslation('repositories')
  const { organizations, selectedOrganizationId } = useAuth()
  const { summary, isLoading, loadFailed, refresh } = useDashboardSummary()
  const [toggleFailed, setToggleFailed] = useState(false)
  const locale = i18n.language

  const selectedOrganization = organizations.find(
    (organization) => organization.id === selectedOrganizationId,
  )
  const workspaceName = selectedOrganization?.name ?? tCommon('noWorkspace')

  const gauge = useMemo(() => {
    const palette = readChartPalette()
    return gaugeOption(
      summary?.security_score ?? 0,
      t('charts.gaugeValue', { score: summary?.security_score ?? 0 }),
      palette,
    )
  }, [summary?.security_score, t])

  const distribution = useMemo(
    () => distributionOption(summary?.severity_distribution ?? [], readChartPalette()),
    [summary?.severity_distribution],
  )

  const hasFindings = (summary?.severity_distribution ?? []).some((item) => item.total > 0)

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
              <span className="metric-caption">{t('kpis.monitoredRepositories')}: {summary.repositories_monitored}</span>
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
                      option={distribution}
                      height={220}
                      ariaLabel={t('charts.distributionCaption')}
                    />
                  </Suspense>
                  <ul className="visually-hidden">
                    {summary.severity_distribution
                      .filter((item) => item.total > 0)
                      .map((item) => (
                        <li key={item.severity}>
                          {item.severity}: {item.total}
                        </li>
                      ))}
                  </ul>
                </>
              ) : (
                <p className="chart-empty">{t('charts.distributionEmpty')}</p>
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
