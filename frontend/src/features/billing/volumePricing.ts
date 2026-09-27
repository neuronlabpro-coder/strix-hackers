/**
 * Aritmética de la escalera de descuento por volumen.
 *
 * Vive en su propio archivo, y no junto al componente, por una razón concreta: un módulo que
 * exporta **componentes y funciones** rompe el refresco en caliente de React. Al guardar,
 * el cargador solo se invalida si el módulo contiene un único tipo de export, y con dos
 * tipos obliga a recargar la página entera —en desarrollo, mientras se arrastra el slider—.
 *
 * Aquí no hay JSX, no hay estado y no hay nada de React: son funciones puras sobre la
 * escalera que manda el servidor.
 */

import type { VolumePricing } from '../../types/billing'

/** Rango de gasto de un tramo, en dólares. */
export interface SpendRange {
  min: number
  max: number
}

/**
 * Rango de gasto de cada tramo, derivado de los umbrales que manda el servidor.
 *
 * ## Por qué hay que convertir de créditos a dólares
 *
 * El servidor manda `minimum_credits` y `maximum_credits` de cada tramo, no los dólares. La
 * barra tiene que estar medida en **dólares**, que es la unidad en la que el cliente decide
 * y la que define la escalera: al cruzar un umbral lo que sube es el saldo —gastar un dólar
 * más en el tramo del 15% da 65 créditos más—, no el precio de un crédito.
 *
 * Si la barra se midiera en créditos, el tramo de $10 a $251 ocuparía casi nada y el de
 * $5.001 a $10.000 ocuparía la mitad, con medio hueco vacío en el centro. La escala del
 * dinero es la que refleja la decisión.
 *
 * La conversión es **exacta por construcción**, no aproximada: `credits_for_spend(spend)`
 * devuelve `floor(spend / unit)`, así que el primer gasto que da `minimum_credits` es
 * exactamente `minimum_credits * unit`. Y el último gasto del tramo es un céntimo menos del
 * primer gasto del siguiente, restando la unidad **de ese** tramo.
 */
export function deriveSpendRanges(
  pricing: VolumePricing | undefined,
): readonly SpendRange[] {
  // ## Por qué `[]` y no una escalera por defecto
  //
  // Se propuso usar una escalera de reserva, y es la peor opción posible en esta pantalla: una
  // lista de precios que el servidor no ha enviado se **pinta**. Un cliente que la mirara creería
  // que son los precios de su contrato, y el daño no es un error visible sino una belief
  // equivocada sobre lo que va a pagar. Ante la duda aquí no se enseña nada: la barra se queda
  // vacía y el botón de compra se deshabilita.
  //
  // ## Por qué hace falta esto
  //
  // Porque el tipo declaraba `volume` como obligatorio y el servidor lo es, así que el
  // compilador no veía el fallo: el panel se quedó en negro con
  // `Cannot read properties of undefined (reading 'tiers')` cuando la respuesta llegó sin ese
  // bloque. Declarar el campo opcional obliga a TypeScript a señalarlo en **cada** punto de
  // uso, que es justo lo que faltaba.
  const tiers = pricing?.tiers
  if (tiers === undefined || tiers.length === 0) {
    return []
  }
  return tiers.map((tier, index) => {
    const unit = Number(tier.usd_per_credit)
    const next = tiers[index + 1]
    return {
      min: Math.round(tier.minimum_credits * unit),
      max:
        next === undefined
          ? Math.round(tier.maximum_credits * unit)
          : Math.round(next.minimum_credits * Number(next.usd_per_credit)) - 1,
    }
  })
}

/**
 * Los créditos que da un gasto.
 *
 * Es una réplica de `credits_for_spend` del backend, y es a propósito.
 *
 * ## Por qué el panel replica el cálculo en vez de pedirlo
 *
 * Porque el slider tiene que **responder al instante** mientras se arrastra. Pedir el saldo
 * de cada valor sería una petición por píxel, y el control se sentiría pegajoso. El servidor
 * manda la escalera entera en la respuesta, así que el panel tiene todo lo necesario para
 * calcular sin ir a preguntar.
 *
 * ## Por qué eso no significa que el panel decida el precio
 *
 * Porque **no**. El botón de compra manda la **cantidad de créditos** que sale de aquí, y el
 * servidor recalcula el importe con su propia `price_for_credits` antes de crear la sesión
 * de cobro. El panel no envía ningún importe ni ningún descuento, y el backend los rechaza
 * con un `422` si se intentan.
 *
 * La consecuencia práctica: si esta réplica se desincronizara del servidor, lo que se vería
 * es una cifra que no cuadra con el precio de Stripe antes de pagar. Nunca un cobro distinto
 * del anunciado, porque el cobro lo decide la función del servidor.
 *
 * ## Por qué se recorre la escalera entera y no solo el tramo activo
 *
 * Porque el saldo de un tramo **no** es `spend / unit` del tramo que toca y ya. Un gasto que
 * cae en el tramo del 15% también podría «comprarse» en el del 10% con un saldo distinto, y el
 * cliente quiere el **máximo** de créditos que su dinero permite. Recorrer los cinco y
 * quedarse con el mayor candidato es lo que hace el servidor, y es por eso que los dos lados
 * coinciden.
 */
export function creditsForSpend(spend: number, pricing: VolumePricing | undefined): number {
  // Misma regla que en `deriveSpendRanges`: sin escalera no hay cálculo, y el resultado
  // neutro es `0` créditos, que es lo que deshabilita el botón de compra. Un `NaN` o un
  // `Infinity` aquí aparecerían como un número en la caja de créditos y llegarían hasta la
  // cantidad enviada al servidor.
  if (pricing === undefined) {
    return 0
  }
  const minimoBase = Number(pricing.minimum_credits) * Number(pricing.list_usd_per_credit)
  if (spend < minimoBase) {
    return 0
  }
  let mejor = 0
  for (const tier of pricing.tiers) {
    const unit = Number(tier.usd_per_credit)
    if (unit <= 0) continue
    const candidato = Math.floor(spend / unit)
    if (candidato > mejor) {
      mejor = candidato
    }
  }
  return mejor
}

/**
 * El tramo vigente para un gasto: el último cuyo mínimo no lo supera.
 *
 * Devuelve `-1` cuando el gasto no llega al mínimo, que es el caso en el que no hay tramo y
 * el botón de compra tiene que estar deshabilitado. No devuelve el primer tramo por defecto
 * porque eso haría que un gasto de $3 pareciera estar en el tramo base, que no es donde
 * está.
 */
export function tierIndexFor(spend: number, ranges: readonly SpendRange[]): number {
  let indice = -1
  for (let i = 0; i < ranges.length; i += 1) {
    if (spend >= ranges[i].min) {
      indice = i
    }
  }
  return indice
}
