/**
 * Los dos gráficos de la pantalla de issues.
 *
 * ## Por qué viven en un componente y no en `IssuesPage`
 *
 * Porque `IssuesPage` ya lleva la tabla, el tablero, la tira de severidad y cuatro filtros, y
 * añadirle dos `EChart` inline la dejaría en más de trescientas líneas con dosources de
 * `Suspense`, dos `useMemo` de esquemas y un bloque de imports que solo existen para el
 * gráfico. Separado, el esquema se puede probar y la tabla se puede leer.
 *
 * ## Por qué **no** se dibujan cuando no hay nada
 *
 * Porque un gráfico de ceros es ruido: cinco barras de宽度 cero, o una línea plana en el suelo,
 * ocupan una tarjeta entera para decir «no hay nada». Cuando el conjunto filtrado está vacío
 * sale el estado vacío con su texto, que es la misma decisión que ya se tomó en el dashboard
 * para la distribución por severidad.
 *
 * ## Por qué los ejes no llevan nombres literales
 *
 * Porque el eje vertical de un reparto por severidad **es** la etiqueta traducida de cada
 * fila. No hay un título de eje que traducir: hay cinco palabras que quien llama pasa ya
 * resueltas desde `t('severityCounts.CRITICAL')`.
 */

import { Suspense, lazy, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

import type { ChartOption } from '../../charts/EChart'
import { opcionBarrasReparto, puntosDeEstado, puntosDeSeveridad } from '../../charts/opciones'
import { readChartPalette } from '../../charts/palette'
import type { SeverityCount, StatusCount } from '../../types/api'

// Apache ECharts entra en su propio chunk: la tabla y el login no lo descargan. El `lazy` es la
// misma decisión que en `DashboardPage` y en `NetworksPage`, y por el mismo motivo: el canvas
// pesa lo que pesa y no tiene sentido cargarlo para ver una tabla.
const EChart = lazy(() =>
  import('../../charts/EChart').then((module) => ({ default: module.EChart })),
)

export interface IssuesChartsProps {
  severityCounts: readonly SeverityCount[]
  statusCounts: readonly StatusCount[]
  /**
   * Cuántos hallazgos casan con el filtro activo.
   *
   * Se pasa y no se deduce de los `severityCounts`, porque hay un caso en que los dos no
   * coinciden: cuando el servidor responde sin los desgloses, `total` viene bien y el desglose
   * viene vacío. Un gráfico con cinco ceros al lado de una tabla con cinco filas afirma lo
   * contrario de la verdad, así que en ese caso no se pinta nada. Ver la guarda del componente.
   */
  total: number
}

export function IssuesCharts({ severityCounts, statusCounts, total }: IssuesChartsProps) {
  const { t } = useTranslation('issues')
  const { i18n } = useTranslation()
  const locale = i18n.language

  const palette = useMemo(() => readChartPalette(), [])

  /**
   * El formateador de números se construye una vez por idioma, no en cada fila.
   *
   * Porque `formatearEntero` crea un `Intl.NumberFormat` en cada llamada —y son cinco filas
   * por gráfico más tres llamadas en los `ariaLabel`—, y `Intl.NumberFormat` es de las
   * construcciones más caras de crear en un navegador. Con el idioma en la clave del `useMemo`
   * cambia solo cuando cambia el idioma, que es exactamente cuando debe cambiar.
   */
  const formatea = useMemo(() => {
    const format = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 })
    return (valor: number) => format.format(valor)
  }, [locale])

  /**
   * Las etiquetas de severidad salen del **mismo** namespace que la píldora del filtro y la
   * columna de la tabla, no de un grupo propio de la gráfica.
   *
   * Porque son la misma palabra en los tres sitios. Duplicarlas dentro de `charts.*` haría que
   * un día la tabla dijera «Crítica» y el gráfico «Crítico», y el fallo aparecería solo al
   * comparar dos zonas de la misma pantalla, que es lo más difícil que hay para verlo.
   */
  const severidades = useMemo(
    () => puntosDeSeveridad(severityCounts, palette, (clave) => t(`severityCounts.${clave}`)),
    [severityCounts, palette, t],
  )

  const estados = useMemo(
    () => puntosDeEstado(statusCounts, palette, (clave) => t(`status.${clave}`)),
    [statusCounts, palette, t],
  )

  const totalSeveridad = useMemo(
    () => severidades.reduce((suma, punto) => suma + punto.total, 0),
    [severidades],
  )

  const opcionSeveridad = useMemo(
    () =>
      opcionBarrasReparto(severidades, palette, {
        serie: t('charts.severityLegend'),
        total: t('charts.severityAria', { total: formatea(totalSeveridad) }),
        vacio: t('charts.empty'),
      }) as ChartOption,
    [severidades, palette, t, totalSeveridad, formatea],
  )

  const totalEstado = useMemo(
    () => estados.reduce((suma, punto) => suma + punto.total, 0),
    [estados],
  )

  const opcionEstado = useMemo(
    () =>
      opcionBarrasReparto(estados, palette, {
        serie: t('charts.statusLegend'),
        total: t('charts.statusAria', { total: formatea(totalEstado) }),
        vacio: t('charts.empty'),
      }) as ChartOption,
    [estados, palette, t, totalEstado, formatea],
  )

  /*
   * Los gráficos se ocultan si **el desglose no describe lo que hay**, no solo si no hay nada.
   *
   * Porque el fallo más caro de un panel de seguridad es que la gráfica afirme lo contrario de
   * lo que dice la tabla. Con cinco hallazgos listados y las cinco barras a cero, la pantalla
   * dice dos cosas a la vez, y la que se lee primero —la de arriba, la de color— es la falsa.
   *
   * Y ese caso ocurre de verdad sin que falte ni un solo hallazgo: cuando el servidor responde
   * sin los desgloses, que es lo que pasa con un backend sin reiniciar. Por eso la condición
   * mira **los desgloses** y no `total`: `total` llega bien en los dos casos, y el único indicio
   * de que el reparto no se ha podido leer es que no haya llegado ninguna fila.
   */
  if (severityCounts.length === 0 || statusCounts.length === 0 || total === 0) {
    return null
  }

  return (
    <div className="chart-grid chart-grid-two">
      <section className="content-card" aria-labelledby="issues-severity-chart">
        <p className="eyebrow">{t('charts.severityEyebrow')}</p>
        <h2 id="issues-severity-chart">{t('charts.severityTitle')}</h2>
        <Suspense fallback={<div className="chart-placeholder" style={{ height: 200 }} />}>
          <EChart
            option={opcionSeveridad}
            height={200}
            ariaLabel={t('charts.severityAria', {
              total: formatea(totalSeveridad),
            })}
          />
        </Suspense>
        <DescripcionLista puntos={severidades} formatear={formatea} />
      </section>

      <section className="content-card" aria-labelledby="issues-status-chart">
        <p className="eyebrow">{t('charts.statusEyebrow')}</p>
        <h2 id="issues-status-chart">{t('charts.statusTitle')}</h2>
        <Suspense fallback={<div className="chart-placeholder" style={{ height: 200 }} />}>
          <EChart
            option={opcionEstado}
            height={200}
            ariaLabel={t('charts.statusAria', { total: formatea(totalEstado) })}
          />
        </Suspense>
        <DescripcionLista puntos={estados} formatear={formatea} />
      </section>
    </div>
  )
}

/**
 * Los mismos números del gráfico, en texto, para quien no ve el canvas.
 *
 * ## Por qué está debajo y no dentro de `ariaLabel`
 *
 * Porque `ariaLabel` es una frase que hay que mantener corta para que un lector de pantalla no
 * la lea como un bloque de texto, y cinco filas de datos son exactamente lo que un lector de
 * pantalla **quiere** oírse una por una. El `canvas` no pinta texto seleccionable, así que sin
 * esto el gráfico sería una imagen sin descripción: se anunciaría como «imagen» y punto.
 */
function DescripcionLista({
  puntos,
  formatear,
}: {
  puntos: Array<{ clave: string; etiqueta: string; total: number }>
  formatear: (valor: number) => string
}) {
  return (
    <ul className="visually-hidden">
      {puntos.map((punto) => (
        <li key={punto.clave}>
          {punto.etiqueta}: {formatear(punto.total)}
        </li>
      ))}
    </ul>
  )
}