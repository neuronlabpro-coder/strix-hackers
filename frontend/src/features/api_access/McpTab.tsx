/**
 * Generadores de configuración para conectar un agente al servidor MCP.
 *
 * ## Por qué la URL se construye con `API_BASE_URL` y no con el origen de la ventana
 *
 * Porque el endpoint vive en la **API**, no en el panel. En desarrollo `API_BASE_URL` está
 * vacía y el proxy de Vite resuelve `/api/v1/mcp` contra el backend; en producción es la URL
 * absoluta de `api.`. La versión anterior usaba `window.location.origin`, que en desarrollo
 * funcionaba por casualidad —proxy, mismo origen— y en producción habría apuntado a
 * `panel.` y no a `api.`: una configuración correcta que no conecta, sin error visible.
 *
 * ## Por qué el desplegable **no** puede traer el token
 *
 * Porque el token **no existe** después de crearse. `api_tokens` guarda `token_hash`; el valor
 * en claro solo aparece en la respuesta del alta y no se persiste en ningún sitio. No es que
 * el panel no pueda leerlo: es que no hay de dónde.
 *
 * Y esa regla es correcta y no se toca. Si se guardara el secreto para poder mostrarlo, dejaría
 * de ser recuperable solo por quien loTiene —que sería quien entre en la base de datos, que es
 * justo el escenario que un hash protege— y además convertiría «se ha filtrado la base» en
 * «se han filtrado todas las credenciales de todos los clientes».
 *
 * ## Por qué el desplegable se llama de todas formas
 *
 * Porque el problema que resuelve no es «ver el token», es **«no saber cuál pegar»**. Antes el
 * flujo era: abrir otra pestaña, buscar el token por su nombre entre una lista, copiarlo de un
 * aviso que aparece una vez, cerrar, pegar en un archivo de configuración. El desplegable quita
 * los tres primeros pasos: se elige el token por su **nombre**, con sus **permisos** y su
 * **caducidad** a la vista, y se copia la configuración ya montada.
 *
 * Y el paso que queda —tener el valor en la mano— es el único que la regla de una sola vez
 * impone, y por eso el desplegable ofrece **dos salidas honestas**: usar el token si su valor
 * está a mano, o crear uno nuevo desde aquí con los permisos mínimos ya elegidos. Ambas están
 * en la interfaz porque las dos son reales, y ninguna finge recuperar un secreto que no existe.
 *
 * ## Por qué avisar de que el archivo descargado lleva el secreto
 *
 * Porque el archivo acaba en la carpeta de descargas, y en el caso de un agente de código,
 * dentro de un repositorio. Un `config.json` con un token en claro es una credencial
 * filtrada esperando a que alguien la use. El aviso va **en la interfaz**, no en un comentario
 * del código, porque el que decide dónde lo guarda es quien lo descarga.
 */

import { useEffect, useMemo, useState } from 'react'
import { Boxes, ClipboardCopy, Download, TriangleAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { API_BASE_URL } from '../../config'
import type { ApiToken } from '../../types/api'

interface McpTabProps {
  /** Los tokens del workspace, para que el usuario sepa cuál pegar. */
  tokens: ApiToken[]
}

/**
 * Los permisos mínimos que un agente necesita para las cuatro herramientas.
 *
 * Se declara aquí y no se deduce del catálogo porque es una **recomendación de
 * configuración**, no una regla del sistema: el usuario puede ampliar los permisos si su
 * agente lo necesita, y por eso el texto no dice "obligatorio" sino "lo habitual".
 */
const SCOPES_RECOMENDADOS = [
  'mcp:connect',
  'mcp:invoke',
  'repositories:read',
  'vulnerabilities:read',
  'assets:read',
] as const

/** El marcador que se genera cuando el usuario no ha pegado un token. */
const MARCADOR = '<API_TOKEN>'

export function McpTab({ tokens }: McpTabProps) {
  const { t } = useTranslation('apiAccess')
  const { t: tCommon } = useTranslation('common')
  const { i18n } = useTranslation()
  const [tokenPegado, setTokenPegado] = useState('')
  const [copiado, setCopiado] = useState<string | null>(null)
  /**
   * El token **elegido**, por identificador, no el secreto.
   *
   * ## Por qué es un `id` y no el token
   *
   * Porque es lo único que el backend puede devolver, y porque usar un identificador evita
   * por construcción que el secreto llegue al estado de un componente. Un `useState` con el
   * valor en claro sobrevive a la navegación dentro de la sesión y acaba en cualquier volcado
   * del estado que se le haga a la página.
   *
   * ## Por qué el valor pegado se **descarta** al elegir otro token
   *
   * Porque si no, elegir un token distinto del que está pegado deja la configuración montada
   * con un token que el desplegable dice que no es, y el error no se ve hasta que el cliente
   * MCP devuelve `401` en un terminal del agente, a horas de distancia de quien lo causó.
   */
  const [tokenSeleccionado, setTokenSeleccionado] = useState<string>('')
  const locale = i18n.language

  const tokensVigentes = useMemo(
    () => tokens.filter((token) => token.revoked_at === null),
    [tokens],
  )

  const tokenElegido = useMemo(
    () => tokensVigentes.find((token) => token.id === tokenSeleccionado) ?? null,
    [tokenSeleccionado, tokensVigentes],
  )

  /**
   * Los scopes de la etiqueta de cada opción.
   *
   * Se cuenta la intersección con `SCOPES_RECOMENDADOS` en lugar de volcar los cinco nombres:
   * un desplegable con «mcp:connect, mcp:invoke, repositories:read, vulnerabilities:read,
   * assets:read» en cada opción es ilegible, y lo que hay que comparar de un vistazo es si el
   * token **alcanza** lo que el agente necesita. La lista completa, si hace falta, sale en el
   * detalle del token elegido.
   */
  const alcanceDe = (scopes: readonly string[]): number =>
    SCOPES_RECOMENDADOS.filter((scope) => scopes.includes(scope)).length

  // El token se borra al desmontar la pestaña. Sin esto, el secreto seguiria en el estado del
  // componente mientras el usuario navega por el panel dentro de la misma sesion.
  useEffect(() => {
    return () => {
      setTokenPegado('')
    }
  }, [])

  const url = `${API_BASE_URL}/api/v1/mcp`
  const hayToken = tokenPegado.trim() !== ''
  const credencial = hayToken ? `Bearer ${tokenPegado.trim()}` : `Bearer ${MARCADOR}`

  /**
   * La configuración de Claude Desktop.
   *
   * Va como `mcpServers` que es la clave que ese cliente lee, y el transporte es `http` con
   * la cabecera de autorización: el endpoint no es SSE y no lo sería aunque lo fuera, porque
   * una sola llamada JSON-RPC por petición es lo que necesita un agente que decide qué
   * preguntar y espera la respuesta antes de la siguiente.
   */
  const claudeConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            'mind-guard': {
              type: 'http',
              url,
              headers: { Authorization: credencial },
            },
          },
        },
        null,
        2,
      ),
    [url, credencial],
  )

  /**
   * La URL del perfil `core`.
   *
   * Se construye anadiendo el parametro a la URL del servidor y no con una constante propia,
   * para que cambiar el prefijo del router no deje esta direccion apuntando a un sitio que no
   * existe. El error de un 404 aqui es silencioso: el cliente MCP no muestra nada y el agente
   * simplemente no encuentra herramientas.
   */
  const urlCore = useMemo(() => `${url}?profile=core`, [url])

  /**
   * La configuracion de Cursor en `.cursor/mcp.json`.
   *
   * A diferencia del `.cursorrules` de abajo, este **si** es JSON y si lo lee el cliente: es el
   * formato de servidor MCP de Cursor. Se ofrecen los dos porque un usuario puede tener uno u
   * otro segun como haya configurado el editor, y no son intercambiables: un `.cursorrules` con
   * JSON dentro se ignora en silencio, y un `mcp.json` con instrucciones en prosa no conecta.
   */
  const cursorMcpConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            'mind-guard': {
              type: 'http',
              url,
              headers: { Authorization: credencial },
            },
          },
        },
        null,
        2,
      ),
    [url, credencial],
  )

  /**
   * El comando de alta de Claude Code.
   *
   * Va como comando y no como JSON porque `claude mcp add` **es** un comando: escribe en la
   * configuracion del propio Claude Code. Poner un bloque de JSON para algo que se registra con
   * una orden de terminal hace que el usuario busque donde pegar un fichero que no existe.
   *
   * La URL va entre comillas simples porque `claude mcp add` parte el resto de argumentos por
   * espacios, y una query como `?profile=core` con `&` se romperia sin ellas.
   */
  const claudeCodeCommand = useMemo(
    () => `claude mcp add --transport http fenix '${url}'`,
    [url],
  )

  /**
   * Las instrucciones para ChatGPT.
   *
   * En prosa y no en JSON porque el conector de ChatGPT se configura desde su interfaz, no
   * leyendo un fichero. El texto dice que se use la URL del perfil core y por que: el conjunto
   * reducido es el que responde bien sin que el usuario tenga que decidir que herramientas
   * concede.
   */
  const chatgptInstructions = useMemo(
    () =>
      [
        t('mcp.chatgptStep1'),
        `1. ${urlCore}`,
        '',
        t('mcp.chatgptStep2'),
        `2. ${t('mcp.chatgptAuth')}`,
        `3. ${credencial}`,
        '',
        t('mcp.chatgptStep3'),
        '',
      ].join('\n'),
    [urlCore, credencial, t],
  )

  /**
   * El `.cursorrules` de Cursor.
   *
   * Es texto plano con la misma información, porque Cursor **no** lee `mcpServers`: lee un
   * fichero de instrucciones que le dice al agente que use el servidor por HTTP. Por eso no
   * es el JSON de arriba con otro nombre: el cliente no lo entendería, y un `.cursorrules`
   * con JSON dentro se ignora en silencio.
   */
  const cursorRules = useMemo(
    () =>
      [
        '# Mind Guard Fenix Team',
        '',
        t('mcp.cursorIntro'),
        '',
        `- Endpoint MCP: \`${url}\``,
        `- Autenticación: cabecera \`Authorization: ${credencial}\``,
        '',
        '## Herramientas',
        '',
        `- \`list_repositories\`: repositorios vigilados. Necesita \`repositories:read\`.`,
        `- \`get_vulnerability_summary\`: recuento de hallazgos por severidad. Necesita \`vulnerabilities:read\`.`,
        `- \`get_asset_inventory\`: dominios y activos descubiertos. Necesita \`assets:read\`.`,
        `- \`trigger_pentest\`: lanza un escaneo y **consume créditos**. Necesita \`pentests:create\`. Pídele confirmación al usuario antes de invocarla.`,
        '',
        '## Protocolo',
        '',
        `Las llamadas van a \`${url}\` con \`Content-Type: application/json\` y un sobre JSON-RPC 2.0.`,
        'Herramientas: `tools/list` para el catálogo y `tools/call` para ejecutar.',
        '',
      ].join('\n'),
    [url, credencial, t],
  )

  const copiar = async (
    que: 'claude' | 'cursorrules' | 'cursorMcp' | 'chatgpt' | 'claudeCode' | 'url' | 'urlCore',
    texto: string,
  ) => {
    try {
      await navigator.clipboard.writeText(texto)
      setCopiado(que)
    } catch {
      // El portapapeles puede estar bloqueado. No es un fallo que merezca un aviso: el texto
      // esta en pantalla y se puede seleccionar a mano, que es lo que hace alguien en esa
      // situacion.
      setCopiado(null)
    }
  }

  const descargar = (nombre: string, texto: string) => {
    // Un `Blob` con la URL del objeto. Se libera justo despues del click: sin el
    // `revokeObjectURL` el blob se queda en memoria hasta que se recargue la pagina, y son
    // ficheros que contienen credenciales.
    const blob = new Blob([texto], { type: 'application/json;charset=utf-8' })
    const href = URL.createObjectURL(blob)
    const enlace = document.createElement('a')
    enlace.href = href
    enlace.download = nombre
    enlace.click()
    URL.revokeObjectURL(href)
  }

  /**
   * Los cuatro clientes, con su texto ya resuelto.
   *
   * Se declara como dato y no como cuatro bloques de JSX porque comparten exactamente lo mismo
   * —una etiqueta, una nota, un texto y un boton de copiar— y cuatro copias de ese esqueleto
   * divergen en cuanto uno recibe un boton de descarga que los demas no necesitan. Anadir un
   * quinto cliente es una linea aqui y nada mas.
   *
   * `texto()` es una funcion y no un valor porque los tres dependen de la URL y del token, que
   * el usuario puede cambiar: un array de cadenas las congelaria en el primer render y el
   * snippet mostraria la credencial anterior.
   */
  const CLIENTES = useMemo(
    () => [
      {
        clave: 'claude',
        tituloKey: 'mcp.clients.claude.title',
        notaKey: 'mcp.clients.claude.note',
        texto: () => claudeConfig,
      },
      {
        clave: 'chatgpt',
        tituloKey: 'mcp.clients.chatgpt.title',
        notaKey: 'mcp.clients.chatgpt.note',
        texto: () => chatgptInstructions,
      },
      {
        clave: 'cursorMcp',
        tituloKey: 'mcp.clients.cursor.title',
        notaKey: 'mcp.clients.cursor.note',
        texto: () => cursorMcpConfig,
      },
      {
        clave: 'claudeCode',
        tituloKey: 'mcp.clients.claudeCode.title',
        notaKey: 'mcp.clients.claudeCode.note',
        texto: () => claudeCodeCommand,
      },
    ] as const,
    [claudeConfig, chatgptInstructions, cursorMcpConfig, claudeCodeCommand],
  )

  return (
    <section role="tabpanel" aria-label={t('tabs.mcp')}>
      <div className="mcp-card">
        <div className="mcp-card-header">
          <Boxes size={22} aria-hidden="true" />
          <div>
            <h2>{t('mcp.title')}</h2>
            <p>{t('mcp.description')}</p>
          </div>
        </div>

        <p className="mcp-endpoint">
          <span className="eyebrow">{t('mcp.endpointLabel')}</span>
          <code className="mcp-command mono">{url}</code>
        </p>

        {/*
          El desplegable va **antes** del campo de pegar, y no después.

          ## Por qué este orden y no el inverso

          Porque el desplegable es la vía principal y el campo es el suplente. Al revés, el
          usuario ve un campo de contraseña vacío con un marcador que parece que hay que
          sustituir, y el selector de abajo queda como un añadido. En este orden la pantalla
          dice primero «elige un token» y después «si ya lo tienes a mano, pégalo», que es la
          jerarquía real de las dos opciones.

          Y el campo se **deshabilita** cuando hay un token elegido, porque el valor pegado y el
          elegido son dos credenciales distintas y la configuración tiene que usar una sola.
          Con los dos activos, el snippet salía con el token pegado y el detalle hablaba del
          elegido, que es el peor sitio para que esos dos se contradigan.
        */}
        <div className="form-field">
          <label className="mcp-field-label" htmlFor="mcp-token-select">
            {t('mcp.selectTokenLabel')}
          </label>
          <select
            id="mcp-token-select"
            className="select-input"
            value={tokenSeleccionado}
            disabled={tokensVigentes.length === 0}
            onChange={(event) => {
              setTokenSeleccionado(event.target.value)
              setTokenPegado('')
            }}
          >
            <option value="">{t('mcp.noTokenSelected')}</option>
            {tokensVigentes.map((token) => (
              <option key={token.id} value={token.id}>
                {t('mcp.tokenOption', {
                  name: token.name,
                  prefix: token.token_prefix,
                  scopes: alcanceDe(token.scopes),
                  total: SCOPES_RECOMENDADOS.length,
                })}
              </option>
            ))}
          </select>
          <span className="form-hint">{t('mcp.selectTokenHint')}</span>
        </div>

        {tokenElegido ? (
          <div className="mcp-token-detail">
            <div className="mcp-token-detail-head">
              <span className="mcp-token-name">{tokenElegido.name}</span>
              <code className="mono">{tokenElegido.token_prefix}</code>
              <span className={`badge badge-token-${tokenElegido.token_type.toLowerCase()}`}>
                {t(`mcp.tokenType.${tokenElegido.token_type}`)}
              </span>
            </div>
            <dl className="mcp-token-facts">
              <div>
                <dt>{t('mcp.tokenExpires')}</dt>
                <dd className="mono">
                  {tokenElegido.expires_at
                    ? new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(
                        new Date(tokenElegido.expires_at),
                      )
                    : tCommon('values.never')}
                </dd>
              </div>
              <div>
                <dt>{t('mcp.tokenLastUsed')}</dt>
                <dd className="mono">
                  {tokenElegido.last_used_at
                    ? new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(
                        new Date(tokenElegido.last_used_at),
                      )
                    : t('mcp.tokenNeverUsed')}
                </dd>
              </div>
            </dl>
            <p className="mcp-scope-title">{t('mcp.tokenScopesTitle')}</p>
            <ul className="mcp-scope-list">
              {tokenElegido.scopes.map((scope) => (
                <li key={scope} className="mono">
                  {scope}
                </li>
              ))}
            </ul>
            {/*
              El aviso de que el valor no se puede recuperar sale **siempre** que hay un token
              elegido, y no solo cuando falta el pegado. Es la información que decide el flujo:
              sin él, el usuario elige un token, copia la configuración con el marcador, la
              pega en su cliente y no funciona; y no hay ningún mensaje que le diga por qué,
              porque desde la pantalla la configuración parece completa.
            */}
            {tokenPegado.trim() === '' ? (
              <p className="mcp-warning" role="status">
                <TriangleAlert size={16} aria-hidden="true" />
                <span>{t('mcp.valueUnavailable')}</span>
              </p>
            ) : null}
          </div>
        ) : null}

        <div className="form-field">
          <label className="mcp-field-label" htmlFor="mcp-token">
            {t('mcp.tokenLabel')}
          </label>
          <input
            id="mcp-token"
            className="text-input mono"
            type="password"
            value={tokenPegado}
            disabled={tokenElegido !== null}
            onChange={(event) => setTokenPegado(event.target.value)}
            placeholder={MARCADOR}
            autoComplete="off"
            spellCheck={false}
          />
          <span className="form-hint">{t('mcp.tokenHint')}</span>
        </div>

        {/*
          La lista completa de tokens **desaparece** y la sustituye el desplegable de arriba.

          Antes era una lista informativa: nombre, prefijo y número de permisos, que es
          exactamente la información que ahora está en el `<option>` elegido y en su detalle.
          Mantener las dos era mostrar lo mismo dos veces, con la lista enteriza sin ninguna
          acción: se podía leer que un token tenía cinco permisos y no se podía hacer nada con
          esa información. El desplegable la convierte en una elección.

          Y los permisos recomendados pasan a estar **en el texto de la opción**, no debajo de
          la lista, porque es la pregunta que responde «¿este token sirve para mi agente?».
        */}
        {tokensVigentes.length === 0 ? (
          <p className="mcp-note">{t('mcp.noTokens')}</p>
        ) : (
          <div className="form-field">
            <span className="mcp-field-label">{t('mcp.availableTokens')}</span>
            <span className="form-hint">{t('mcp.recommendedScopes')}</span>
            <p className="mcp-scope-row mono">{SCOPES_RECOMENDADOS.join('  ')}</p>
            <p className="mcp-note">{t('mcp.oneTimeRule')}</p>
          </div>
        )}

        {hayToken ? (
          <p className="mcp-warning" role="alert">
            <TriangleAlert size={16} aria-hidden="true" />
            <span>{t('mcp.downloadWarning')}</span>
          </p>
        ) : null}

        {/*
          Las dos direcciones van primero y como campos copiables, no dentro de los JSON de
          abajo. Quien va a configurar un cliente a mano solo quiere la URL, y pedirle que la
          lea dentro de un bloque de configuracion es un paso de mas que se resuelve siempre
          copiando.
        */}
        <div className="form-field">
          <span className="mcp-field-label">{t('mcp.serverUrlLabel')}</span>
          <div className="mcp-url-row">
            <code className="mono mcp-url-value">{url}</code>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void copiar('url', url)}
            >
              <ClipboardCopy size={16} aria-hidden="true" />
              <span>{copiado === 'url' ? t('mcp.copied') : t('mcp.copy')}</span>
            </button>
          </div>
          <span className="form-hint">{t('mcp.serverUrlHint')}</span>
        </div>

        <div className="form-field">
          <span className="mcp-field-label">{t('mcp.coreUrlLabel')}</span>
          <div className="mcp-url-row">
            <code className="mono mcp-url-value">{urlCore}</code>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void copiar('urlCore', urlCore)}
            >
              <ClipboardCopy size={16} aria-hidden="true" />
              <span>{copiado === 'urlCore' ? t('mcp.copied') : t('mcp.copy')}</span>
            </button>
          </div>
          <span className="form-hint">{t('mcp.coreUrlHint')}</span>
        </div>

        <div className="form-field">
          <span className="mcp-field-label">{t('mcp.clientsLabel')}</span>
          <div className="mcp-client-grid">
            {CLIENTES.map((cliente) => (
              <article key={cliente.clave} className="mcp-client-card">
                <header className="mcp-client-head">
                  <h3 className="mcp-client-title">{t(cliente.tituloKey)}</h3>
                  <p className="mcp-client-note">{t(cliente.notaKey)}</p>
                </header>
                <pre className="mcp-config mcp-client-config">
                  <code className="mono">{cliente.texto()}</code>
                </pre>
                <div className="mcp-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void copiar(cliente.clave, cliente.texto())}
                  >
                    <ClipboardCopy size={16} aria-hidden="true" />
                    <span>{copiado === cliente.clave ? t('mcp.copied') : t('mcp.copy')}</span>
                  </button>
                </div>
              </article>
            ))}
          </div>
        </div>

        <div className="form-field">
          <span className="mcp-field-label">{t('mcp.claudeLabel')}</span>
          <pre className="mcp-config">
            <code className="mono">{claudeConfig}</code>
          </pre>
          <div className="mcp-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => void copiar('claude', claudeConfig)}
            >
              <ClipboardCopy size={16} aria-hidden="true" />
              <span>
                {copiado === 'claude' ? t('mcp.copied') : t('mcp.copy')}
              </span>
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => descargar('claude_desktop_config.json', claudeConfig)}
            >
              <Download size={16} aria-hidden="true" />
              <span>{t('mcp.downloadClaude')}</span>
            </button>
          </div>
        </div>

        <div className="form-field">
          <span className="mcp-field-label">{t('mcp.cursorLabel')}</span>
          <pre className="mcp-config">
            <code className="mono">{cursorRules}</code>
          </pre>
          <div className="mcp-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => void copiar('cursorrules', cursorRules)}
            >
              <ClipboardCopy size={16} aria-hidden="true" />
              <span>
                {copiado === 'cursorrules' ? t('mcp.copied') : t('mcp.copy')}
              </span>
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => descargar('.cursorrules', cursorRules)}
            >
              <Download size={16} aria-hidden="true" />
              <span>{t('mcp.downloadCursor')}</span>
            </button>
          </div>
        </div>

        <p className="mcp-note">{t('mcp.scopesNote')}</p>
      </div>
    </section>
  )
}
