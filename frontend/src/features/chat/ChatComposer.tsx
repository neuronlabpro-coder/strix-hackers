import { useEffect, useRef, useState } from 'react'
import { Code2, Globe, KeyRound, Send, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ChatContextOptions } from '../../types/api'

/**
 * La caja de escribir, con los tres botones de alcance.
 *
 * ## Por que los botones de contexto **editan la caja** y no inyectan nada solos
 *
 * Porque un boton que dice "Credenciales" y pega un secreto en un prompt sin que el usuario vea
 * cual es la forma mas facil de que un secreto acabe en un tercero. El proveedor de inferencia
 * no es el propietario del dato, y mandarle material sensible sin que la persona lo vea decidir
 * es tomar una decision de seguridad por ella.
 *
 * Asi que los botones abren un pequeño editor, lo que el usuario escriba se ve, y lo que se
 * manda es exactamente lo que esta en pantalla. El boton de credenciales, ademas, no transporta
 * el material: solo activa una bandera que le dice al modelo "hay material sensible, no lo
 * repitas", y el material viaja dentro del texto que la persona puede leer antes de enviarlo.
 *
 * ## Por que el alcance vive en `context_options` y no dentro del texto
 *
 * Porque el texto es lo que el usuario lee y el modelo interpreta; el alcance es lo que el
 * backend **afirma** de forma estructurada. Meterlo en el texto seria pedirle al modelo que se
 * auto-imponga una restriccion, cuando lo correcto es que se la impongamos nosotros. Ademas es
 * lo que permite que el backend lo valide: un `extra="forbid"` rechaza una clave desconocida, y
 * con texto libre no hay nada que rechazar.
 */

/**
 * Cuantos dominios caben en el alcance.
 *
 * Es un tope **real**: el editor deja de anadir al llegar aqui. El backend acepta veinte, que es
 * un margen amplio para quien llame por API, pero un prompt con veinte dominios enumerados pesa
 * mas que la pregunta que los trajo y el modelo empieza a responder sobre activos que no estan
 * en juego. Con cuatro cabe el contexto del cliente sin que desplace a la pregunta.
 */
const MAXIMO_DE_DOMINIOS = 4

export interface ChatComposerProps {
  isSending: boolean
  sendFailed: boolean
  value: string
  onValueChange: (value: string) => void
  onSend: (options?: ChatContextOptions) => void
  onRetry: () => void
}

type PanelAbierto = 'credenciales' | 'dominios' | 'repositorios' | null

export function ChatComposer({
  isSending,
  sendFailed,
  value,
  onValueChange,
  onSend,
  onRetry,
}: ChatComposerProps) {
  const { t } = useTranslation('chat')
  const [panel, setPanel] = useState<PanelAbierto>(null)
  const [credenciales, setCredenciales] = useState(false)
  const [dominios, setDominios] = useState<string[]>([])
  const [repositorios, setRepositorios] = useState<string[]>([])
  const [borradorDominios, setBorradorDominios] = useState('')
  const [borradorRepos, setBorradorRepos] = useState('')
  const areaRef = useRef<HTMLTextAreaElement | null>(null)

  const puedeEnviar = value.trim().length > 0 && !isSending

  // El area crece con su contenido hasta un tope, y a partir de ahi scrollea. Sin el tope, un
  // prompt largo empuja el hilo entero fuera de la pantalla; sin el crecimiento, un prompt
  // largo se ve entero en una sola linea y no se puede revisar antes de enviarlo.
  useEffect(() => {
    const area = areaRef.current
    if (!area) return
    area.style.height = 'auto'
    area.style.height = `${Math.min(area.scrollHeight, 220)}px`
  }, [value])

  function alternarPanel(destino: Exclude<PanelAbierto, null>): void {
    setPanel((actual) => (actual === destino ? null : destino))
  }

  function anadirALista(
    destino: 'dominios' | 'repositorios',
    borrador: string,
    limpiar: (valor: string) => void,
  ): void {
    const valor = borrador.trim().toLowerCase()
    if (!valor) return
    const lista = destino === 'dominios' ? dominios : repositorios
    if (lista.includes(valor)) {
      limpiar('')
      return
    }
    // El tope se **impone**, no se avisa. Un aviso que dice "solo se envian cuatro" mientras
    // el campo acepta diez es una afirmacion falsa que el usuario descubre al mandar: el
    // backend acepta veinte, el prompt sale con diez, y el analisis se ha limitado a una lista
    // de dominios que nadie acoto. Si el usuario quiere mas, lo dice en el mensaje.
    if (destino === 'dominios' && lista.length >= MAXIMO_DE_DOMINIOS) {
      limpiar('')
      return
    }
    if (destino === 'dominios') {
      setDominios((current) => [...current, valor])
    } else {
      setRepositorios((current) => [...current, valor])
    }
    limpiar('')
  }

  function enviar(): void {
    if (!puedeEnviar) return
    const hayAlcance = credenciales || dominios.length > 0 || repositorios.length > 0
    // El objeto se omite entero cuando no hay nada acotado. Mandar un `context_options` de
    // ceros documentaria en el trafico de red que el panel ha considerado el alcance cuando en
    // realidad no ha considerado nada.
    onSend(
      hayAlcance
        ? { credenciales_de_contexto: credenciales, dominios, repositorios }
        : undefined,
    )
    onValueChange('')
  }

  function alPulsarEnter(event: React.KeyboardEvent<HTMLTextAreaElement>): void {
    // Enter envia y Shift+Enter parte linea. Es la convencion que espera cualquiera que haya
    // usado un chat, y ademas es la unica que se puede tener sin documentar: en un panel de
    // seguridad, donde los prompts son largos y con saltos, mandar con Enter sin querer es
    // gastar un turno y unos creditos.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      enviar()
    }
  }

  return (
    <div className="chat-composer">
      {sendFailed && (
        <div className="chat-composer-error" role="alert">
          <span>{t('composer.sendFailed')}</span>
          <button className="secondary-button" type="button" onClick={onRetry}>
            {t('composer.retry')}
          </button>
        </div>
      )}

      <div className="chat-composer-box">
        <textarea
          ref={areaRef}
          className="chat-composer-input"
          value={value}
          rows={1}
          placeholder={t('composer.placeholder')}
          onChange={(event) => onValueChange(event.target.value)}
          onKeyDown={alPulsarEnter}
          disabled={isSending}
          aria-label={t('composer.placeholder')}
        />
        <button
          className="primary-button chat-composer-send"
          type="button"
          onClick={enviar}
          disabled={!puedeEnviar}
        >
          <Send size={16} aria-hidden="true" />
          {t('composer.send')}
        </button>
      </div>

      <div className="chat-scope">
        <button
          className={credenciales ? 'chat-scope-button chat-scope-active' : 'chat-scope-button'}
          type="button"
          aria-pressed={credenciales}
          onClick={() => {
            setCredenciales((actual) => !actual)
            alternarPanel('credenciales')
          }}
        >
          <KeyRound size={16} aria-hidden="true" />
          {t('scope.credentials')}
        </button>

        <button
          className={dominios.length > 0 ? 'chat-scope-button chat-scope-active' : 'chat-scope-button'}
          type="button"
          aria-pressed={dominios.length > 0}
          aria-expanded={panel === 'dominios'}
          onClick={() => alternarPanel('dominios')}
        >
          <Globe size={16} aria-hidden="true" />
          {t('scope.domains')}
          {dominios.length > 0 && <span className="chat-scope-badge">{dominios.length}</span>}
        </button>

        <button
          className={repositorios.length > 0 ? 'chat-scope-button chat-scope-active' : 'chat-scope-button'}
          type="button"
          aria-pressed={repositorios.length > 0}
          aria-expanded={panel === 'repositorios'}
          onClick={() => alternarPanel('repositorios')}
        >
          <Code2 size={16} aria-hidden="true" />
          {t('scope.repositories')}
          {repositorios.length > 0 && (
            <span className="chat-scope-badge">{repositorios.length}</span>
          )}
        </button>
      </div>

      {panel === 'credenciales' && (
        <div className="chat-scope-panel">
          <p className="chat-scope-panel-text">{t('scope.credentialsHelp')}</p>
          <p className="chat-scope-panel-warning">{t('scope.credentialsWarning')}</p>
        </div>
      )}

      {panel === 'dominios' && (
        <div className="chat-scope-panel">
          <label className="chat-scope-field">
            <span>{t('scope.domainsLabel')}</span>
            <input
              className="chat-scope-input"
              value={borradorDominios}
              onChange={(event) => setBorradorDominios(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  anadirALista('dominios', borradorDominios, setBorradorDominios)
                }
              }}
              placeholder={t('scope.domainsPlaceholder')}
            />
          </label>
          {dominios.length > 0 && (
            <ul className="chat-scope-list">
              {dominios.map((dominio) => (
                <li key={dominio}>
                  <span>{dominio}</span>
                  <button
                    type="button"
                    aria-label={t('scope.removeDomain', { domain: dominio })}
                    onClick={() => setDominios((current) => current.filter((d) => d !== dominio))}
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {dominios.length >= MAXIMO_DE_DOMINIOS && (
            <p className="chat-scope-panel-text">{t('scope.domainsLimit')}</p>
          )}
        </div>
      )}

      {panel === 'repositorios' && (
        <div className="chat-scope-panel">
          <label className="chat-scope-field">
            <span>{t('scope.repositoriesLabel')}</span>
            <input
              className="chat-scope-input"
              value={borradorRepos}
              onChange={(event) => setBorradorRepos(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  anadirALista('repositorios', borradorRepos, setBorradorRepos)
                }
              }}
              placeholder={t('scope.repositoriesPlaceholder')}
            />
          </label>
          {repositorios.length > 0 && (
            <ul className="chat-scope-list">
              {repositorios.map((repositorio) => (
                <li key={repositorio}>
                  <span>{repositorio}</span>
                  <button
                    type="button"
                    aria-label={t('scope.removeRepository', { repository: repositorio })}
                    onClick={() =>
                      setRepositorios((current) => current.filter((r) => r !== repositorio))
                    }
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <p className="chat-composer-footnote">{t('composer.footnote')}</p>
    </div>
  )
}
