/**
 * Paleta estricta de `design-dark.md`: ningún color fuera de la ficha.
 * La distribución por severidad se resuelve con opacidad sobre `#EDEDED`,
 * de modo que el tema conserve un único acento interactivo (`#17a163`).
 */
export const chartPalette = {
  surface: '#2A2A2A',
  primary: '#EDEDED',
  secondary: '#8A8F8A',
  accent: '#17a163',
} as const
