import { useTranslation } from 'react-i18next'

import type { WorkspaceMember } from '../../types/workspace'

/**
 * Confirmación de retirada de un miembro.
 *
 * ## Por qué es un diálogo y no un `window.confirm`
 *
 * Porque el texto necesita decir **qué pasa con su cuenta**, no solo "¿está seguro?". La
 * fila de `memberships` no se borra —se desactiva— porque su identificador es el actor de
 * otras entradas de la auditoría, y R4 no permite que ese rastro desaparezca. Eso es
 * información que el usuario necesita antes de decidir, y no cabe en el mensaje de dos
 * botones de un `confirm`.
 *
 * ## Por qué no hay campo de confirmación aquí
 *
 * Al revés que en la baja del workspace. Ahí lo que se borra es el workspace entero de
 * otra gente y no tiene vuelta atrás. Aquí es una fila reversible —reconvidarlo la
 * devuelve— y quien decide es un admin que ya está autenticado y ya tiene el poder de
 * hacerlo. Poner una barrera de tecleo para una operación reversible sería teatro de
 * seguridad: la barrera de verdad es el rol.
 */
interface RemoveMemberDialogProps {
  member: WorkspaceMember | null
  working: boolean
  onCancel: () => void
  onConfirm: () => void
}

export function RemoveMemberDialog({
  member,
  working,
  onCancel,
  onConfirm,
}: RemoveMemberDialogProps) {
  const { t } = useTranslation('settings')

  if (member === null) {
    return null
  }

  return (
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
        aria-labelledby="remove-member-title"
        aria-describedby="remove-member-body"
      >
        <div className="modal-header">
          <h2 id="remove-member-title">
            <span>{t('members.removeConfirmTitle', { name: member.full_name })}</span>
          </h2>
        </div>
        <div className="modal-body" id="remove-member-body">
          <p className="danger-note">{t('members.removeConfirmBody')}</p>
          <div className="settings-form-actions">
            <button
              className="danger-button"
              type="button"
              onClick={onConfirm}
              disabled={working}
            >
              <span>{t('members.removeConfirmAction')}</span>
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
        </div>
      </div>
    </div>
  )
}
