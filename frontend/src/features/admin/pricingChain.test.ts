import { describe, expect, it } from 'vitest'
import type {
  OrganizationPriceOverride,
  OrganizationPricingDetail,
} from '../../types/api'

/**
 * Cómo se lee la cadena de pactados en la ficha del cliente.
 *
 * ## Por qué esta lógica vive aquí y no dentro del componente
 *
 * Porque la regla de qué fila se pinta como vigente, y de qué color va su canto, es lo único de
 * esta pantalla que se puede equivocar sin que salte un error: un componente que pinta mal sigue
 * compilando, sigue renderizando y sigue enseñando números. Lo que falla es silencioso —el
 * comercial ve «vigente» sobre un precio que no se está cobrando— y eso no se pilla mirando la
 * pantalla, se pilla con un test.
 *
 * Y la parte que de verdad importa no es el color: es que un pactado **futuro** no se confunda con
 * uno **caducado**. Los dos tienen `valido_hasta === null`, así que cualquier lógica que use esa
 * columna para distinguirlos está equivocada, y hay dos formas naturales de equivocarse.
 */
import { estadoDePactado, ordenarCadena, resumenDeLaCadena } from './pricingChain'

function pactado(over: Partial<OrganizationPriceOverride> = {}): OrganizationPriceOverride {
  return {
    id: 'p1',
    organization_id: 'o1',
    operacion: 'SCAN_CREDIT_COST',
    alcance: null,
    valor: '3.00000000',
    motivo: 'primera cotizacion',
    valido_desde: '2026-01-01T00:00:00Z',
    valido_hasta: null,
    created_by: null,
    created_at: '2026-01-01T00:00:00Z',
    vigente: false,
    ...over,
  }
}

describe('estadoDePactado', () => {
  it('un pactado vigente se distingue de uno que ya no aplica', () => {
    expect(estadoDePactado(pactado({ vigente: true }))).toBe('vigente')
  })

  it('un pactado que termina antes de la fecha de fin no es vigente', () => {
    const caducado = pactado({ vigente: false, valido_hasta: '2026-01-15T00:00:00Z' })

    expect(estadoDePactado(caducado)).toBe('caducado')
  })

  it('un pactado sin fecha de fin y no vigente es futuro, no caducado', () => {
    /**
     * El caso que separa las dos columnas. Un pactado programado tiene `valido_hasta === null`
     * igual que uno vigente, así que la única forma de distinguirlos es `vigente`. Si esta
     * aserción se invirtiera, un pactado que empieza dentro de un mes se pintaría como caducado y
     * el comercial creería que ya no hay precio pactado cuando en realidad el viejo sigue cobrándose.
     */
    expect(estadoDePactado(pactado({ vigente: false, valido_hasta: null }))).toBe('futuro')
  })

  it('un pactado con fecha de fin y vigente sigue siendo vigente', () => {
    /**
     * Un pactado con ventana y abierto a la vez es legal: se pactó «del 1 al 31» y hoy es día 15.
     * La fecha de fin no lo convierte en caducado mientras el servidor lo marque vigente, porque de
     * la caducidad responde el reloj del servidor y no esta función.
     */
    const ventana = pactado({
      vigente: true,
      valido_desde: '2026-01-01T00:00:00Z',
      valido_hasta: '2026-01-31T00:00:00Z',
    })

    expect(estadoDePactado(ventana)).toBe('vigente')
  })
})

describe('ordenarCadena', () => {
  it('deja el más reciente primero, aunque el servidor no lo mande así', () => {
    const viejo = pactado({ id: 'a', valido_desde: '2026-01-01T00:00:00Z' })
    const nuevo = pactado({ id: 'b', valido_desde: '2026-06-01T00:00:00Z' })

    expect(ordenarCadena([viejo, nuevo]).map((p) => p.id)).toEqual(['b', 'a'])
  })

  it('ordena por fecha de inicio, no por el orden de inserción', () => {
    /**
     * El orden por `id` o por posición de la lista daría un resultado distinto, y es justo el
     * resultado que hace que el comercial lea el último de la lista como el último acuerdo. La
     * cadena se ordena por lo que dice la fecha, que es lo que dice el acuerdo.
     */
    const uno = pactado({ id: 'zzz', valido_desde: '2026-01-01T00:00:00Z' })
    const otro = pactado({ id: 'aaa', valido_desde: '2026-02-01T00:00:00Z' })

    expect(ordenarCadena([uno, otro]).map((p) => p.id)).toEqual(['aaa', 'zzz'])
  })

  it('una cadena vacía se queda vacía', () => {
    expect(ordenarCadena([])).toEqual([])
  })

  it('no muta la lista que recibe', () => {
    const original = [
      pactado({ id: 'a', valido_desde: '2026-01-01T00:00:00Z' }),
      pactado({ id: 'b', valido_desde: '2026-06-01T00:00:00Z' }),
    ]
    const copia = [...original]

    ordenarCadena(original)

    expect(original).toEqual(copia)
  })
})

describe('resumenDeLaCadena', () => {
  it('cuenta los tres estados por separado', () => {
    // Dos programados, no uno: `c` y `d` son las dos filas que no son vigentes ni tienen fecha de
    // fin, que es exactamente lo que hace que las dos sean futuras. La primera vez que se
    // escribió este test con un solo futuro pasó la función equivocada, y por eso el caso
    // lleva dos de cada clase en vez de uno: así un `||` donde debería haber dos `if` se ve.
    const cadena = [
      pactado({ id: 'a', vigente: true }),
      pactado({ id: 'b', valido_hasta: '2026-01-15T00:00:00Z' }),
      pactado({ id: 'c' }),
      pactado({ id: 'd' }),
    ]

    expect(resumenDeLaCadena(cadena)).toEqual({ vigente: 1, futuro: 2, caducado: 1, total: 4 })
  })

  it('una cadena sin pactados da todos los contadores a cero', () => {
    expect(resumenDeLaCadena([])).toEqual({ vigente: 0, futuro: 0, caducado: 0, total: 0 })
  })

  it('no cuenta un pactado en dos estados a la vez', () => {
    const cadena = [pactado({ id: 'a', vigente: true, valido_hasta: '2026-01-15T00:00:00Z' })]

    const resumen = resumenDeLaCadena(cadena)

    expect(resumen.vigente + resumen.futuro + resumen.caducado).toBe(resumen.total)
  })
})

describe('lo que la ficha muestra de un cliente sin pactar', () => {
  it('el precio pactado coincide con el de plataforma', () => {
    const plataforma: OrganizationPricingDetail['precios'] = {
      credits_per_usd: '100.00000000',
      scan_credit_cost: '10.00000000',
      quick_scan_credit_multiplier: '0.50000000',
      low_credit_balance_threshold: '20.00000000',
      custom_spend_minimum_usd: '500.00000000',
      custom_spend_maximum_usd: '5000.00000000',
      pro_subscription_monthly_usd: '99.00000000',
      updated_at: null,
    }

    const detalle: OrganizationPricingDetail = {
      organization_id: 'o1',
      precios: { ...plataforma },
      precios_de_plataforma: plataforma,
      overrides: [],
    }

    expect(detalle.precios.scan_credit_cost).toBe(detalle.precios_de_plataforma.scan_credit_cost)
    expect(resumenDeLaCadena(detalle.overrides).total).toBe(0)
  })
})
