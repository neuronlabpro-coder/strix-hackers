/**
 * Alta de un agente de escaneo, y la **única** vez que se ve su token.
 *
 * ## Por qué el token se enseña en un modal y no en la tabla
 *
 * Porque no se puede volver a pedir. La plataforma guarda su SHA-256 y no el secreto, que es lo
 * correcto, y de eso sale la consecuencia: si el operador cierra el modal sin copiarlo, el token
 * está perdido y la única salida es dar de baja el agente y registrar otro.
 *
 * Por eso el modal no se cierra con un clic fuera ni con la `Escape` cuando el token ya está en
 * pantalla, y por eso el aviso va **antes** de enseñarlo y no después: un token que se pierde
 * con un `Enter` de más es el peor fallo posible de esta pantalla, y uno que se pierde con un
 * clic accidental también.
 *
 * ## Por qué no hay «solo mostrar una vez» como en otras herramientas
 *
 * Porque en este caso el valor se muestra **una** vez y no hay opción. En una herramienta que
 * deja volver a pedirlo, el botón evita elAccidente; aquí no hay botón posible, y decirlo sería
 * prometer algo que el sistema no puede hacer.
 */

import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { createAgent } from '../../lib/agentsApi'
import type { AgentEnrolled } from '../../types/agents'
import { AgentSetupGuide } from './AgentSetupGuide'
import type { SistemaObjetivo } from '../../types/agents'
import { useAuth } from '../auth/useAuth'

interface Props {
  onClose: () => void
  onRegistered: () => void
}

export function AgentRegisterModal({ onClose, onRegistered }: Props) {
  const { t } = useTranslation('agents')
  const { token, selectedOrganizationId } = useAuth()
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creado, setCreado] = useState<AgentEnrolled | null>(null)
  // El sistema se elige en el alta, antes que nada, porque es lo que decide qué instrucciones
  // de despliegue se enseñan. Sin esto, un operador con un Windows Server se encontraba una
  // guia de systemd.
  const [sistema, setSistema] = useState<SistemaObjetivo>('linux')
  const [copiado, setCopiado] = useState(false)

  if (!token || !selectedOrganizationId) return null

  async function registrar() {
    if (!name.trim()) {
      setError(t('agent.form.nameRequired'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const respuesta = await createAgent(token as string, selectedOrganizationId as string, {
        name: name.trim(),
        sistema_objetivo: sistema,
      })
      setCreado(respuesta)
    } catch (fallo) {
      setError(fallo instanceof Error ? fallo.message : String(fallo))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onKeyDown={(evento) => {
        // Con el token a la vista, la `Escape` no cierra. Perder un token por una tecla es
        // justo el fallo que este modal existe para evitar.
        if (evento.key === 'Escape' && !creado) {
          onClose()
        }
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="agent-register-title">
        <div className="modal-header">
          <h2 id="agent-register-title">
            {creado ? t('agent.registered') : t('agent.register')}
          </h2>
        </div>

        <div className="modal-body">
          {creado ? (
            <>
              <div className="inline-notice inline-notice-warning">
                <p>
                  <strong>{t('agent.tokenShownOnce')}</strong>
                </p>
                <p>{t('agent.tokenWarning')}</p>
              </div>
              <div className="token-row">
                <code className="token-block">{creado.token}</code>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    void navigator.clipboard
                      ?.writeText(creado.token)
                      .then(() => setCopiado(true))
                      .catch(() => setCopiado(false))
                  }}
                  aria-label={t('agent.copyToken')}
                >
                  {copiado ? (
                    <Check size={16} aria-hidden="true" />
                  ) : (
                    <Copy size={16} aria-hidden="true" />
                  )}
                  <span>{copiado ? t('agent.copied') : t('agent.copyToken')}</span>
                </button>
              </div>
              <p className="field-hint">{t('agent.tokenNextStep')}</p>
              {/* La guia va aqui, no en otra pantalla: el token se entrega una vez, y el
                  paso siguiente es exactamente el que hay que dar a los cinco segundos de
                  haberlo visto. Enseñarla despues, en la tabla, es tarde. */}
              <AgentSetupGuide token={creado.token} sistema={sistema} />
            </>
          ) : (
            <>
              <p className="modal-intro">{t('agent.form.intro')}</p>
              <label className="field">
                <span>{t('agent.form.name')}</span>
                <input
                  type="text"
                  value={name}
                  onChange={(evento) => setName(evento.target.value)}
                  placeholder={t('agent.form.namePlaceholder')}
                  autoFocus
                  onKeyDown={(evento) => {
                    if (evento.key === 'Enter') void registrar()
                  }}
                />
              </label>

              <fieldset className="field setup-system-fieldset">
                <legend>{t('agent.form.system')}</legend>
                <p className="field-hint">{t('agent.form.systemWhy')}</p>
                {(['linux', 'windows', 'macos', 'desconocido'] as const).map((opcion) => (
                  <label key={opcion} className="setup-system-option">
                    <input
                      type="radio"
                      name="sistema-objetivo"
                      value={opcion}
                      checked={sistema === opcion}
                      onChange={() => setSistema(opcion)}
                    />
                    <span>
                      <strong>{t(`agent.system.${opcion}.name`)}</strong>
                      <span className="cell-muted">{t(`agent.system.${opcion}.hint`)}</span>
                    </span>
                  </label>
                ))}
              </fieldset>
              {error && (
                <p className="form-error" role="alert">
                  {error}
                </p>
              )}
            </>
          )}
        </div>

        <div className="modal-footer">
          {creado ? (
            <button type="button" className="primary-button" onClick={onRegistered}>
              {t('agent.done')}
            </button>
          ) : (
            <>
              <button type="button" className="ghost-button" onClick={onClose}>
                {t('common:cancel')}
              </button>
              <button
                type="button"
                className="primary-button"
                onClick={() => void registrar()}
                disabled={busy || !name.trim()}
              >
                {t('agent.register')}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
