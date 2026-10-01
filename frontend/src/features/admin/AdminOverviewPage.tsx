import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../lib/api'
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
  /**
   * Por qué falló: el estado HTTP y el mensaje del servidor, o `null` si nada falló.
   *
   * ## Por qué se guarda en vez de solo un `failed: true`
   *
   * ## Por qué hace falta
   *
   * Porque un `catch` que se come el error convierte «no puedo cargar la consola» en un
   * misterio: no hay forma de saber si es un `403` por no ser superusuario, un `500` del backend,
   * o un backend que no está levantado. Los tres se pintaban igual, y el botón de «Reintentar»
   * es lo único que se podía hacer: pulsar y rezar.
   *
   * Con el estado y el `detail` del servidor, la pantalla dice qué ha pasado y qué hacer. Es la
   * diferencia entre un aviso y una puerta cerrada sin pomo.
   */
  fallo: { status: number; detalle: string | null } | null
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
/**
 * Qué le decimos a quien mira, según por qué falló.
 *
 * ## Por qué hay cuatro textos y no uno
 *
 * Porque «no se pudo cargar» no dice qué hacer, y las cuatro causas necesitan acciones
 * distintas: una sesión caducada se arregla iniciando sesión, un `403` se arregla con una cuenta
 * de superusuario, un `5xx` mirando el log del backend, y un `0` —que es la señal de que ni
 * siquiera hubo respuesta— arrancando el backend.
 *
 * ## Por qué el `detail` del servidor se enseña tal cual
 *
 * ## Por qué el `detail` va tal cual debajo
 *
 * Porque es el mensaje que FastAPI escribió para este caso concreto, y traducirlo o recortarlo
 * es perder lo único que distingue un fallo de otro. Se enseña en monoespaciado, debajo de la
 * frase que sí es accionable, para que la frase se lea y el detalle se copie.
 *
 * @param fallo El motivo guardado, o `null` si no se sabe.
 * @param t El traductor, para no tener que importarlo aquí.
 */
function descripcionDelFallo(
  fallo: { status: number; detalle: string | null } | null,
  t: (clave: string, valores?: Record<string, unknown>) => string,
): string {
  if (!fallo) return t('overview.failureUnknown')
  if (fallo.status === 0) return t('overview.failureNetwork')
  if (fallo.status === 401) return t('overview.failureSession')
  if (fallo.status === 403) return t('overview.failureForbidden')
  if (fallo.status >= 500) return t('overview.failureServer')
  return t('overview.failureOther', { status: fallo.status })
}

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
          fallo: null,
        })
      })
      .catch((fallo: unknown) => {
        if (!isActive) {
          return
        }
        // El fallo no vacía las métricas ya cargadas. Un resumen que se borra al perder la
        // conexión informa de "no hay datos", que es una conclusión falsa sobre la plataforma.
        //
        // Y el motivo **se guarda**. Antes este `catch` no tenía parámetro: el error se perdía
        // entero, y la pantalla pintaba la misma frase para un `403` que para un `500` o para un
        // backend que no está levantado. Sin el motivo, el único recurso era reintentar.
        const motivo: { status: number; detalle: string | null } =
          fallo instanceof ApiError
            ? { status: fallo.status, detalle: fallo.detalle }
            : {
                status: 0,
                detalle: fallo instanceof Error ? fallo.message : null,
              }
        setSnapshot((current) => ({
          key: requestKey,
          metrics: current?.metrics ?? [],
          status: current?.status ?? null,
          checkedAt: current?.checkedAt ?? null,
          generatedAt: current?.generatedAt ?? null,
          failed: true,
          fallo: motivo,
        }))
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
          {/* Y ahora el motivo, que antes se perdía. Un `403` y un `500` no se arreglan
              pulsando «Reintentar»: uno es que la sesión no vale y el otro es que el servidor
              ha fallado. Pintados por igual, el operador pulsa y no pasa nada. */}
          <p className="section-hint">{descripcionDelFallo(snapshot?.fallo ?? null, t)}</p>
          {snapshot?.fallo?.status ? (
            <p className="mono admin-failure-status">
              {t('overview.failureStatus', { status: snapshot.fallo.status })}
              {snapshot.fallo.detalle ? ` · ${snapshot.fallo.detalle}` : ''}
            </p>
          ) : null}
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
  const { t, i18n } = useTranslation('admin')
  /*
    Las pistas se resuelven quitando el namespace si la clave viene con él.
    *
    * El backend envía `hint_key` **relativa** al namespace `admin` —`metrics.mrrHint`— y por
    * eso esto funciona. Antes venía cualificada —`admin.metrics.mrrHint`— y se buscaba
    * `admin.admin.metrics.mrrHint`: no existía, e i18next devolvía la cadena entera en pantalla.
    *
    * El Backend ya no lo hace, y aun así se quita el prefijo. La razón no es desconfianza sino
    * que la forma de esta clase de fallo es silenciosa y con muy mal aspecto: no hay error, no
    * hay excepción en la consola, y la pantalla parece Translate bien porque la etiqueta —que
    * siempre fue relativa— sí lo está. Un panel de seis cifras correctas con una línea de texto que es un identificador es de las
    * pocas cosas que un revisor
    * no detecta mirando la página. Quitar cuatro caracteres cuesta una línea y hace que un
    * servidor viejo, una caché o un consumidor externo degrade a texto traducido en vez de a
    * claves en crudo.
    */
  const namespace = i18n.resolvedLanguage ?? i18n.language
  const pista = metric.hint_key.startsWith(`${namespace}.`)
    ? metric.hint_key.slice(namespace.length + 1)
    : metric.hint_key
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
      <p className="metric-hint">{t(pista)}</p>
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
