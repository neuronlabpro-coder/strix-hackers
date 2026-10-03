/**
 * Paleta de los gráficos.
 *
 * `chartPalette` es la **copia literal** de los tokens de `index.css`, no una paleta propia.
 *
 * ## Por qué vive en el panel de TypeScript y no en el CSS
 *
 * Porque un `<canvas>` no lee variables CSS: se le pasa un color ya resuelto, y el valor que
 * llega a ECharts es una cadena opaca. La lectura del token tiene que hacerse en JavaScript
 * (`getComputedStyle`), y el CSS solo puede seguir siendo la **fuente** del valor, no su
 * canal. Por eso la copia de aquí es la que se usa y la del CSS es la que manda.
 *
 * ## Por qué los valores están repetidos aquí y no solo leídos
 *
 * Porque `readChartPalette()` necesita un valor de reserva para cuando el token no existe: se
 * llama durante el primer render, y hay navegadores y pruebas que resuelven `getComputedStyle`
 * antes de que el tema haya aplicado sus variables. Si el valor de reserva fuese inventado,
 * ese primer render pintaría un color que no existe en ninguna pantalla y, en el siguiente,
 * saltaría al bueno. Al ser una copia del token, el salto es invisible.
 *
 * Y por qué la copia es **exacta**: si alguien cambia `--color-critical` en el tema y no en
 * aquí, la mitad de los gráficos ve un tono y la otra mitad otro, y la única forma de saber
 * cuál está mal es comparar dos ficheros. `_css_palette.test.ts` existe para que eso no
 * dependa de que alguien se acuerde.
 */
export const chartPalette = {
  accent: '#17a163',
  primary: '#f3f4f6',
  secondary: '#79818f',
  caption: '#a8aeb9',
  surface: '#121316',
  surfaceElevated: '#16181b',
  background: '#0b0c0e',
  border: '#1e2024',
  borderHover: '#2a2d33',
  critical: '#f87171',
  high: '#fb923c',
  medium: '#fbbf24',
  low: '#60a5fa',
  info: '#9ca3af',
} as const

export type ChartPalette = { [K in keyof typeof chartPalette]: string }

/**
 * Las cinco severidades, de más a menos grave.
 *
 * El orden es el de `SeverityEnum` en el modelo y el de `_SEVERITY_ORDER` en los servicios, y
 * tiene que ser el mismo en los tres sitios: es lo que garantiza que la barra `CRITICAL` de la
 * gráfica sea la misma fila que la píldora `Crítico` del filtro y la primera del desglose. Tres
 * listas en tres sitios que se puedan desincronizar producirían una pantalla donde el color
 * rojo señala el alto a veces y el crítico otras, sin que nada pareciera roto.
 */
export const SEVERITY_KEYS = ['critical', 'high', 'medium', 'low', 'info'] as const

export type SeverityColorKey = (typeof SEVERITY_KEYS)[number]

function readToken(name: string, fallback: string): string {
  if (typeof window === 'undefined' || typeof getComputedStyle !== 'function') {
    return fallback
  }
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value.length > 0 ? value : fallback
}

/**
 * Los colores de un gráfico salen de los tokens de la interfaz, uno a uno.
 *
 * ## Por qué `--chart-*` desapareció
 *
 * Antes esta función leía `--chart-critical`, `--chart-high`... y si el tema no los definía
 * caía a una paleta propia con tonos **500** (`#EF4444` en vez de `#f87171`). Esos tonos no
 * existen en ninguna otra pantalla: el resto de la interfaz usa la rampa **400** de
 * `design-dark.md`, y ese fue justo el motivo por el que se eligió la 400 — sobre negro, el
 * 500 vibra y se ve sucio. El resultado era que un gráfico y una píldora de severidad del
 * mismo color eran **distintos colores**, y no había forma de verlo sin comparar capturas de
 * dos pantallas distintas.
 *
 * Ahora cada token se lee de su nombre real, y `--chart-*` sigue teniendo prioridad si algún
 * día un gráfico necesita un tono que la interfaz no usa: la sobrescritura es explícita y
 * local, no un valor por defecto silencioso.
 */
export function readChartPalette(): ChartPalette {
  return {
    accent: readToken('--chart-accent', readToken('--color-accent', chartPalette.accent)),
    primary: readToken('--color-primary', chartPalette.primary),
    secondary: readToken('--color-secondary', chartPalette.secondary),
    caption: readToken('--color-caption', chartPalette.caption),
    surface: readToken('--chart-surface', readToken('--color-surface', chartPalette.surface)),
    surfaceElevated: readToken(
      '--color-surface-elevated',
      chartPalette.surfaceElevated,
    ),
    background: readToken(
      '--chart-background',
      readToken('--color-background', chartPalette.background),
    ),
    border: readToken('--color-border', chartPalette.border),
    borderHover: readToken('--color-border-hover', chartPalette.borderHover),
    critical: readToken('--chart-critical', readToken('--color-critical', chartPalette.critical)),
    high: readToken('--chart-high', readToken('--color-high', chartPalette.high)),
    medium: readToken('--chart-medium', readToken('--color-medium', chartPalette.medium)),
    low: readToken('--chart-low', readToken('--color-low', chartPalette.low)),
    info: readToken('--chart-info', readToken('--color-info', chartPalette.info)),
  }
}
