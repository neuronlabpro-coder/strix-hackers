import i18n from '../i18n'

/**
 * Formateo de fechas y números sensible al idioma activo.
 *
 * Vive aquí y no en cada vista porque `Intl.DateTimeFormat` necesita el locale
 * explícito: usar `toLocaleDateString()` sin argumentos deja la fecha en el idioma del
 * navegador, que en un panel con selector de idioma produce la mezcla "22 sept 2026"
 * dentro de una interfaz que dice "22 sept 2026" en la tabla y "September 22, 2026" en
 * otra. `cve/format.ts` hacía exactamente eso y queda como candidato a migrar.
 */

export function activeLocale(): string {
  return i18n.resolvedLanguage ?? i18n.language
}

export function formatDate(isoDate: string): string {
  const parsed = new Date(isoDate)
  if (Number.isNaN(parsed.getTime())) {
    return isoDate
  }
  return new Intl.DateTimeFormat(activeLocale(), { dateStyle: 'medium' }).format(parsed)
}

export function formatDateTime(isoDate: string): string {
  const parsed = new Date(isoDate)
  if (Number.isNaN(parsed.getTime())) {
    return isoDate
  }
  return new Intl.DateTimeFormat(activeLocale(), { dateStyle: 'medium', timeStyle: 'short' }).format(
    parsed,
  )
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(activeLocale(), options).format(value)
}
