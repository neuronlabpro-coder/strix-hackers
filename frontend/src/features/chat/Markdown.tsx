import { type ReactNode, useState } from 'react'

/**
 * Renderizador de Markdown para las respuestas del asistente.
 *
 * ## Por que esta aqui y no es `react-markdown`
 *
 * Por dos razones, y la segunda es la que decide.
 *
 * La primera es el control del subconjunto: una respuesta de un consultor de Red Team tiene
 * encabezados, listas, negritas, codigo en linea, bloques de codigo, citas y tablas. Es un
 * subconjunto acotado y estable, y renderizarlo aqui significa que se ve exactamente que se
 * soporta y que no.
 *
 * La segunda es la seguridad, y no es un argumento teorico. La respuesta del modelo es **texto
 * no confiable**: puede venir de un documento que el cliente pego, de un repositorio que se
 * escaneo o de un prompt inyectado. Este renderizador construye un arbol de elementos de React
 * y **nunca** produce una cadena de HTML, asi que no hay `dangerouslySetInnerHTML` en ninguna
 * parte y no hay superficie de inyeccion. Lo que no reconoce se muestra como texto literal, y
 * React lo escapa.
 *
 * Una libreria de Markdown necesita lo mismo para ser segura —parsear a elementos, no a
 * cadenas— pero ademas trae un motor de HTML embebido, reglas de referencia y un
 * sanitizador. Aqui no hay nada que sanear: si el modelo escribe `<script>`, el usuario ve
 * `<script>`.
 *
 * ## Que subconjunto cubre
 *
 * Encabezados de nivel 1 a 4, listas con vinetas y numeradas, negritas, cursivas, codigo en
 * linea, bloques de codigo con lenguaje, citas, reglas horizontales, parrafos y tablas con
 * cabecera.
 *
 * ## Que hace con lo que no cubre
 *
 * Lo muestra como texto. Una imagen, una lista de tareas o un anidamiento de tres niveles se
 * ven como caracteres, que es feo pero **no engañoso**: el usuario ve exactamente lo que el
 * modelo escribio. Un sanitizador que descarta lo que no reconoce tiene el problema opuesto: el
 * modelo cree haber devuelto una lista y el usuario ve un parrafo, sin que nada avise.
 */

interface CodeBlockProps {
  language: string
  code: string
  copyLabel: string
  copiedLabel: string
  /** Texto accesible del boton cuando no hay lenguaje declarado. */
  fallbackLabel: string
}

function CodeBlock({ language, code, copyLabel, copiedLabel, fallbackLabel }: CodeBlockProps) {
  const [isCopied, setIsCopied] = useState(false)

  async function copy(): Promise<void> {
    // `navigator.clipboard` solo existe en contexto seguro, y el panel se sirve por HTTP en
    // desarrollo. Un `try` vacio seria un fallo silencioso: el usuario pulsa copiar, no ocurre
    // nada y no tiene forma de saber por que. Se refleja el estado en los dos casos para que
    // el boton siempre responda.
    try {
      await navigator.clipboard.writeText(code)
      setIsCopied(true)
      window.setTimeout(() => setIsCopied(false), 1600)
    } catch {
      setIsCopied(false)
    }
  }

  return (
    <div className="chat-code">
      <div className="chat-code-header">
        <span className="chat-code-language">{language || fallbackLabel}</span>
        <button
          className="chat-code-copy"
          type="button"
          onClick={() => void copy()}
          aria-label={isCopied ? copiedLabel : copyLabel}
        >
          {isCopied ? copiedLabel : copyLabel}
        </button>
      </div>
      <pre className="chat-code-body">
        <code>{code}</code>
      </pre>
    </div>
  )
}

/**
 * Trocea una linea en codigo en linea, negrita y cursiva.
 *
 * El orden de las alternativas del patron importa: el codigo en linea se busca primero porque
 * su contenido no debe interpretarse. Si la negrita se buscara antes, un `**` dentro de un
 * fragmento de codigo abriria un elemento que nunca se cierra, y el texto siguiente se
 * renderizaria en negrita hasta el final del bloque.
 */
const PATRON_EN_LINEA = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)/g

function inline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let cursor = 0
  let match: RegExpExecArray | null
  let index = 0

  PATRON_EN_LINEA.lastIndex = 0
  while ((match = PATRON_EN_LINEA.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index))
    }
    const codigo = match[1]
    const negrita = match[2]
    const cursiva = match[3]
    if (codigo !== undefined) {
      nodes.push(
        <code className="chat-inline-code" key={`${keyPrefix}-c${index}`}>
          {codigo.slice(1, -1)}
        </code>,
      )
    } else if (negrita !== undefined) {
      nodes.push(<strong key={`${keyPrefix}-b${index}`}>{negrita.slice(2, -2)}</strong>)
    } else if (cursiva !== undefined) {
      nodes.push(<em key={`${keyPrefix}-i${index}`}>{cursiva.slice(1, -1)}</em>)
    }
    cursor = match.index + match[0].length
    index += 1
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor))
  }
  return nodes
}

/** Una celda: recorta la tuberia exterior que Markdown pone en cada fila. */
function celda(texto: string): string {
  return texto.trim().replace(/^\|/, '').replace(/\|$/, '').trim()
}

const RE_LISTA_VINETA = /^\s*[-*+]\s+/
const RE_LISTA_NUMERADA = /^\s*\d+[.)]\s+/
const RE_ENCABEZADO = /^(#{1,4})\s+(.*)$/
const RE_VALLA = /^```(.*)$/
const RE_VALLA_CIERRE = /^```\s*$/
const RE_REGLA = /^(-{3,}|\*{3,}|_{3,})\s*$/
const RE_CITA = /^>\s?/
/** La fila de separacion de una tabla: solo guiones, dos puntos y barras. */
const RE_SEPARADOR_TABLA = /^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$/

/**
 * Si una linea **corta** el parrafo que se esta acumulando.
 *
 * Existe como funcion y no inline porque el bucle de parrafo la consulta para cada linea y la
 * tabla necesita exactamente el mismo criterio. Dos listas de condiciones que deciden el final
 * de un bloque tienen que coincidir: si una reconoce un patron que la otra no, la linea cae en
 * un hueco y el renderizador puede no avanzar.
 */
function cortaParrafo(linea: string): boolean {
  return (
    linea.trim() === '' ||
    linea.includes('|') ||
    RE_VALLA.test(linea) ||
    RE_ENCABEZADO.test(linea) ||
    RE_LISTA_VINETA.test(linea) ||
    RE_LISTA_NUMERADA.test(linea) ||
    RE_CITA.test(linea) ||
    RE_REGLA.test(linea)
  )
}

export interface MarkdownProps {
  content: string
  /**
   * Texto del boton de copiar. Se usa a la vez como texto visible y como etiqueta accesible,
   * y por eso **no** hay una segunda cadena para el `aria-label`: dos textos distintos para el
   * mismo boton se desincronizan en cuanto uno se cambia, y el que se queda es el que no se ve.
   * Cuando el texto visible es "Copiar", el lector de pantalla no necesita otra cosa.
   */
  copyLabel: string
  /** Texto del boton durante el instante posterior a copiar. */
  copiedLabel: string
  /** Nombre que se muestra cuando el bloque no declara lenguaje. */
  codeLabel: string
}

export function Markdown({ content, copyLabel, copiedLabel, codeLabel }: MarkdownProps) {
  const lineas = content.split('\n')
  const bloques: ReactNode[] = []
  let indice = 0
  let clave = 0

  while (indice < lineas.length) {
    const linea = lineas[indice] ?? ''

    // --- Bloque de codigo ------------------------------------------------ //
    const apertura = RE_VALLA.exec(linea)
    if (apertura) {
      const lenguaje = (apertura[1] ?? '').trim()
      const cuerpo: string[] = []
      indice += 1
      while (indice < lineas.length && !RE_VALLA_CIERRE.test(lineas[indice] ?? '')) {
        cuerpo.push(lineas[indice] ?? '')
        indice += 1
      }
      // Si no hay valla de cierre el bloque llega hasta el final del texto, y eso es lo
      // correcto: un bloque sin cerrar es un recorte de la respuesta, y tirar la respuesta
      // entera por una valla sin pareja seria peor que mostrarla a medias.
      indice += 1
      clave += 1
      bloques.push(
        <CodeBlock
          key={`code-${clave}`}
          language={lenguaje}
          code={cuerpo.join('\n')}
          copyLabel={copyLabel}
          copiedLabel={copiedLabel}
          fallbackLabel={codeLabel}
        />,
      )
      continue
    }

    if (linea.trim() === '') {
      indice += 1
      continue
    }

    // --- Regla horizontal ------------------------------------------------- //
    if (RE_REGLA.test(linea)) {
      clave += 1
      bloques.push(<hr className="chat-rule" key={`hr-${clave}`} />)
      indice += 1
      continue
    }

    // --- Encabezado -------------------------------------------------------- //
    const encabezado = RE_ENCABEZADO.exec(linea)
    if (encabezado) {
      const nivel = (encabezado[1] ?? '#').length
      const texto = encabezado[2] ?? ''
      // El nivel se limita a cuatro porque el CSS solo define cuatro. Sin el limite, un
      // `#####` de un modelo sobreexpresivo cae en el estilo por defecto de React y aparece con
      // el tamano de un parrafo, que es peor que mostrarlo pequeño.
      const Etiqueta = (['h3', 'h4', 'h5', 'h6'] as const)[Math.min(nivel, 4) - 1]
      clave += 1
      bloques.push(
        <Etiqueta className="chat-heading" key={`h-${clave}`}>
          {inline(texto, `h${clave}`)}
        </Etiqueta>,
      )
      indice += 1
      continue
    }

    // --- Cita -------------------------------------------------------------- //
    if (RE_CITA.test(linea)) {
      const cuerpo: string[] = []
      while (indice < lineas.length && RE_CITA.test(lineas[indice] ?? '')) {
        cuerpo.push((lineas[indice] ?? '').replace(RE_CITA, ''))
        indice += 1
      }
      clave += 1
      bloques.push(
        <blockquote className="chat-quote" key={`q-${clave}`}>
          {inline(cuerpo.join(' '), `q${clave}`)}
        </blockquote>,
      )
      continue
    }

    // --- Tabla -------------------------------------------------------------- //
    // Se exige que la linea siguiente sea un separador con al menos un guion. Sin esa
    // comprobacion, cualquier parrafo con una barra —una ruta, un comando de shell— se
    // interpretaria como el inicio de una tabla.
    const separador = lineas[indice + 1] ?? ''
    if (linea.includes('|') && separador.includes('|') && RE_SEPARADOR_TABLA.test(separador)) {
      const cabecera = linea.split('|').map(celda)
      indice += 2
      const filas: string[][] = []
      while (indice < lineas.length && (lineas[indice] ?? '').includes('|')) {
        filas.push((lineas[indice] ?? '').split('|').map(celda))
        indice += 1
      }
      clave += 1
      bloques.push(
        <div className="chat-table-wrapper" key={`t-${clave}`}>
          <table className="chat-table">
            <thead>
              <tr>
                {cabecera.map((texto, i) => (
                  <th key={i}>{inline(texto, `th${clave}-${i}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filas.map((fila, f) => (
                <tr key={f}>
                  {fila.map((valor, c) => (
                    <td key={c}>{inline(valor, `td${clave}-${f}-${c}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    // --- Listas ------------------------------------------------------------- //
    const patron = RE_LISTA_VINETA.test(linea)
      ? RE_LISTA_VINETA
      : RE_LISTA_NUMERADA.test(linea)
        ? RE_LISTA_NUMERADA
        : null
    if (patron) {
      const ElementoLista = RE_LISTA_VINETA.test(linea) ? 'ul' : 'ol'
      const items: string[] = []
      // Solo se acepta un nivel de sangria. Un modelo que anida tres niveles produce una lista
      // que el CSS no distingue del primer nivel y se ve como una linea larguisima. Aplanar es
      // peor que anidar bien, y anidar bien exige un parser que aqui no hay.
      while (indice < lineas.length && patron.test(lineas[indice] ?? '')) {
        items.push((lineas[indice] ?? '').replace(patron, ''))
        indice += 1
      }
      clave += 1
      bloques.push(
        <ElementoLista className="chat-list" key={`l-${clave}`}>
          {items.map((item, i) => (
            <li key={i}>{inline(item, `li${clave}-${i}`)}</li>
          ))}
        </ElementoLista>,
      )
      continue
    }

    // --- Parrafo ------------------------------------------------------------- //
    const parrafo: string[] = []
    while (indice < lineas.length && !cortaParrafo(lineas[indice] ?? '')) {
      parrafo.push(lineas[indice] ?? '')
      indice += 1
    }
    clave += 1
    if (parrafo.length > 0) {
      bloques.push(
        <p className="chat-paragraph" key={`p-${clave}`}>
          {inline(parrafo.join(' '), `p${clave}`)}
        </p>,
      )
    }
    // Si el parrafo ha quedado vacio, la linea actual corta por un motivo que ningun patron
    // anterior ha recogido —una linea con una barra que no forma tabla—. Se consume **una**
    // linea para no entrar en un bucle infinito. Es el unico sitio donde el renderizador puede
    // tragarse el resto del texto, y por eso consume exactamente una y no las que necesite.
    if (indice < lineas.length && parrafo.length === 0) {
      indice += 1
    }
  }

  return <>{bloques}</>
}
