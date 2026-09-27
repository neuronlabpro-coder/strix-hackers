import { describe, expect, it } from 'vitest'

import { creditsForSpend, deriveSpendRanges, tierIndexFor } from './volumePricing'
import type { VolumePricing } from '../../types/billing'

/**
 * La escalera que devuelve el servidor. Va a mano y no como `vi.mock` porque lo que se quiere
 * comprobar es la aritmética y la tolerancia a la ausencia, no la integración con la red.
 */
/**
 * La escalera que devuelve el servidor. Va a mano y no como `vi.mock` porque lo que se quiere
 * comprobar es la aritmética y la tolerancia a la ausencia, no la integración con la red.
 *
 * ## Ojo con las unidades
 *
 * `usd_per_credit` son **dólares por crédito**, no créditos por dólar. Con 0,025 un crédito
 * cuesta veinticinco céntimos, así que el mínimo del primer tramo —25 créditos— son
 * `25 * 0,025 = 0,625` dólares, no 6,25. Es la lectura que hace que estas cifras parezcan
 * absurdas la primera vez, y por eso la aritmética va escrita al lado de cada aserción.
 *
 * `usd_per_credit` y `discount` son **cadenas**, no números: el backend los serializa desde
 * `Decimal` y aquí llegan como texto. Por eso las funciones aritméticas envuelven con
 * `Number()` en cada acceso, y por eso `vitest` —que no comprueba tipos— deja pasar un
 * fixture con números que `tsc` rechaza.
 */
const ESCALERA: VolumePricing = {
  minimum_credits: 25,
  maximum_credits: 5000,
  list_usd_per_credit: '0.025',
  tiers: [
    {
      minimum_credits: 25,
      maximum_credits: 250,
      usd_per_credit: '0.025',
      discount: '0',
    },
    {
      minimum_credits: 250,
      maximum_credits: 1000,
      usd_per_credit: '0.0225',
      discount: '0.1',
    },
  ],
}

describe('deriveSpendRanges', () => {
  it('mide los tramos en dólares, no en créditos', () => {
    const tramos = deriveSpendRanges(ESCALERA)

    // min del primer tramo: round(25 créditos * 0,025 $/crédito) = round(0,625) = 1
    expect(tramos[0].min).toBe(1)
    // max del primer tramo: el último céntimo antes del mínimo del siguiente,
    // round(250 * 0,0225) - 1 = round(5,625) - 1 = 6 - 1 = 5
    expect(tramos[0].max).toBe(5)
  })

  // Este es el crash que se vio en la pantalla: la respuesta llegó sin `volume` y la pantalla
  // se quedó en negro. El nombre del test lo dice para que volver a romperlo sea deliberado.
  it('no revienta cuando la escalera no ha llegado', () => {
    expect(() => deriveSpendRanges(undefined)).not.toThrow()
    expect(deriveSpendRanges(undefined)).toEqual([])
  })

  it('no revienta con una escalera sin tramos', () => {
    expect(deriveSpendRanges({ ...ESCALERA, tiers: [] })).toEqual([])
  })
})

describe('creditsForSpend', () => {
  it('devuelve 0 por debajo del mínimo', () => {
    // El mínimo son 25 créditos * 0,025 = 0,625 dólares. Un gasto de 0,50 queda debajo.
    expect(creditsForSpend(0.5, ESCALERA)).toBe(0)
  })

  it('recorre los cinco tramos y se queda con el máximo de créditos', () => {
    // Gasto de 1 dólar: el tramo base da floor(1 / 0,025) = 40 y el siguiente
    // floor(1 / 0,0225) = 44. El cliente quiere el máximo, y ese es el segundo.
    expect(creditsForSpend(1, ESCALERA)).toBe(44)
  })

  // Un `NaN` aquí se pintaría en la caja de créditos y llegaría como cantidad en la compra.
  it('devuelve 0, y no NaN, cuando no hay escalera', () => {
    const resultado = creditsForSpend(500, undefined)

    expect(resultado).toBe(0)
    expect(Number.isNaN(resultado)).toBe(false)
  })
})

describe('tierIndexFor', () => {
  it('devuelve -1 sin tramos, que es lo que deshabilita la compra', () => {
    expect(tierIndexFor(500, [])).toBe(-1)
  })
})
