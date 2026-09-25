/**
 * Paleta de los gráficos.
 *
 * `chartPalette` es la definición explícita de la paleta de visualización: los
 * cuatro colores que comparte la interfaz más una rampa de severidad para el
 * grafico de datos. `readChartPalette()` resuelve en runtime los colores que la
 * interfaz ya define como tokens CSS, de modo que el fondo del grafico nunca
 * sea distinto de la tarjeta que lo contiene, y usa esta paleta para la rampa
 * de severidad.
 */
export const chartPalette = {
  accent: '#17A163',
  primary: '#EDEDED',
  secondary: '#8A8F8A',
  surface: '#161718',
  background: '#0F1112',
  critical: '#EF4444',
  high: '#F97316',
  medium: '#F59E0B',
  low: '#3B82F6',
  info: '#8A8F8A',
} as const

export type ChartPalette = { [K in keyof typeof chartPalette]: string }

export type SeverityColorKey = 'critical' | 'high' | 'medium' | 'low' | 'info'

function readToken(name: string, fallback: string): string {
  if (typeof window === 'undefined' || typeof getComputedStyle !== 'function') {
    return fallback
  }
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value.length > 0 ? value : fallback
}

/**
 * Los colores de interfaz se leen de los tokens del tema; la severidad usa la
 * paleta de visualizacion. Si algun dia el tema define `--chart-surface` o
 * `--chart-background`, esos mandan sobre los tokens de interfaz.
 */
export function readChartPalette(): ChartPalette {
  return {
    accent: readToken('--color-accent', chartPalette.accent),
    primary: readToken('--color-primary', chartPalette.primary),
    secondary: readToken('--color-secondary', chartPalette.secondary),
    surface: readToken('--chart-surface', readToken('--color-surface', chartPalette.surface)),
    background: readToken(
      '--chart-background',
      readToken('--color-background', chartPalette.background),
    ),
    critical: readToken('--chart-critical', chartPalette.critical),
    high: readToken('--chart-high', chartPalette.high),
    medium: readToken('--chart-medium', chartPalette.medium),
    low: readToken('--chart-low', chartPalette.low),
    info: readToken('--chart-info', chartPalette.info),
  }
}
