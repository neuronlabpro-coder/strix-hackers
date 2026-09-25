import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { GaugeChart, PieChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

echarts.use([GaugeChart, PieChart, LegendComponent, TooltipComponent, CanvasRenderer])

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
