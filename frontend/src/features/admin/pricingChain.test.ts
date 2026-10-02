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
import {
  estadoDePactado,
  estadosDeLaCadena,
  ordenarCadena,
  resumenDeLaCadena,
} from './pricingChain'

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

  it('un pactado sin fecha de fin y no vigente es programado, no caducado', () => {
    /**
     * El caso que separa las dos columnas. Un pactado programado tiene `valido_hasta === null`
     * igual que uno vigente, así que la única forma de distinguirlos es `vigente`. Si esta
     * aserción se invirtiera, un pactado que empieza dentro de un mes se pintaría como caducado y
     * el comercial creería que ya no hay precio pactado cuando en realidad el viejo sigue cobrándose.
     */
    expect(estadoDePactado(pactado({ vigente: false, valido_hasta: null }))).toBe('programado')
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

    expect(resumenDeLaCadena(cadena)).toEqual({ vigente: 1, programado: 2, caducado: 1, sustituido: 0, total: 4 })
  })

  it('una cadena sin pactados da todos los contadores a cero', () => {
    expect(resumenDeLaCadena([])).toEqual({ vigente: 0, programado: 0, caducado: 0, sustituido: 0, total: 0 })
  })

  it('no cuenta un pactado en dos estados a la vez', () => {
    const cadena = [pactado({ id: 'a', vigente: true, valido_hasta: '2026-01-15T00:00:00Z' })]

    const resumen = resumenDeLaCadena(cadena)

    expect(
      resumen.vigente + resumen.programado + resumen.caducado + resumen.sustituido,
    ).toBe(resumen.total)
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


describe('estadosDeLaCadena', () => {
  /**
   * La cadena real de la demo: uno vigente y cuatro ya sustituidos, con uno programado por delante.
   *
   * ## Por qué este caso y no un ejemplo inventado
   *
   * ## Por qué es el caso que salió en la captura
   *
   * Porque es el que se ve en la ficha del cliente de la demo, y durante el rediseño se vio en
   * pantalla que los cuatro sustituidos salían como "Programado". Un caso inventado lo demuestra
   * igual, pero este es el que falló de verdad, así que es el que se fija.
   */
  it('un pactado ya empezado al que se le puso otro se dice sustituido, no programado', () => {
    const ahora = Date.now()
    const dia = 24 * 60 * 60 * 1000
    const vigente = pactado({ id: 'v', vigente: true, valido_desde: new Date(ahora - dia).toISOString() })
    const sustituidos = ['a', 'b', 'c'].map((id, i) =>
      pactado({ id, valido_desde: new Date(ahora - (i + 2) * dia).toISOString() }),
    )

    const estados = estadosDeLaCadena([vigente, ...sustituidos])

    expect(estados.get(vigente.id)).toBe('vigente')
    for (const fila of sustituidos) {
      expect(estados.get(fila.id)).toBe('sustituido')
    }
  })

  it('un pactado que aun no empieza no esta sustituido: esta esperando su turno', () => {
    const dia = 24 * 60 * 60 * 1000
    const vigente = pactado({ id: 'v', vigente: true, valido_desde: new Date(Date.now() - dia).toISOString() })
    const futuro = pactado({ id: 'f', valido_desde: new Date(Date.now() + 30 * dia).toISOString() })

    const estados = estadosDeLaCadena([vigente, futuro])

    expect(estados.get(futuro.id)).toBe('programado')
  })

  it('sin vigente del servidor, el mas reciente que ya empezo manda y el otro se marca sustituido', () => {
    /**
     * La cadena sin ningún vigente marcado es un estado que el servidor puede devolver —la caché se
     * refresca por evento, y hay una ventana— y ahí hay que deducir quién manda con la misma regla
     * del negocio. Dejarla toda en "programado" diría que hay dos subidas futuras.
     */
    const dia = 24 * 60 * 60 * 1000
    const viejo = pactado({ id: 'a', valido_desde: new Date(Date.now() - 10 * dia).toISOString() })
    const nuevo = pactado({ id: 'b', valido_desde: new Date(Date.now() - dia).toISOString() })

    const estados = estadosDeLaCadena([viejo, nuevo])

    expect(estados.get(nuevo.id)).toBe('vigente')
    expect(estados.get(viejo.id)).toBe('sustituido')
  })

  it('el vigente del servidor manda aunque haya uno mas reciente ya empezado', () => {
    /**
     * El caso que faltaba, y el que hace que el servidor sea la autoridad.
     *
     * ## Por qué tiene que ganar la marca del servidor y no la fecha
     *
     * Porque el servidor sabe cosas que el cliente no: la fila se insertó, pero su caché se
     * refresca por evento, así que durante una ventana puede devolver la cadena con la marca
     * puesta en el pactado de ayer y el de hoy sin marcar. Si la pantalla deshiciera eso —因为 la
     * fecha dice que el de hoy es más nuevo— estaría **corrigiendo al servidor con una regla
     * incompleta**, y lo que se vería es un precio vigente que el cobro no está aplicando.
     *
     * ## Por qué este caso es el que distingue las dos implementaciones
     *
     * ## Por qué aquí sí se nota
     *
     * Porque si el de más reciente ya empezó y no está marcado, la regla de la fecha elegiría al
     * de hoy. Con la marca del servidor, se queda el de ayer como vigente y el de hoy pasa a
     * sustituido. Son respuestas opuestas a partir de la misma entrada.
     */
    const dia = 24 * 60 * 60 * 1000
    const ayer = pactado({ id: 'ayer', vigente: true, valido_desde: new Date(Date.now() - 2 * dia).toISOString() })
    const hoy = pactado({ id: 'hoy', valido_desde: new Date(Date.now() - dia).toISOString() })

    const estados = estadosDeLaCadena([hoy, ayer])

    expect(estados.get(ayer.id)).toBe('vigente')
    expect(estados.get(hoy.id)).toBe('sustituido')
  })

  it('una operacion no se come a la otra al marcar los sustituidos', () => {
    const dia = 24 * 60 * 60 * 1000
    const escaneoA = pactado({ id: 'a', valido_desde: new Date(Date.now() - 3 * dia).toISOString() })
    const escaneoB = pactado({ id: 'b', valido_desde: new Date(Date.now() - dia).toISOString() })
    // El pack viejo es **más antiguo** que el nuevo: con la misma fecha quién gana sería
    // arbitrario y el test no probaría nada.
    const pack = pactado({ id: 'p', valido_desde: new Date(Date.now() - 4 * dia).toISOString() })
    pack.operacion = 'CREDIT_PACK_AMOUNT'
    pack.alcance = '250'
    const packNuevo = pactado({ id: 'q', valido_desde: new Date(Date.now() - dia).toISOString() })
    packNuevo.operacion = 'CREDIT_PACK_AMOUNT'
    packNuevo.alcance = '250'

    const estados = estadosDeLaCadena([escaneoA, escaneoB, pack, packNuevo])

    expect(estados.get(escaneoB.id)).toBe('vigente')
    expect(estados.get(escaneoA.id)).toBe('sustituido')
    expect(estados.get(pack.id)).toBe('sustituido')
    expect(estados.get(packNuevo.id)).toBe('vigente')
  })

  it('cadena vacia: mapa vacio', () => {
    expect(estadosDeLaCadena([]).size).toBe(0)
  })
})
