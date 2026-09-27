import { useState, type FormEvent } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { deactivateWorkspace } from '../../lib/workspaceApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'

/**
 * Baja lógica del workspace, con confirmación estricta.
 *
 * ## Por qué hay que escribir el nombre y no solo pulsar un botón
 *
 * Una baja revoca el acceso de todo el equipo y cancela la suscripción, y es la única
 * operación de la plataforma que **no** tiene vuelta atrás desde la interfaz. Un
 * `window.confirm` con dos botones no basta: el botón destructivo está en la misma posición
 * que el de cancelar, y un clic en la posición de siempre lo pulsa. Escribir el nombre
 * añade un paso que no se puede completar por inercia y, sobre todo, hace que quien lo
 * escribe **lea** lo que está borrando.
 *
 * ## Por qué el estado se reinicia en la **apertura**, no en el cierre
 *
 * El reinicio va en el manejador de abrir, y no en un efecto que vigila `open`. Con un
 * efecto, cerrar y reabrir dejaría el campo con lo escrito a medias, que es el peor sitio
 * para un texto a medias: parece una confirmación a medio hacer y hace que la segunda
 * apertura exija menos esfuerzo que la primera.
 *
 * ## Por qué el `404` tras la baja no se trata como error
 *
 * Porque la organización deja de existir en cuanto la baja se aplica, y cualquier petición
 * posterior que viaje con sus cabeceras devolverá `403` por no resolver contexto. Los dos
 * códigos son el resultado esperado de la operación, no un fallo, y mostrarlos como error
 * haría que el cliente creyera que la baja no se aplicó —cuando justamente sí— e intentara
 * de nuevo, ya sin efecto. La función del cliente trata el `404` como éxito; ver su nota.
 */
interface DangerZoneDialogProps {
  open: boolean
  workspaceName: string
  canDelete: boolean
  onOpenChange: (open: boolean) => void
  onDeleted: () => void
}

export function DangerZoneDialog({
  open,
  workspaceName,
  canDelete,
  onOpenChange,
  onDeleted,
}: DangerZoneDialogProps) {
  const { t } = useTranslation('settings')
  const { token, selectedOrganizationId } = useAuth()
  const { notify } = useToast()

  const [confirmText, setConfirmText] = useState('')
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const matches = confirmText === workspaceName

  function onOpen() {
    // El reinicio va aquí, en el manejador, y no en un efecto. Ver la nota de la cabecera.
    setConfirmText('')
    setError(null)
    onOpenChange(true)
  }

  function onCancel() {
    setConfirmText('')
    setError(null)
    onOpenChange(false)
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || !selectedOrganizationId || working || !matches) return

    setWorking(true)
    setError(null)
    try {
      await deactivateWorkspace(token, selectedOrganizationId)
      onOpenChange(false)
      onDeleted()
    } catch {
      setError(t('general.deleteFailed'))
      notify('error', t('general.deleteFailed'))
      setWorking(false)
    }
  }

  return (
    <div className="panel danger-zone">
      <div className="settings-section-heading">
        <h2>{t('general.dangerSection')}</h2>
        <p>{t('general.dangerDescription')}</p>
      </div>

      <ul className="danger-list">
        <li>{t('general.deleteWarningItems.revokeAccess')}</li>
        <li>{t('general.deleteWarningItems.cancelSubscription')}</li>
        <li>{t('general.deleteWarningItems.keepAudit')}</li>
        <li>{t('general.deleteWarningItems.reactivate')}</li>
      </ul>

      <div className="settings-form-row">
        <button className="danger-button" type="button" disabled={!canDelete || working} onClick={onOpen}>
          <span>{t('general.deleteWorkspace')}</span>
        </button>
        {!canDelete ? <p className="field-hint">{t('members.adminOnly')}</p> : null}
      </div>

      {open ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) onCancel()
          }}
        >
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="danger-dialog-title"
            aria-describedby="danger-dialog-body"
          >
            <div className="modal-header">
              <h2 id="danger-dialog-title">
                <AlertTriangle size={18} aria-hidden="true" />
                <span>{t('general.deleteConfirmTitle', { name: workspaceName })}</span>
              </h2>
            </div>

            <div className="modal-body" id="danger-dialog-body">
              <p className="danger-note">{t('general.deleteConfirmIntro')}</p>
              <ul className="danger-list">
                <li>{t('general.deleteWarningItems.revokeAccess')}</li>
                <li>{t('general.deleteWarningItems.cancelSubscription')}</li>
              </ul>
              <p className="danger-note">{t('general.deleteConfirmIrreversible')}</p>

              <form className="settings-form" onSubmit={onSubmit}>
                <div className="field">
                  <label htmlFor="danger-confirm">{t('general.deleteConfirmPhraseLabel')}</label>
                  <input
                    id="danger-confirm"
                    type="text"
                    value={confirmText}
                    placeholder={workspaceName}
                    autoComplete="off"
                    onChange={(event) => setConfirmText(event.target.value)}
                    aria-describedby="danger-confirm-hint"
                    aria-invalid={confirmText.length > 0 && !matches}
                  />
                  <p className="field-hint" id="danger-confirm-hint">
                    {t('general.deleteConfirmPhraseHint', { name: workspaceName })}
                  </p>
                  {confirmText.length > 0 && !matches ? (
                    <p className="field-error">{t('general.deleteConfirmPhraseMismatch')}</p>
                  ) : null}
                  {error !== null ? <p className="field-error">{error}</p> : null}
                </div>

                <div className="settings-form-actions">
                  <button className="danger-button" type="submit" disabled={!matches || working}>
                    <span>
                      {working ? t('general.deleteWorking') : t('general.deleteConfirmAction')}
                    </span>
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={onCancel}
                    disabled={working}
                  >
                    <span>{t('form.cancel')}</span>
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
