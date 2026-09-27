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
 * ## Por qué el token se pega y no se elige de una lista
 *
 * Porque el panel **no** puede leer los tokens: la tabla guarda un hash. La única vez que el
 * valor en claro existe en el navegador es en la respuesta del alta, y esa respuesta no se
 * conserva. Un desplegable con los tokens del workspace serviría para que el usuario creyera
 * que puede elegir uno y acabaría con un `<API_TOKEN>` sin sustituir.
 *
 * El selector lista los tokens **para identificar cuál pegar** —nombre, prefijo, permisos—,
 * y el campo de al lado es donde se pega el valor. El campo no se persiste y se limpia al
 * salir de la pestaña: un secreto guardado en el estado de un componente sobrevive a la
 * navegación dentro de la sesión.
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
  const [tokenPegado, setTokenPegado] = useState('')
  const [copiado, setCopiado] = useState<'claude' | 'cursorrules' | null>(null)

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

  const copiar = async (que: 'claude' | 'cursorrules', texto: string) => {
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

  const tokensVigentes = useMemo(
    () => tokens.filter((token) => token.revoked_at === null),
    [tokens],
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

        <div className="form-field">
          <label className="mcp-field-label" htmlFor="mcp-token">
            {t('mcp.tokenLabel')}
          </label>
          <input
            id="mcp-token"
            className="text-input mono"
            type="password"
            value={tokenPegado}
            onChange={(event) => setTokenPegado(event.target.value)}
            placeholder={MARCADOR}
            autoComplete="off"
            spellCheck={false}
          />
          <span className="form-hint">{t('mcp.tokenHint')}</span>
        </div>

        {tokensVigentes.length > 0 ? (
          <div className="form-field">
            <span className="mcp-field-label">{t('mcp.availableTokens')}</span>
            <ul className="mcp-token-list">
              {tokensVigentes.map((token) => (
                <li key={token.id} className="mcp-token-item">
                  <span className="mcp-token-name">{token.name}</span>
                  <code className="mono">{token.token_prefix}</code>
                  <span className="form-hint">
                    {t('mcp.tokenScopes', { count: token.scopes.length })}
                  </span>
                </li>
              ))}
            </ul>
            <span className="form-hint">{t('mcp.recommendedScopes')}</span>
            <p className="mcp-scope-row mono">{SCOPES_RECOMENDADOS.join('  ')}</p>
          </div>
        ) : (
          <p className="mcp-note">{t('mcp.noTokens')}</p>
        )}

        {hayToken ? (
          <p className="mcp-warning" role="alert">
            <TriangleAlert size={16} aria-hidden="true" />
            <span>{t('mcp.downloadWarning')}</span>
          </p>
        ) : null}

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
