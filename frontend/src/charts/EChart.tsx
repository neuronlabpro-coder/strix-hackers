import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, GaugeChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

/*
 * Qué se registra aquí y por qué importa.
 *
 * ## Por qué `BarChart` y `GridComponent` estaban sin registrar
 *
 * Porque `echarts/core` es una versión modular: importar el paquete no registra nada. Si un tipo de
 * gráfico o un componente no aparece en `echarts.use()`, la pantalla **no da error** —el `<canvas>`
 * se queda en blanco y en la consola aparece un aviso `[ECharts] Bar series ... not imported`—.
 * Ese es el peor modo de fallo posible: la aplicación está sana, los tests pasan, y lo único que
 * lo delata es mirar la pantalla.
 *
 * ## Por qué `LineChart` entra ahora
 *
 * Porque sin ella no hay forma de dibujar una serie temporal, y el panel necesita evolución: altas
 * por semana, créditos gastados por día, hallazgos acumulados. Con `BarChart` sola sí se puede
 * apilar —`stack: 'total'` funciona en barras—, pero no se puede trazar una línea, y por tanto
 * tampoco una línea apilada.
 *
 * ## Qué NO se registra aquí
 *
 * Ningún tema, ningún `dataZoom` y ningún `dataset`. Se registran **a demanda**, en el módulo que
 * los use, y no aquí: si este fichero se convierte en un catálogo de intenciones, deja de decir qué
 * usa la aplicación. Si un gráfico nuevo necesita más, se registra con él.
 *
 * ## Por qué `CanvasRenderer` y no `SVGRenderer`
 *
 * Porque los datos llegan por paginación y un gráfico de barras apiladas con cientos de categorías
 * pinta muchas más primitivas de las que un SVG con un nodo por dato aguanta. El coste es que el
 * texto deja de ser seleccionable, y para eso está la descripción accesible de `ariaLabel`.
 */
echarts.use([
  BarChart,
  GaugeChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

export type ChartOption = EChartsCoreOption

export interface EChartProps {
  option: ChartOption
  height: number
  /** Descripción accesible del gráfico para lectores de pantalla. */
  ariaLabel: string
}

export function EChart({ option, height, ariaLabel }: EChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) {
      return
    }

    const chart = echarts.init(container, undefined, { renderer: 'canvas' })
    chart.setOption(option, true)

    const handleResize = () => chart.resize()
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.dispose()
    }
  }, [option])

  return (
    <div
      ref={containerRef}
      className="chart-canvas"
      style={{ height: `${height}px` }}
      role="img"
      aria-label={ariaLabel}
    />
  )
}
