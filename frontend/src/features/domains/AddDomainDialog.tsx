/**
 * Diálogo de alta de dominio con las instrucciones de verificación.
 *
 * ## Por qué las instrucciones se muestran **después** del alta y no antes
 *
 * Porque el token lo genera el servidor. Pedir el nombre antes de tener el token obligaría
 * a generar el token en el cliente para poder mostrar la instrucción, y un token generado
 * en el cliente es un token que el cliente puede elegir: se acabaría comprobando que el
 * cliente se demuestra a sí mismo, que no es una prueba de nada.
 *
 * ## Por qué el botón de copiar dice qué se copia
 *
 * Porque hay dos cadenas distintas —el **nombre** del registro y su **valor**— y en la
 * práctica se copia la equivocada. Un botón único de "copiar" obligaría al usuario a saber
 * cuál de las dos va al campo "nombre" y cuál al "valor", que es exactamente lo que no sabe
 * quien está configuring DNS por primera vez.
 */

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { DomainVerificationMethod, VerifiedDomain } from '../../types/assets'

interface AddDomainDialogProps {
  open: boolean
  /** Dominio recién creado, o `null` mientras el alta está en curso o antes de empezar. */
  created: VerifiedDomain | null
  creating: boolean
  conflictMessage: string | null
  /** `true` si el conflicto es con un dominio que ya está en este workspace. */
  conflictIsOwn: boolean
  onSubmit: (name: string, method: DomainVerificationMethod) => void
  onClose: () => void
}

type Copiado = 'name' | 'value' | null

export function AddDomainDialog({
  open,
  created,
  creating,
  conflictMessage,
  conflictIsOwn,
  onSubmit,
  onClose,
}: AddDomainDialogProps) {
  const { t } = useTranslation('domains')
  const [name, setName] = useState('')
  const [method, setMethod] = useState<DomainVerificationMethod>('DNS_TXT')
  const [copied, setCopied] = useState<Copiado>(null)

  if (!open) {
    return null
  }

  /** Copia al portapapeles y marca qué se copió, para confirmar visualmente. */
  const copy = async (que: 'name' | 'value', texto: string) => {
    try {
      await navigator.clipboard.writeText(texto)
      setCopied(que)
    } catch {
      // El portapapeles puede estar bloqueado por permisos o por un contexto no seguro.
      // No es un error que merezca un aviso: el texto está en pantalla y se puede
      // seleccionar a mano, que es lo que hace alguien en esa situación.
      setCopied(null)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-domain-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="add-domain-title">{t('dialog.addTitle')}</h2>

        {created === null ? (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              onSubmit(name, method)
            }}
          >
            {conflictMessage !== null ? (
              <p className="assets-field-error" role="alert">
                {conflictMessage}
              </p>
            ) : null}

            <label className="assets-field">
              <span className="assets-field-label">{t('dialog.domainLabel')}</span>
              <input
                className="assets-field-input mono"
                type="text"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="empresa.com"
                autoComplete="off"
                spellCheck={false}
                required
                disabled={creating}
              />
              <span className="assets-field-hint">{t('dialog.domainHint')}</span>
            </label>

            <fieldset className="assets-field">
              <legend className="assets-field-label">{t('dialog.methodLabel')}</legend>
              <label className="assets-choice">
                <input
                  type="radio"
                  name="verification-method"
                  value="DNS_TXT"
                  checked={method === 'DNS_TXT'}
                  onChange={() => setMethod('DNS_TXT')}
                  disabled={creating}
                />
                <span>
                  <span className="assets-choice-title">{t('dialog.methodDns')}</span>
                  <span className="assets-field-hint">{t('dialog.methodDnsHint')}</span>
                </span>
              </label>
              <label className="assets-choice">
                <input
                  type="radio"
                  name="verification-method"
                  value="HTTP_FILE"
                  checked={method === 'HTTP_FILE'}
                  onChange={() => setMethod('HTTP_FILE')}
                  disabled={creating}
                />
                <span>
                  <span className="assets-choice-title">{t('dialog.methodHttp')}</span>
                  <span className="assets-field-hint">{t('dialog.methodHttpHint')}</span>
                </span>
              </label>
            </fieldset>

            <div className="assets-modal-actions">
              <button className="secondary-button" type="button" onClick={onClose}>
                <span>{t('actions.cancel')}</span>
              </button>
              <button className="primary-button" type="submit" disabled={creating || name === ''}>
                <span>{creating ? t('dialog.creating') : t('dialog.create')}</span>
              </button>
            </div>
          </form>
        ) : (
          <div>
            <p className="assets-modal-lead">{t('dialog.instructionsLead', { domain: created.domain_name })}</p>

            <div className="assets-dns-record">
              <div className="assets-dns-field">
                <span className="assets-field-label">{t('dialog.recordName')}</span>
                <div className="assets-dns-value-row">
                  <code className="mono">{created.txt_record_name}</code>
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => void copy('name', created.txt_record_name)}
                    aria-label={t('dialog.copyName')}
                  >
                    {copied === 'name' ? (
                      <Check size={16} aria-hidden="true" />
                    ) : (
                      <Copy size={16} aria-hidden="true" />
                    )}
                  </button>
                </div>
                <span className="assets-field-hint">{t('dialog.recordNameHint')}</span>
              </div>

              <div className="assets-dns-field">
                <span className="assets-field-label">{t('dialog.recordValue')}</span>
                <div className="assets-dns-value-row">
                  <code className="mono">{created.txt_record_value}</code>
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => void copy('value', created.txt_record_value)}
                    aria-label={t('dialog.copyValue')}
                  >
                    {copied === 'value' ? (
                      <Check size={16} aria-hidden="true" />
                    ) : (
                      <Copy size={16} aria-hidden="true" />
                    )}
                  </button>
                </div>
                <span className="assets-field-hint">{t('dialog.recordValueHint')}</span>
              </div>
            </div>

            <p className="assets-field-hint">{t('dialog.propagationHint', { record: created.txt_record_name })}</p>

            {conflictIsOwn ? (
              <p className="assets-field-error" role="alert">
                {conflictMessage}
              </p>
            ) : null}

            <div className="assets-modal-actions">
              <button className="primary-button" type="button" onClick={onClose}>
                <span>{t('dialog.done')}</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
