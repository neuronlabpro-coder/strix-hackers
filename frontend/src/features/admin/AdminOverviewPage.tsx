import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getAdminOverview } from '../../lib/adminApi'
import { formatCount, formatCredits, formatDateTime, formatUsd } from '../../lib/format'
import type { AdminMetric } from '../../types/api'
import { useAuth } from '../auth/useAuth'

interface OverviewSnapshot {
  /** Identifica al sondeo al que pertenecen estos datos. Ver `useAdminPage`. */
  key: string
  metrics: AdminMetric[]
  status: 'healthy' | 'degraded' | null
  checkedAt: string | null
  generatedAt: string | null
  failed: boolean
}

/**
 * Resumen global de la plataforma.
 *
 * ## Por qué las métricas llegan con su formato desde el servidor
 *
 * El backend manda `format: 'currency' | 'credits' | 'count'` junto a cada valor. Podría
 * haberlo hecho al revés, que el panel supiera que el MRR va en dólares y los recuentos
 * no, y la diferencia se vería el día que un valor llegue en centavos: la tarjeta
 * mostraría «250» donde debía decir «$2,50». Al venir el formato con el valor, el panel no
 * tiene ningún número mágico que pueda equivocarse.
 *
 * ## Por qué el estado lleva la clave del sondeo
 *
 * «Cargando» se deriva de "no tengo la respuesta que pedí" en vez de marcarse antes de
 * empezar. Marcándolo a mano, un efecto que se limpia antes de resolver deja la vista
 * colgada en «cargando» para siempre, y no hay forma de que la pantalla vuelva.
 */
export function AdminOverviewPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()
  const [reloadToken, setReloadToken] = useState(0)
  const [snapshot, setSnapshot] = useState<OverviewSnapshot | null>(null)

  const isSuperuser = user?.is_superuser === true
  const isReady = token !== null && isSuperuser
  const requestKey = `${token}:${reloadToken}`

  useEffect(() => {
    if (token === null || !isSuperuser) {
      return
    }
    let isActive = true
    void getAdminOverview(token)
      .then((overview) => {
        if (!isActive) {
          return
        }
        setSnapshot({
          key: requestKey,
          metrics: overview.metrics,
          status: overview.infrastructure.status,
          checkedAt: overview.infrastructure.checked_at,
          generatedAt: overview.generated_at,
          failed: false,
        })
      })
      .catch(() => {
        if (isActive) {
          // El fallo no vacía las métricas ya cargadas. Un resumen que se borra al
          // perder la conexión informa de "no hay datos", que es una conclusión falsa
          // sobre la plataforma.
          setSnapshot((current) => ({
            key: requestKey,
            metrics: current?.metrics ?? [],
            status: current?.status ?? null,
            checkedAt: current?.checkedAt ?? null,
            generatedAt: current?.generatedAt ?? null,
            failed: true,
          }))
        }
      })
    return () => {
      isActive = false
    }
  }, [isSuperuser, reloadToken, requestKey, token])

  const refresh = useCallback(() => {
    setReloadToken((current) => current + 1)
  }, [])

  const isCurrent = snapshot !== null && snapshot.key === requestKey
  const metrics = isCurrent ? snapshot.metrics : []
  const isLoading = isReady && !isCurrent
  const hasFailed = isCurrent && snapshot.failed

  if (hasFailed && metrics.length === 0) {
    return (
      <section className="page-section">
        <div className="empty-card">
          <span className="empty-card-mark" aria-hidden="true">
            <ShieldAlert size={20} />
          </span>
          <h2>{t('states.error')}</h2>
          <button className="secondary-button" type="button" onClick={refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="page-section" aria-labelledby="admin-overview-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="admin-overview-title">{t('overview.title')}</h1>
          <p className="page-description">{t('overview.description')}</p>
        </div>
        <div className="page-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={refresh}
            disabled={isLoading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('overview.refresh')}</span>
          </button>
        </div>
      </div>

      {isLoading && metrics.length === 0 ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : metrics.length === 0 ? (
        <div className="empty-card">
          <p>{t('overview.empty')}</p>
        </div>
      ) : (
        <div className="metric-grid">
          {metrics.map((metric) => (
            <MetricCard key={metric.key} metric={metric} />
          ))}
        </div>
      )}

      {hasFailed && metrics.length > 0 ? (
        <p className="chart-empty admin-inline-warning" role="status">
          {t('states.error')}
        </p>
      ) : null}

      {isCurrent && snapshot.status !== null ? (
        <section className="content-card" aria-labelledby="admin-health-title">
          <p className="eyebrow">{t('health.title')}</p>
          <h2 id="admin-health-title">{t('health.caption')}</h2>
          <p className="chart-empty">
            {t(`health.status.${snapshot.status}`)} · {t('health.checkedAt')}{' '}
            {snapshot.checkedAt === null ? '—' : formatDateTime(snapshot.checkedAt)}
          </p>
        </section>
      ) : null}

      {isCurrent && snapshot.generatedAt !== null ? (
        <p className="chart-empty">
          {t('overview.generatedAt')} {formatDateTime(snapshot.generatedAt)}
        </p>
      ) : null}
    </section>
  )
}

function MetricCard({ metric }: { metric: AdminMetric }) {
  const { t } = useTranslation('admin')
  return (
    <article className="metric-card">
      <p className="eyebrow">{t(`metrics.${metric.key}`)}</p>
      {/*
        El símbolo monetario y los separadores los pone `Intl` según el idioma activo, así
        que la misma cifra se ve «$1.234,00» en español y «$1,234.00» en inglés. La unidad
        de créditos se añade aparte porque `Intl` no la lleva, y «500 créditos» se lee
        mejor que «500,00 credits», que parece una cantidad con decimales.
      */}
      <p className="metric-value">
        {formatMetricValue(metric)}
        {metric.format === 'credits' ? (
          <span className="metric-unit"> {t('metrics.creditsUnit')}</span>
        ) : null}
      </p>
      <p className="metric-hint">{t(metric.hint_key)}</p>
    </article>
  )
}

function formatMetricValue(metric: AdminMetric): string {
  if (metric.format === 'count') {
    return formatCount(Number(metric.value))
  }
  if (metric.format === 'currency') {
    return formatUsd(metric.value)
  }
  return formatCredits(metric.value)
}
