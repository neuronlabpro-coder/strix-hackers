/**
 * Propuesta de remediación generada por la plataforma.
 *
 * ## Por qué hay un diálogo de confirmación y no un botón directo
 *
 * Porque la acción **cuesta créditos** y crea una rama en el repositorio del cliente. Un
 * botón que se pulsa sin querer tiene dos consecuencias irreversibles: saldo gastado y una
 * rama publicada que alguien tiene que limpiar. El diálogo no pide permiso para pensar, solo
 * para gastar.
 *
 * ## Por qué se muestra el parche generado cuando vuelve, y no solo la URL
 *
 * Porque la propuesta es un borrador y el usuario tiene que poder juzgarla antes de abrirla.
 * Un enlace a GitHub exige cambiar de aplicación; el diff está en la misma pantalla donde
 * está el PoC, que es donde se contrasta la evidencia con la corrección. Y por eso se
 * distingue del diff del motor: el primero es evidencia inmutable, el segundo es una propuesta
 * que se puede regenerar.
 */

import { useState } from 'react'
import { GitPullRequest, LoaderCircle, ShieldCheck, Sparkles } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { IssueStatus } from '../../types/api'
import { DiffViewer } from './DiffViewer'

interface RemediationBlockProps {
  status: IssueStatus
  remediationPrUrl: string | null
  patchGenerado: string | null
  /** El diff del motor. Es evidencia y no se sustituye por el borrador. */
  parcheDelMotor: string | null
  busy: boolean
  failed: boolean
  onGenerate: () => void
}

export function RemediationBlock({
  status,
  remediationPrUrl,
  patchGenerado,
  parcheDelMotor,
  busy,
  failed,
  onGenerate,
}: RemediationBlockProps) {
  const { t } = useTranslation('issues')
  const [confirmando, setConfirmando] = useState(false)

  const yaPropuesta = remediationPrUrl !== null
  const estaPropuestaAhora = status === 'REMEDIATION_PROPOSED'
  // El botón se esconde cuando ya hay una propuesta **y** el estado lo refleja. Con una de
  // las dos, se ofrece "regenerar": lo contrario sería un panel que muestra una PR y a la vez
  // ofrece crear otra, sin decir que la anterior sigue ahí.
  const SeOculta = yaPropuesta && estaPropuestaAhora

  return (
    <section className="content-card" aria-labelledby="remediation-title">
      <p className="eyebrow">{t('detail.remediation.eyebrow')}</p>
      <h2 id="remediation-title">{t('detail.remediation.title')}</h2>
      <p className="chart-empty">{t('detail.remediation.description')}</p>

      {failed ? (
        <p className="inline-notice inline-notice-warning" role="alert">
          {t('detail.remediation.error')}
        </p>
      ) : null}

      {estaPropuestaAhora && yaPropuesta ? (
        <div className="remediation-banner" role="status">
          <ShieldCheck size={18} aria-hidden="true" />
          <div>
            <p className="remediation-banner-title">{t('detail.remediation.proposed')}</p>
            <p className="remediation-banner-note">{t('detail.remediation.proposedNote')}</p>
            <a
              className="secondary-button"
              href={remediationPrUrl}
              target="_blank"
              rel="noreferrer"
            >
              <GitPullRequest size={16} aria-hidden="true" />
              <span>{t('detail.remediation.openPr')}</span>
            </a>
          </div>
        </div>
      ) : null}

      {SeOculta ? null : (
        <>
          <div className="page-actions">
            <button
              className="primary-button"
              type="button"
              disabled={busy}
              onClick={() => setConfirmando(true)}
            >
              <Sparkles size={16} aria-hidden="true" />
              <span>
                {busy ? t('detail.remediation.working') : t('detail.remediation.button')}
              </span>
            </button>
            {busy ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : null}
          </div>
          {yaPropuesta ? (
            <p className="form-hint">{t('detail.remediation.willRegenerate')}</p>
          ) : null}
        </>
      )}

      {parcheDelMotor ? (
        <div className="remediation-diff">
          <p className="eyebrow">{t('detail.remediation.engineDiff')}</p>
          <DiffViewer diff={parcheDelMotor} />
        </div>
      ) : null}

      {patchGenerado && !estaPropuestaAhora ? (
        <div className="remediation-diff">
          <p className="eyebrow">{t('detail.remediation.draftTitle')}</p>
          <DiffViewer diff={patchGenerado} />
        </div>
      ) : null}

      {confirmando ? (
        <RemediationDialog
          yaHabiaPropuesta={yaPropuesta}
          busy={busy}
          onCancel={() => setConfirmando(false)}
          onConfirm={() => {
            setConfirmando(false)
            onGenerate()
          }}
        />
      ) : null}
    </section>
  )
}

interface RemediationDialogProps {
  yaHabiaPropuesta: boolean
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}

/**
 * La confirmación previa.
 *
 * Dice **las dos** cosas que la acción cambia: el saldo y el repositorio. Decir solo "se
 * generará un parche" sería una confirmación vacía, porque el usuario que va a decir sí lo
 * que quiere saber es cuánto le cuesta y dónde va a aparecer la rama.
 */
function RemediationDialog({
  yaHabiaPropuesta,
  busy,
  onCancel,
  onConfirm,
}: RemediationDialogProps) {
  const { t } = useTranslation('issues')
  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="remediation-dialog-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="remediation-dialog-title">{t('detail.remediation.dialog.title')}</h2>
        <ul className="remediation-dialog-list">
          <li>{t('detail.remediation.dialog.cost')}</li>
          <li>{t('detail.remediation.dialog.branch')}</li>
          <li>{t('detail.remediation.dialog.target')}</li>
        </ul>
        {yaHabiaPropuesta ? (
          <p className="form-error" role="alert">
            {t('detail.remediation.dialog.replaceWarning')}
          </p>
        ) : null}
        <div className="modal-actions">
          <button className="secondary-button" type="button" onClick={onCancel}>
            <span>{t('detail.remediation.dialog.cancel')}</span>
          </button>
          <button
            className="primary-button"
            type="button"
            onClick={onConfirm}
            disabled={busy}
          >
            <Sparkles size={16} aria-hidden="true" />
            <span>{t('detail.remediation.dialog.confirm')}</span>
          </button>
        </div>
      </div>
    </div>
  )
}
