/**
 * Los esquemas de los gráficos del panel, en un solo sitio.
 *
 * ## Por qué un módulo y no un `option` en cada pantalla
 *
 * Por lo mismo que en el backend: cada copia es un sitio donde el día que cambie un color, un
 * tamaño de letra o el contraste de un eje nadie lo va a notar hasta que se mire la pantalla.
 * Aquí se pueden revisar las cuatro de golpe, y se pueden probar sin montar un panel.
 *
 * ## Las cuatro reglas que sigue todo esquema de este fichero
 *
 * 1. **El color no es el único portador de significado.** Cada serie lleva su nombre escrito en
 *    la leyenda y, cuando cabe, junto al dato. El panel tiene gente que no distingue el rojo
 *    del naranja, y una gráfica que solo se puede leer con la vista completa no es accesible.
 * 2. **El dato se enseña sin interrogarlo.** Ninguna serie depende del tooltip. Las barras
 *    llevan su valor en la propia barra; las líneas, en el último punto.
 * 3. **Los colores salen de `readChartPalette()`**, que los lee de los tokens de `index.css`.
 *    Aquí no hay ni un `#` escrito: un literal en este fichero es un color que no existe en
 *    ninguna otra pantalla.
 * 4. **Nada de texto escrito dentro del esquema** que vaya a ser visible. Las etiquetas y los
 *    ejes se construyen con los textos que les pasa quien llama, ya traducidos, porque un
 *    `formatter` no puede llamar a `t()` y quedarse traducible.
 */

import type { ChartOption } from './EChart'
import { SEVERITY_KEYS, type ChartPalette, type SeverityColorKey } from './palette'

/**
 * El eje de texto de un gráfico.
 *
 * Usa `caption` y no `muted` a propósito. `--color-muted` está en 3,04:1, y la propia
 * documentación del tema lo limita a «etiquetas supremas de 11 px en mono, que cuentan como
 * texto grande». Una etiqueta de eje de Apache ECharts mide 11 px en una tipografía de palo
 * seco sin el interletraje que convierte el mayúsculas en «grande»: son 3,04:1 de texto
 * normal, es decir, por debajo del 4,5:1 de AA, y por tanto ilegible sobre el fondo de la
 * tarjeta. `caption` está en 8,0:1 y se lee a la primera.
 */
const TEXTO_EJE = { fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } as const

/**
 * La retícula y el eje de una serie.
 *
 * `borderHover` y no `border`: sobre `--color-surface`, `--color-border` es un paso de
 * luminosidad que la tarjeta ya usa para sus propias líneas horizontales, y una retícula que
 * se confunde con el separador de fila no ordena nada. `--color-border-hover` es un paso
 * visible por encima de la superficie y por debajo del texto, que es lo que tiene que ser una
 * línea de guía.
 */
function ejeCategoria(palette: ChartPalette, color = palette.caption) {
  return {
    type: 'category' as const,
    axisLine: { lineStyle: { color: palette.borderHover } },
    axisTick: { show: false },
    axisLabel: { ...TEXTO_EJE, color },
  }
}

function ejeValor(palette: ChartPalette) {
  return {
    type: 'value' as const,
    minInterval: 1,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { ...TEXTO_EJE, color: palette.caption },
    splitLine: { lineStyle: { color: palette.border, width: 1 } },
  }
}

/** Los cinco colores de severidad, en el orden del modelo. */
function colorDeSeveridad(palette: ChartPalette, clave: SeverityColorKey): string {
  return palette[clave]
}

/**
 * Las cinco severidades del modelo, en el orden de `SeverityEnum`.
 *
 * Va declarado aquí y no importado de `types/api.ts` porque un array de tipos de unión es
 * justo lo que un `as const` convierte en una tupla de literales, y de esa tupla sale
 * exactamente el `string` que la API devuelve en `severity`. Importarlo como
 * `VulnerabilitySeverity[]` obligaría a castear en cada acceso.
 */
const SEVERITY_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'] as const

/** Un número en el formato del idioma activo, sin decimales que no aporta nada aquí. */
export function formatearEntero(valor: number, locale: string): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(valor)
}

/** Un porcentaje de un reparto, con un solo decimal solo cuando hace falta. */
export function formatearPorcentaje(parte: number, total: number, locale: string): string {
  // El caso de `total` a cero **no** se resuelve con un `'0%'` escrito a mano: en español el
  // Intl escribe `0 %`, con espacio, y tener las dos formas en la misma pantalla deja un cero
  // pegado al borde y otro con hueco. Se le pasa un total de uno y que lo formatee el idioma.
  return new Intl.NumberFormat(locale, {
    style: 'percent',
    maximumFractionDigits: 1,
  }).format(total <= 0 ? 0 : parte / total)
}

/** Una entrada de un desglose por severidad o por estado, ya con su color resuelto. */
export interface RepartoPunto {
  /** Clave de la rampa: `critical`... o el nombre del estado cuando el reparto es por estado. */
  clave: string
  /** Texto visible de la fila, ya traducido. */
  etiqueta: string
  total: number
  color: string
}

/**
 * Barras horizontales para un reparto de cinco o seis categorías.
 *
 * ## Por qué horizontales y no verticales
 *
 * Porque las etiquetas son **palabras**, no números: «Remediación propuesta» es veintidós
 * caracteres y no cabe girada bajo una columna de 260 px sin recortarse. Con las categorías en
 * el eje vertical el texto va en horizontal y se lee entero, y la barra es la que se acorta.
 * Con cinco o seis categorías la altura de cada barra sale de sobra para un número de dos
 * cifras dentro.
 *
 * ## Por qué el valor va dentro de la barra y no encima
 *
 * Porque «encima» obliga a reservar espacio vertical para el caso de la barra más corta, que
 * es el más alto del conjunto: un gráfico de barras apiladas en vertical con etiqueta encima
 * gasta la mitad de su alto en nada. Dentro de la barra el alto se aprovecha entero, y el
 * texto se lee sobre el tono de severidad, que es lo que hay que leer.
 *
 * ## Por qué el total y el porcentaje van juntos
 *
 * Porque un `7` al lado de una etiqueta no dice si es mucho o poco. El total da el volumen y
 * el porcentaje da la proporción, y en un reparto por severidad son dos preguntas distintas
 * que se responden leyendo la misma fila: «¿cuántos críticos tengo?» y «¿mis críticos son el
 * quince por ciento de lo que hay abierto o son todo?».
 */
export function opcionBarrasReparto(
  puntos: RepartoPunto[],
  palette: ChartPalette,
  textos: { serie: string; total: string; vacio: string },
): ChartOption {
  // ECharts recorre el eje `category` de abajo arriba: la primera categoría se pinta abajo.
  // Por eso el reparto se invierte, para que el más grave quede arriba, que es donde el ojo
  // entra en una lista de riesgo.
  const filas = [...puntos].reverse()

  return {
    backgroundColor: 'transparent',
    aria: {
      enabled: true,
      decal: { show: false },
      description: textos.total,
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: palette.surfaceElevated,
      borderColor: palette.borderHover,
      textStyle: { color: palette.primary, fontFamily: 'JetBrains Mono, monospace', fontSize: 12 },
      extraCssText: 'box-shadow: none;',
    },
    /*
     * `outerBounds`, no `containLabel`.
     *
     * ECharts 6 marca `containLabel` como obsoleto y avisa por consola en cada pintado:
     * `use (LegacyGridContainLabel); use grid.outerBounds instead`. El aviso es el peor sitio
     * para enterarse de una deprecación, porque se pierde entre los `[vite] connecting...` y
     * nadie lo lee hasta que la versión que lo rompe quite el compat. Su equivalente exacto, según
     * la propia documentación de la librería, es
     * `{ outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' }`, y es lo que se escribe.
     *
     * `right: 72` es lo que deja sitio a los números de la derecha: sin él, el valor de la
     * fila más larga se saldría fuera del lienzo en vez de recortarse a la mitad.
     */
    grid: {
      left: 8,
      right: 72,
      top: 8,
      bottom: 8,
      outerBoundsMode: 'same',
      outerBoundsContain: 'axisLabel',
    },
    xAxis: ejeValor(palette),
    yAxis: {
      ...ejeCategoria(palette),
      data: filas.map((fila) => fila.etiqueta),
    },
    series: [
      {
        name: textos.serie,
        type: 'bar',
        barMaxWidth: 22,
        data: filas.map((fila) => ({
          value: fila.total,
          name: fila.etiqueta,
          itemStyle: {
            color: fila.color,
            // El valor dentro de la barra solo se pinta si la barra es más ancha que el texto.
            // Un `7` de tres píxeles de ancho es indistinguible del ruido, y cinco números
            // amontonados leen peor que un hueco limpio.
            label: {
              show: fila.total > 0,
              position: 'insideRight' as const,
              color: palette.background,
              fontSize: 11,
              fontFamily: 'JetBrains Mono, monospace',
            },
          },
          label: {
            show: true,
            position: 'right' as const,
            distance: 8,
            color: palette.caption,
            fontSize: 11,
            fontFamily: 'JetBrains Mono, monospace',
          },
        })),
        labelLayout: { hideOverlap: true },
      },
    ],
  }
}

/** Una serie de la línea temporal, con su nombre ya traducido. */
export interface SerieTemporal {
  clave: SeverityColorKey
  nombre: string
  valores: number[]
}

/**
 * Línea **apilada** por severidad sobre los días de la ventana.
 *
 * ## Por qué apilada y no cinco líneas sueltas
 *
 * Por lo que se lee primero: la silueta de la línea de arriba es la producción total de
 * hallazgos, y su pendiente es lo único que responde a «¿está improving o empeorando?». Con
 * cinco líneas sueltas encima de la otra hay que comparar cinco pendientes para llegar a la
 * misma conclusión, y las líneas que bajan por la mitad se tapan entre sí. Apiladas, cada
 * banda conserva su grosor —su propia severidad— y el total se lee por la silueta.
 *
 * ## Por qué área y no solo línea
 *
 * Porque el grosor de la banda es la lectura, y una banda sin relleno se lee como cinco
 * líneas que casualmente no se cruzan. El relleno es opaco, no degradado: `design-dark.md`
 * prohíbe los degradados, y aquí además un relleno suave haría que el amarillo de `MEDIUM` se
 * mezclara con el naranja de `HIGH` justo donde ambos se tocan, que es exactamente la
 * frontera donde el ojo necesita ver el corte.
 *
 * ## Por qué el último punto es el único etiquetado
 *
 * Porque treinta días por cinco severidades son ciento cincuenta rótulos, y eso no es un
 * gráfico: es ruido con líneas detrás. El valor de cada serie se escribe en su último punto,
 * que es el dato que la gráfica responde —«cómo termina el mes»— y el resto se lee en la
 * pendiente. Los valores exactos de todos los días están en la tabla para lectores de
 * pantalla que acompaña al canvas y en el tooltip al pasar por encima.
 */
export function opcionLineaApilada(
  puntos: { dia: string; total: number }[],
  series: SerieTemporal[],
  palette: ChartPalette,
  textos: { total: string; eje: string },
): ChartOption {
  const ultimoIndice = puntos.length - 1

  return {
    backgroundColor: 'transparent',
    aria: {
      enabled: true,
      decal: { show: false },
      description: textos.total,
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: palette.borderHover } },
      backgroundColor: palette.surfaceElevated,
      borderColor: palette.borderHover,
      textStyle: { color: palette.primary, fontFamily: 'JetBrains Mono, monospace', fontSize: 12 },
      extraCssText: 'box-shadow: none;',
    },
    legend: {
      top: 0,
      right: 0,
      icon: 'roundRect',
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 12,
      textStyle: { ...TEXTO_EJE, color: palette.caption },
    },
    // `top: 30` deja sitio a la leyenda de arriba. Mismo par que en las barras: `outerBounds` en vez
    // de `containLabel`, porque ECharts 6 lo marca como obsoleto y avisa por consola en cada
    // pintado. Ver el comentario de `opcionBarrasReparto`.
    grid: {
      left: 8,
      right: 16,
      top: 30,
      bottom: 8,
      outerBoundsMode: 'same',
      outerBoundsContain: 'axisLabel',
    },
    xAxis: {
      ...ejeCategoria(palette),
      boundaryGap: false,
      data: puntos.map((punto) => punto.dia),
    },
    yAxis: ejeValor(palette),
    series: series.map((serie) => ({
      name: serie.nombre,
      type: 'line' as const,
      stack: 'total',
      smooth: false,
      showSymbol: false,
      symbol: 'circle',
      symbolSize: 5,
      lineStyle: { width: 1.5, color: colorDeSeveridad(palette, serie.clave) },
      itemStyle: { color: colorDeSeveridad(palette, serie.clave) },
      areaStyle: { color: colorDeSeveridad(palette, serie.clave), opacity: 0.16 },
      // El valor del último día es la respuesta a «cómo termina el periodo». Ponerlo en todos
      // los puntos produciría ciento cincuenta rótulos; no ponerlo dejaría la gráfica muda.
      endLabel: {
        show: true,
        color: palette.caption,
        fontSize: 11,
        fontFamily: 'JetBrains Mono, monospace',
        formatter: (params: { seriesName?: string; value?: unknown }) => {
          const valor = typeof params.value === 'number' ? params.value : 0
          return valor > 0 ? `${params.seriesName ?? ''} ${valor}` : ''
        },
      },
      emphasis: { focus: 'series' as const },
      data: serie.valores.map((valor, indice) => ({
        value: valor,
        label:
          indice === ultimoIndice
            ? {
                show: valor > 0,
                position: 'top' as const,
                color: palette.caption,
                fontSize: 11,
                fontFamily: 'JetBrains Mono, monospace',
                formatter: (params: { value?: unknown }) => String(params.value ?? ''),
              }
            : { show: false },
      })),
    })),
  }
}

/**
 * Las cinco severidades como puntos de reparto, con su color y su etiqueta ya resueltas.
 *
 * ## Por qué existe
 *
 * Porque el mapeo «`CRITICAL` va al token `--color-critical`» está escrito en cuatro pantallas y
 * cada vez que se añade una hay que acordarse. Aquí el orden de `SEVERITY_KEYS` es el del
 * modelo, y quien llama solo tiene que pasar los textos ya traducidos. Un orden distinto en el
 * esquema y en la lista de colores produce un gráfico donde el rojo es la barra de abajo, y
 * eso no falla en ninguna parte: se ve.
 */
export function puntosDeSeveridad(
  conteos: ReadonlyArray<{ severity: string; total: number }>,
  palette: ChartPalette,
  etiqueta: (severity: string) => string,
): RepartoPunto[] {
  const porClave = new Map(conteos.map((conteo) => [conteo.severity, conteo.total]))
  return SEVERITY_ORDER.map((severity, indice) => {
    const clave = SEVERITY_KEYS[indice]!
    return {
      clave,
      etiqueta: etiqueta(severity),
      total: porClave.get(severity) ?? 0,
      color: colorDeSeveridad(palette, clave),
    }
  })
}

/** El orden del ciclo de vida de la remediación, que es el de `_STATUS_ORDER` en el servidor. */
export const STATUS_ORDER = [
  'OPEN',
  'IN_PROGRESS',
  'REMEDIATION_PROPOSED',
  'FIXED',
  'SNOOZED',
  'IGNORED',
] as const

/**
 * Los estados como puntos de reparto, en el orden del ciclo de vida.
 *
 * ## Por qué el color se decide aquí y no en quien llama
 *
 * Porque hay una razón de sistema para que **todos** los estados compartan tono salvo uno, y
 * esa razón no pertenece a la pantalla: si cada estado tuviera su color, el color dejaría de
 * significar severidad y empezaría a significar «estado de triaje», que es información que en
 * otra celda de la misma fila ya está escrita como texto. `design-dark.md` lo dice sin rodeos:
 * las píldoras de estado de remediación se quedan monocromas para que el color signifique una
 * sola cosa por celda. Lo único que se distingue es `FIXED`, en el acento del sistema, porque
 * «esto ya está resuelto» es la única afirmación de esta serie que tiene un color propio en el
 * resto de la interfaz.
 */
export function puntosDeEstado(
  conteos: ReadonlyArray<{ status: string; total: number }>,
  palette: ChartPalette,
  etiqueta: (status: string) => string,
): RepartoPunto[] {
  const porClave = new Map(conteos.map((conteo) => [conteo.status, conteo.total]))
  return STATUS_ORDER.map((estado) => ({
    clave: estado,
    etiqueta: etiqueta(estado),
    total: porClave.get(estado) ?? 0,
    color: estado === 'FIXED' ? palette.accent : palette.info,
  }))
}