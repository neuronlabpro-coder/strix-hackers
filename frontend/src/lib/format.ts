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

// --------------------------------------------------------------------------- //
// Importes y saldos de créditos
// --------------------------------------------------------------------------- //
//
// Los importes viajan como **cadenas decimales** porque el backend los declara `Decimal`
// y Pydantic los serializa así. El motivo está en `types/billing.ts`: un número JSON es un
// binario en coma flotante donde `0.1` no es exactamente `0.1`, y con `Numeric(18, 4)` los
// saldos tienen justo los decimales donde eso empieza a mentir.
//
// Las funciones de abajo **no** convierten a número. `Intl.NumberFormat` acepta la cadena
// directamente y la interpreta con redondeo decimal exacto, que es lo que se quiere para
// pintar. Lo que no se hace —y no debe hacerse— es sumar o restar sobre estas cadenas en el
// cliente: para eso está la columna de la base, que ya es `Decimal`.

/** Decimales con los que se muestra un saldo en el panel. */
const CREDIT_FRACTION_DIGITS = 2

/**
 * Lo que `Intl.NumberFormat.format` acepta en la práctica, aunque los tipos no lo digan.
 *
 * ## Por qué una cadena no es un accidente de la implementación
 *
 * La especificación de ECMA-402 convierte el argumento con `ToIntlMathematicalValue`, y esa
 * función tiene una rama explícita para `String` que **interpreta el texto como literal
 * decimal**: separa la parte entera de la fraccionaria y guarda los dígitos como enteros.
 * No pasa por un binario de coma flotante en ningún momento.
 *
 * Es exactamente el comportamiento que hace falta para un `Numeric(18, 4)`: `0.1` se
 * muestra como `0,10` y no como `0,1000000000000000055511151231257827`, porque no hay
 * coma flotante que perder precision por el camino. Por eso se usa el texto del backend
 * tal cual y no `Number(texto)`, que sí perdería la precisión.
 *
 * Lo que sí sería un accidente es que TypeScript no haya tipado ese camino. El `cast`
 * local de abajo no oculta un comportamiento indefinido: documenta uno definido que la
 * biblioteca de tipos todavía no refleja. Si algún día los tipos lo incluyen, el `cast`
 * sobra y se puede borrar sin más.
 */
type IntlFormatValue = number | bigint | string

function formatWith(formatter: Intl.NumberFormat, value: IntlFormatValue): string {
  return (formatter.format as (value: IntlFormatValue) => string)(value)
}

/** Un saldo o un asiento, con separadores y redondeado a dos decimales. */
export function formatCredits(valor: string | number): string {
  return formatWith(
    new Intl.NumberFormat(activeLocale(), {
      minimumFractionDigits: CREDIT_FRACTION_DIGITS,
      maximumFractionDigits: CREDIT_FRACTION_DIGITS,
    }),
    valor,
  )
}

/** Un importe en dólares, con el símbolo de la moneda del idioma activo. */
export function formatUsd(valor: string | number): string {
  return formatWith(
    new Intl.NumberFormat(activeLocale(), {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: CREDIT_FRACTION_DIGITS,
      maximumFractionDigits: CREDIT_FRACTION_DIGITS,
    }),
    valor,
  )
}

/** Un entero con separadores de miles, para recuentos que no son dinero. */
export function formatCount(valor: number): string {
  return formatWith(new Intl.NumberFormat(activeLocale(), { maximumFractionDigits: 0 }), valor)
}

/**
 * El signo de un asiento, listo para pintar con color.
 *
 * Un delta de cero devuelve `'neutral'` y no `'positive'`. En una columna de saldo, un
 * `+0,00` en verde convince al lector de que se acreditó algo, y ese mismo cero en un
 * asiento de consumo se leería como un fallo de formato en vez de lo que es: nada cambió.
 */
export type DeltaTone = 'positive' | 'negative' | 'neutral'

/** Cero en cualquiera de las formas que puede enviar el backend: `0`, `0.00`, `0.0000`. */
const ZERO_PATTERN = /^[+-]?0+(\.0+)?$/

export function deltaTone(valor: string): DeltaTone {
  const limpio = valor.trim()
  if (limpio.startsWith('-') && !ZERO_PATTERN.test(limpio)) {
    return 'negative'
  }
  if (ZERO_PATTERN.test(limpio)) {
    return 'neutral'
  }
  return 'positive'
}

/**
 * La magnitud de un delta, sin el signo.
 *
 * Existe porque `Intl` ya pone el menos de una cadena negativa: anteponer otro para
 * controlarlo desde el color produciría «−−50,00». Quitar el signo del texto y devolver
 * solo los dígitos deja que la composición del color y del glifo la haga quien llama.
 */
export function deltaMagnitude(valor: string): string {
  return formatCredits(valor.replace(/^[+-]/, ''))
}
