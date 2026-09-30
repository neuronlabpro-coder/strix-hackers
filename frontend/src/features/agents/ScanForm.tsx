/**
 * Alta de un escaneo: una imagen de contenedor o una red, según la pantalla desde la que se
 * abre.
 *
 * ## Por qué el `kind` es un parámetro y no un selector dentro del modal
 *
 * Porque `/containers` y `/networks` son dos pantallas con dos intenciones distintas, y un
 * selector de tipo dentro del modal dejaría al usuario **eligiendo el tipo de escaneo en la
 * pantalla equivocada**: abrir «nuevo escaneo» en redes y que aparezca una casilla para
 * escribir `alpine:3.20` es un formulario que no tiene sentido ahí.
 *
 * El tipo llega desde la pantalla, así que el modal solo pide lo que esa pantalla sabe pedir.
 *
 * ## Por qué el destino se valida **aquí** y no solo en el servidor
 *
 * El servidor valida la forma en el esquema, y esto valida lo que el esquema no puede saber: que
 * un prefijo `/8` no es escaneable. Un `10.0.0.0/8` son 16 millones de direcciones; el agente lo
 * recortaría a 256 y lo declararía en el resultado, y eso es correcto, pero devuelve algo que no
 * es lo que el usuario pidió. Avisar antes es mejor que descubrirlo leyendo 256 hosts cuando se
 * pidieron 16 millones.
 *
 * ## Por qué los puertos no se eligen aquí
 *
 * Porque los pone la plataforma en el `claim`. Una lista elegida desde el navegador sería una
 * lista que alguien elige sin ver lo que el escaneo cuesta, y la política de qué se mira está
 * en el servidor, que es donde está el presupuesto.
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { createAgentJob } from '../../lib/agentsApi'
import type { AgentJobKind } from '../../types/agents'
import { useAuth } from '../auth/useAuth'

interface Props {
  kind: AgentJobKind
  onClose: () => void
  onCreated: () => void
}

/** El prefijo más ancho que se acepta sin recortar, en forma de su máscara. */
const PREFIJO_MAXIMO_UTIL = 22

function prefijoDe(cidr: string): number | null {
  const partes = cidr.split('/')
  if (partes.length !== 2) return null
  const prefijo = Number(partes[1])
  return Number.isInteger(prefijo) && prefijo >= 0 && prefijo <= 32 ? prefijo : null
}

export function ScanForm({ kind, onClose, onCreated }: Props) {
  const { t } = useTranslation('agents')
  const { token, selectedOrganizationId } = useAuth()
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!token || !selectedOrganizationId) return null

  const esRed = kind === 'NETWORK_SCAN'
  const espacio = esRed ? 'networks' : 'containers'
  const prefijo = esRed ? prefijoDe(target.trim()) : null
  const demasiadoAmplio = prefijo !== null && prefijo < PREFIJO_MAXIMO_UTIL

  async function enviar() {
    const valor = target.trim()
    if (!valor) {
      setError(t(`${espacio}.form.emptyTarget`))
      return
    }
    setBusy(true)
    setError(null)
    try {
      await createAgentJob(token as string, selectedOrganizationId as string, { kind, target: valor })
      onCreated()
    } catch (fallo) {
      setError(fallo instanceof Error ? fallo.message : String(fallo))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="scan-form-title">
        <div className="modal-header">
          <h2 id="scan-form-title">{t(`${espacio}.form.title`)}</h2>
        </div>

        <div className="modal-body">
          <p className="modal-intro">{t(`${espacio}.form.intro`)}</p>

          <label className="field">
            <span>{t(`${espacio}.form.target`)}</span>
            <input
              type="text"
              value={target}
              onChange={(evento) => setTarget(evento.target.value)}
              placeholder={t(`${espacio}.form.placeholder`)}
              autoFocus
              onKeyDown={(evento) => {
                if (evento.key === 'Enter') void enviar()
              }}
            />
          </label>

          <p className="field-hint">{t(`${espacio}.form.help`)}</p>

          {demasiadoAmplio && (
            <p className="inline-notice inline-notice-warning" role="alert">
              {t('networks.form.tooWide')}
            </p>
          )}

          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>

        <div className="modal-footer">
          <button type="button" className="ghost-button" onClick={onClose}>
            {t('common:cancel')}
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => void enviar()}
            disabled={busy || demasiadoAmplio || !target.trim()}
          >
            {t(`${espacio}.form.submit`)}
          </button>
        </div>
      </div>
    </div>
  )
}
