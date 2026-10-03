/**
 * Los esquemas de los gráficos tienen que cumplir las reglas que los hacen legibles.
 *
 * ## Por qué estas pruebas y no una captura
 *
 * Porque una captura demuestra que el gráfico se pinta, y no que se lee. Lo que falla de
 * verdad —un eje en `--color-muted` a 3,04:1, una serie que solo se distingue por el color,
 * una etiqueta que solo aparece al pasar el ratón— sigue pintándose igual con el color
 * equivocado. Estas pruebas miran las cuatro reglas: color del sistema, significado que no
 * depende solo del color, valor visible sin tooltip y texto traducible.
 */

import { describe, expect, it } from 'vitest'

import { chartPalette } from './palette'
import {
  STATUS_ORDER,
  formatearEntero,
  formatearPorcentaje,
  opcionBarrasReparto,
  opcionLineaApilada,
  puntosDeEstado,
  puntosDeSeveridad,
  type RepartoPunto,
  type SerieTemporal,
} from './opciones'

const palette = chartPalette

/** Los textos ya traducidos que le pasaría una pantalla. */
const TEXTOS = { serie: 'Hallazgos', total: 'Total: 12', vacio: 'Sin datos' }

/** Una estructura mínima de severidades para no escribir los cinco en cada prueba. */
const SEVERIDADES = [
  { severity: 'CRITICAL', total: 3 },
  { severity: 'HIGH', total: 7 },
  { severity: 'MEDIUM', total: 0 },
  { severity: 'LOW', total: 2 },
  { severity: 'INFO', total: 1 },
]

/**
 * El esquema con la forma real, porque `ChartOption` es `EChartsCoreOption` y no declara
 * ninguna de estas propiedades: sin esta interfaz, `opcion.series[0].itemStyle.color` no
 * compilaría y el fichero acabaría lleno de `as any`, que es justo lo que estas pruebas
 * existen para no necesitar.
 */
interface Esquema {
  series: Array<{
    type: string
    stack?: string
    name?: string
    data: unknown[]
    itemStyle?: { color?: string; label?: { show?: boolean } }
    label?: { show?: boolean }
    endLabel?: { show?: boolean }
    areaStyle?: { color?: string }
    lineStyle?: { color?: string }
  }>
  xAxis?: unknown
  yAxis?: { data?: string[]; axisLabel?: { color?: string } }
  legend?: { show?: boolean }
}

describe('barras de reparto por severidad', () => {
  const puntos: RepartoPunto[] = puntosDeSeveridad(SEVERIDADES, palette, (s) => s)
  const opcion = opcionBarrasReparto(puntos, palette, TEXTOS) as unknown as Esquema
  const serie = opcion.series[0]!

  it('devuelve una barra por cada severidad, en las cinco', () => {
    expect(serie.type).toBe('bar')
    expect(serie.data).toHaveLength(5)
  })

  it('pone el más grave arriba', () => {
    // ECharts pinta la primera categoría abajo, así que la lista se invierte para que el
    // ojo entre por el crítico. Es la comprobación que detecta un `reverse()` que se pierde.
    expect(opcion.yAxis?.data).toEqual(['INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
  })

  it('cada fila lleva su valor escrito, sin depender del tooltip', () => {
    for (const dato of serie.data as Array<{ value: number }>) {
      expect(dato.value).toBeGreaterThanOrEqual(0)
    }
    // La etiqueta exterior es la que muestra el número: está activa para toda fila con datos.
    const conDatos = serie.data as Array<{ label: { show: boolean }; value: number }>
    expect(conDatos.every((dato) => dato.label.show)).toBe(true)
  })

  it('una severidad en cero no se pinta como si tuviera valor dentro de la barra', () => {
    const filas = serie.data as Array<{ value: number; itemStyle: { label: { show: boolean } } }>
    const vacia = filas.find((fila) => fila.value === 0)
    expect(vacia).toBeDefined()
    expect(vacia!.itemStyle.label.show).toBe(false)
  })

  it('cada fila usa el token de su severidad y solo ese', () => {
    const filas = serie.data as Array<{ itemStyle: { color: string } }>
    const colores = filas.map((fila) => fila.itemStyle.color)
    // La fila de arriba es CRITICAL, la de abajo INFO: los colores van en el orden inverso.
    expect(colores).toEqual([
      palette.info,
      palette.low,
      palette.medium,
      palette.high,
      palette.critical,
    ])
  })

  it('no contiene ningún color escrito a mano fuera de la paleta', () => {
    const permitidos = new Set<string>(Object.values(palette))
    for (const fila of serie.data as Array<{ itemStyle: { color: string } }>) {
      expect(permitidos.has(fila.itemStyle.color)).toBe(true)
    }
  })
})

describe('barras de reparto por estado', () => {
  const puntos = puntosDeEstado(
    [
      { status: 'OPEN', total: 4 },
      { status: 'FIXED', total: 6 },
      { status: 'IGNORED', total: 1 },
    ],
    palette,
    (s) => s,
  )
  const serie = (opcionBarrasReparto(puntos, palette, TEXTOS) as unknown as Esquema).series[0]!

  it('devuelve las seis filas aunque el servidor no mande las que valen cero', () => {
    expect(serie.data).toHaveLength(STATUS_ORDER.length)
  })

  it('solo FIXED lleva el acento; el resto comparte un tono neutro', () => {
    const colores = (serie.data as Array<{ name: string; itemStyle: { color: string } }>).map(
      (fila) => fila.itemStyle.color,
    )
    const porNombre = Object.fromEntries(
      (serie.data as Array<{ name: string; itemStyle: { color: string } }>).map((fila) => [
        fila.name,
        fila.itemStyle.color,
      ]),
    )
    expect(porNombre.FIXED).toBe(palette.accent)
    expect(porNombre.OPEN).toBe(palette.info)
    expect(porNombre.IGNORED).toBe(palette.info)
    // Y hay un tono repetido a propósito: el estado no compite con la severidad por el color.
    expect(new Set(colores).size).toBe(2)
  })

  it('las etiquetas son las claves del ciclo de vida, que es lo que se lee', () => {
    const nombres = (serie.data as Array<{ name: string }>).map((fila) => fila.name)
    expect(nombres).toContain('REMEDIATION_PROPOSED')
  })
})

describe('línea apilada por severidad', () => {
  const puntos = [
    { dia: '2026-09-28', total: 0 },
    { dia: '2026-09-29', total: 4 },
    { dia: '2026-09-30', total: 9 },
  ]
  const series: SerieTemporal[] = [
    { clave: 'critical', nombre: 'Critico', valores: [0, 1, 2] },
    { clave: 'high', nombre: 'Alto', valores: [0, 2, 4] },
    { clave: 'medium', nombre: 'Medio', valores: [0, 1, 3] },
  ]
  const opcion = opcionLineaApilada(puntos, series, palette, {
    total: 'Total: 13',
    eje: 'Dia',
  }) as unknown as Esquema

  it('las tres series van apiladas con el mismo nombre de pila', () => {
    expect(opcion.series).toHaveLength(3)
    expect(opcion.series.every((serie) => serie.stack === 'total')).toBe(true)
  })

  it('etiqueta el último punto de cada serie y ningún otro', () => {
    const primera = opcion.series[0]!
    const datos = primera.data as Array<{ label: { show: boolean } }>
    expect(datos[0]!.label.show).toBe(false)
    expect(datos[1]!.label.show).toBe(false)
    expect(datos[2]!.label.show).toBe(true)
  })

  it('el último día con valor cero no inventa una etiqueta', () => {
    const serieSinNada: SerieTemporal[] = [
      { clave: 'critical', nombre: 'Critico', valores: [0, 0, 0] },
    ]
    const opcionSinNada = opcionLineaApilada(puntos, serieSinNada, palette, {
      total: 'Total: 0',
      eje: 'Dia',
    }) as unknown as Esquema
    const datos = opcionSinNada.series[0]!.data as Array<{ label: { show: boolean } }>
    expect(datos[2]!.label.show).toBe(false)
  })

  it('cada serie usa su token de severidad en la línea, el área y el símbolo', () => {
    const colores = opcion.series.map((serie) => serie.lineStyle?.color)
    expect(colores).toEqual([palette.critical, palette.high, palette.medium])
    for (const serie of opcion.series) {
      expect(serie.areaStyle?.color).toBe(serie.lineStyle?.color)
    }
  })

  it('los días van en el eje y la retícula se pinta', () => {
    const opcionCompleta = opcion as unknown as {
      xAxis: { data: string[] }
      yAxis: { splitLine: { lineStyle: { color: string } } }
    }
    expect(opcionCompleta.xAxis.data).toEqual(['2026-09-28', '2026-09-29', '2026-09-30'])
    expect(opcionCompleta.yAxis.splitLine.lineStyle.color).toBe(palette.border)
  })
})

describe('el texto de los ejes no usa el tono de contraste insuficiente', () => {
  it('las etiquetas de eje usan un tono legible, no --color-muted', () => {
    const opcion = opcionBarrasReparto(
      puntosDeSeveridad(SEVERIDADES, palette, (s) => s),
      palette,
      TEXTOS,
    ) as unknown as {
      xAxis: { axisLabel: { color: string } }
      yAxis: { axisLabel: { color: string } }
    }
    // `--color-muted` está en 3,04:1 y la propia documentación del tema lo reserva para
    // etiquetas supremas de 11 px en mono. Una etiqueta de eje no es eso.
    expect(opcion.yAxis.axisLabel.color).not.toBe('#5e6575')
    expect(opcion.xAxis.axisLabel.color).toBe(palette.caption)
  })
})

describe('formato de números', () => {
  it('los enteros usan el separador de miles del idioma activo', () => {
    // En español el punto separa los millares a partir de cinco cifras: `1234` va sin
    // separador y `1.234.567` con él. Escribir `toLocaleString()` con el idioma equivocado
    // daría `1,234,567` a un lector español, y el fallo no se ve hasta que hay un hallazgo
    // con seis cifras —que es exactamente cuando ya no se puede corregir a mano.
    expect(formatearEntero(1234567, 'es')).toBe('1.234.567')
    expect(formatearEntero(1234567, 'en')).toBe('1,234,567')
  })

  it('un reparto de cero es cero por ciento y no NaN', () => {
    // El `undefined` sale de dividir por cero, y una etiqueta con el separador vacío es
    // justo lo que aparece cuando un filtro deja la lista vacía.
    expect(formatearPorcentaje(0, 0, 'es')).toBe(formatearPorcentaje(0, 1, 'es'))
    expect(formatearPorcentaje(3, 12, 'es')).toContain('25')
    expect(formatearPorcentaje(3, 12, 'es')).not.toContain('NaN')
  })
})