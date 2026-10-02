/**
 * El panel de agentes, que es lo único que comparten `/containers` y `/networks`.
 *
 * ## Por qué está en las dos pantallas y no en un sitio aparte
 *
 * Porque un escaneo sin agente no ocurre, y la pregunta que trae a alguien a esta pantalla casi
 * nunca es «¿qué ha salido?», sino «¿por qué no sale nada?». La respuesta habitual es que no hay
 * ningún agente dado de alta, o que el que hay no se ha conectado nunca. Poner el alta del
 * agente donde está el escaneo que la desbloquea hace que el desbloqueo se vea sin tener que ir
 * a buscarlo.
 *
 * Y se repite en las dos porque son dos pantallas distintas: quien está en `/networks` no
 * debería tener que saltar a `/containers` para registrar el agente que falta.
 *
 * ## Por qué el token se enseña **una vez** y la tabla no lo puede devolver
 *
 * Porque la plataforma guarda su SHA-256 y no el secreto, que es lo correcto. La consecuencia
 * es que si el operador cierra el modal sin copiarlo, el token está perdido y la única salida
 * es dar de baja el agente y registrar otro. Por eso el modal avisa antes de enseñar nada, y
 * el botón de cerrar dice lo que hay que hacer.
 *
 * ## Por qué «conectado» se decide al recibir los datos y no al pintar
 *
 * Porque decidirlo al pintar obliga a llamar a `Date.now()` **durante el render**, y eso es una
 * impureza: dos renders seguidos del mismo componente dan dos respuestas distintas y React no
 * sabe cuál es la buena. Con `StrictMode` en desarrollo se ve el doble, y en producción la
 * insignia puede cambiar de «conectado» a «sin conexión» sin que haya pasado nada.
 *
 * El precio de decidirlo aquí es que la insignia no se refresca sola: hay que recargar. Se
 * acepta, porque el resto de la pantalla tampoco se refresca sola, y un reloj por pantalla que
 * se congela a mitad de una sesión es peor que un botón.
 */

import { useEffect, useState } from 'react'
import { Cpu, Plus, ShieldOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { listAgents, revokeAgent } from '../../lib/agentsApi'
import type { AgentSummary, ScannerAgent } from '../../types/agents'
import { useAuth } from '../auth/useAuth'
import { AgentRegisterModal } from './AgentRegisterModal'
import { normalizaSistema } from '../../types/sistema'
import { AgentSetupGuide } from './AgentSetupGuide'

interface Props {
  resumen: AgentSummary | null
  onRegistered: () => void
  onRevoked: () => void
}

/** Un agente con la decisión de si está conectado ya tomada. */
type AgenteEnPantalla = ScannerAgent & { conectado: boolean }

export function AgentPanel({ resumen, onRegistered, onRevoked }: Props) {
  const { t } = useTranslation('agents')
  const { token, selectedOrganizationId } = useAuth()
  const [agents, setAgents] = useState<AgenteEnPantalla[]>([])
  const [loading, setLoading] = useState(true)
  const [registerOpen, setRegisterOpen] = useState(false)
  // La guia se abre con un boton y no con un `<details>` porque el caso al que sirve es el de un
  // agente que lleva rato sin conectar: eso no es un extra, es el siguiente paso, y un
  // `<summary>` de texto gris es justo lo que nadie abre cuando busca una pista de por que no
  // le funciona.
  const [guiaAbierta, setGuiaAbierta] = useState(false)

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    // La ventana llega del servidor, en vez de estar escrita aquí como constante, para que el
    // criterio sea **uno**: si el backend cambiara el plazo, las dos pantallas y el panel
    // cambiarían a la vez. Con un 300 en el frontend, un cambio en el backend dejaría la
    // insignia diciendo una cosa y el resumen otra.
    const ventana = resumen?.ventana_de_vida ?? 300
    void listAgents(token, selectedOrganizationId)
      .then((respuesta) => {
        if (!isActive) return
        const instante = Date.now()
        setAgents(
          respuesta.items.map((agente) => ({
            ...agente,
            conectado: agente.last_seen_at
              ? instante - new Date(agente.last_seen_at).getTime() < ventana * 1000
              : false,
          }))
        )
      })
      .catch(() => {
        // Los agentes son contexto, no el contenido de la pantalla. Si no se pueden listar, la
        // tabla de escaneos sigue valiendo y taparla sería tirar trabajo.
        if (isActive) setAgents([])
      })
      .finally(() => {
        if (isActive) setLoading(false)
      })
    return () => {
      isActive = false
    }
  }, [token, selectedOrganizationId, onRegistered, resumen])

  if (!token || !selectedOrganizationId) return null

  const vivos = resumen?.vivos ?? 0

  return (
    <div className="content-card">
      <div className="section-header">
        <div>
          <h2>{t('agent.title')}</h2>
          <p className="section-hint">{t('agent.whyShort')}</p>
        </div>
        <button
          type="button"
          className="secondary-button"
          onClick={() => setRegisterOpen(true)}
        >
          <Plus size={16} aria-hidden="true" />
          <span>{t('agent.register')}</span>
        </button>
      </div>

      {vivos === 0 && !loading && (
        <p className="inline-notice inline-notice-warning" role="status">
          {t('agent.noneConnected')}
        </p>
      )}

      {/* La guia se repite aqui, y sin token, por un motivo concreto: «nunca ha conectado» es el
          estado de un despliegue que arranco con el token mal pegado, y la respuesta a eso no es
          «dale a registrar otra vez» sino «mira estos tres pasos». La plataforma no puede
          devolverle el token al que lo registro —guarda su huella— asi que el bloque sale con un
          marcador, que es la unica cosa honesta que se puede mostrar. */}
      {!loading && agents.some((agente) => !agente.last_seen_at && agente.status === 'ACTIVE') && (
        <div className="setup-callout">
          <div className="setup-callout-body">
            <p className="setup-callout-title">{t('agent.setupCalloutTitle')}</p>
            <p>{t('agent.setupCalloutBody')}</p>
          </div>
          <button
            type="button"
            className="primary-button"
            onClick={() => setGuiaAbierta((v) => !v)}
          >
            {guiaAbierta ? t('agent.hideSetup') : t('agent.showSetup')}
          </button>
        </div>
      )}

      {guiaAbierta && (
        <AgentSetupGuide
          token={null}
          sistema={normalizaSistema(
            agents.find((agente) => agente.status === 'ACTIVE')?.sistema_objetivo
          )}
        />
      )}

      {agents.length === 0 && !loading ? (
        <div className="empty-card">
          <span className="empty-card-mark" aria-hidden="true">
          <Cpu size={20} />
        </span>
          <h3>{t('agent.emptyTitle')}</h3>
          <p>{t('agent.emptyBody')}</p>
          <button
            type="button"
            className="primary-button"
            onClick={() => setRegisterOpen(true)}
          >
            <Plus size={16} aria-hidden="true" />
            <span>{t('agent.register')}</span>
          </button>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('agent.table.name')}</th>
                <th>{t('agent.table.system')}</th>
                <th>{t('agent.table.enrolled')}</th>
                <th>{t('agent.table.lastSeen')}</th>
                <th aria-label={t('agent.table.actions')} />
              </tr>
            </thead>
            <tbody>
              {agents.map((agente) => (
                <tr key={agente.id}>
                  <td>
                    <span className="mono">{agente.name}</span>{' '}
                    <span className="cell-muted mono">{agente.token_prefix}…</span>
                  </td>
                  <td>
                    {/*
                      El sistema declarado es el que manda, porque es el que decide las
                      instrucciones. El medido va debajo, en pequeño y solo si lo hay: son dos
                      hechos distintos y meterlos en una columna con una flecha obligaría a
                      explicar la flecha.
                    */}
                    <span className="badge badge-muted">
                      {t(`agent.system.${normalizaSistema(agente.sistema_objetivo)}.name`)}
                    </span>
                    {agente.platform_hint && (
                      <span className="cell-muted mono setup-measured">
                        {agente.platform_hint}
                      </span>
                    )}
                  </td>
                  <td>{new Date(agente.enrolled_at).toLocaleDateString()}</td>
                  <td>
                    <AgentStateBadge agente={agente} />
                  </td>
                  <td className="cell-acciones">
                    {agente.status === 'ACTIVE' && (
                      <button
                        type="button"
                        className="ghost-button"
                        onClick={() => {
                          const motivo = window.prompt(t('agent.revokeReasonPlaceholder'))
                          if (motivo === null) return
                          void revokeAgent(
                            token,
                            selectedOrganizationId,
                            agente.id,
                            motivo || undefined
                          ).then(onRevoked)
                        }}
                      >
                        <ShieldOff size={16} aria-hidden="true" />
                        <span>{t('agent.revoke')}</span>
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {registerOpen && (
        <AgentRegisterModal
          onClose={() => setRegisterOpen(false)}
          onRegistered={() => {
            setRegisterOpen(false)
            onRegistered()
          }}
        />
      )}
    </div>
  )
}

/**
 * El estado de un agente: dado de baja, conectado, o sin conexión.
 *
 * Los tres casos se distinguen porque son tres cosas distintas. «Nunca ha conectado» no es
 * «sin conexión»: la primera significa que el despliegue no llegó a levantarse —el token está
 * mal puesto o el contenedor no arrancó— y la segunda que se cayó. Arreglarlas es distinto, y
 * un panel que colapsa los dos casos obliga al operador a mirar los logs de un despliegue que
 * quizá ni existe.
 */
function AgentStateBadge({ agente }: { agente: AgenteEnPantalla }) {
  const { t } = useTranslation('agents')
  if (agente.status === 'REVOKED') {
    return <span className="badge badge-muted">{t('agent.status.REVOKED')}</span>
  }
  if (!agente.last_seen_at) {
    return <span className="badge badge-warning">{t('agent.never')}</span>
  }
  return agente.conectado ? (
    <span className="badge badge-success">{t('agent.live')}</span>
  ) : (
    <span className="badge badge-muted">{t('agent.stale')}</span>
  )
}
