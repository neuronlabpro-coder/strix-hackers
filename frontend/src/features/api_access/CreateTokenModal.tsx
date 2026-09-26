import { useState } from 'react'
import { KeyRound, LoaderCircle, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ApiScopeCatalog, ApiTokenCreated } from '../../types/api'
import { DEFAULT_EXPIRY_DAYS, EXPIRY_CHOICES } from './useApiAccess'
import { ScopeSelector } from './ScopeSelector'

export interface CreateTokenModalProps {
  catalog: ApiScopeCatalog
  isOpen: boolean
  onClose: () => void
  onCreate: (input: { name: string; scopes: string[]; expiresInDays: number }) => Promise<ApiTokenCreated>
}

/**
 * Alta de un token de API.
 *
 * ## Por qué el secreto no se muestra en este modal
 *
 * Crear y usar son dos momentos distintos. Si el modal de alta mostrara el secreto y se
 * cerrara, el usuario tendría que pegar un texto de 73 caracteres en un diálogo de
 * creación y volver atrás a buscarlo. Por eso el alta termina y da paso a un modal
 * separado cuyo único trabajo es no perder el secreto.
 *
 * ## Por qué no se puede crear con la lista vacía
 *
 * El botón de guardar está deshabilitado sin permisos, y con una explicación al lado. Un
 * token sin permisos es una credencial que no puede hacer nada: crearla y descubrirlo en
 * el primer `GET` de producción es la forma más cara de aprenderlo.
 */
export function CreateTokenModal({
  catalog,
  isOpen,
  onClose,
  onCreate,
}: CreateTokenModalProps) {
  const { t } = useTranslation('apiAccess')
  const [name, setName] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [expiresInDays, setExpiresInDays] = useState<number>(DEFAULT_EXPIRY_DAYS)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [failed, setFailed] = useState(false)

  const trimmedName = name.trim()
  const canSubmit = trimmedName.length > 0 && selected.size > 0 && !isSubmitting

  const submit = async () => {
    if (!canSubmit) {
      return
    }
    setIsSubmitting(true)
    setFailed(false)
    try {
      await onCreate({
        name: trimmedName,
        scopes: [...selected].sort(),
        expiresInDays,
      })
    } catch {
      setFailed(true)
      setIsSubmitting(false)
    }
  }

  if (!isOpen) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-token-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h2 id="create-token-title">{t('create.title')}</h2>
            <p className="modal-section-caption">{t('create.description')}</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label={t('common:close')}
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="modal-section">
          <div className="form-field">
            <label htmlFor="token-name">{t('create.name')}</label>
            <input
              id="token-name"
              type="text"
              value={name}
              maxLength={100}
              placeholder={t('create.namePlaceholder')}
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className="form-field">
            <label htmlFor="token-expiry">{t('create.expiry')}</label>
            {/*
              `select` y no un grupo de botones: las opciones son tres y todas caben en
              una línea, y un desplegable evita gastar el ancho de un modal que ya lo
              comparten con un selector de 46 casillas.
            */}
            <select
              id="token-expiry"
              className="filter-field"
              value={expiresInDays}
              onChange={(event) => setExpiresInDays(Number(event.target.value))}
            >
              {EXPIRY_CHOICES.map((choice) => (
                <option key={choice.days} value={choice.days}>
                  {t(`expiry.${choice.key}`)}
                </option>
              ))}
            </select>
            <p className="form-hint">{t('create.expiryHint')}</p>
          </div>

          <div className="form-field">
            <span className="eyebrow">{t('create.scopes')}</span>
            <ScopeSelector
              catalog={catalog}
              selected={selected}
              onChange={setSelected}
              isDisabled={isSubmitting}
            />
          </div>

          {failed && (
            <p className="form-error" role="alert">
              {t('create.failed')}
            </p>
          )}
        </div>

        <footer className="modal-footer">
          <p className="modal-caption">{t('create.submitHint')}</p>
          <button className="secondary-button" type="button" onClick={onClose}>
            {t('common:cancel')}
          </button>
          <button
            className="primary-button"
            type="button"
            disabled={!canSubmit}
            onClick={() => void submit()}
          >
            {isSubmitting ? (
              <LoaderCircle size={16} className="spin" aria-hidden="true" />
            ) : (
              <KeyRound size={16} aria-hidden="true" />
            )}
            <span>{t('create.submit')}</span>
          </button>
        </footer>
      </div>
    </div>
  )
}

/** Modal de revelación del secreto, de un solo uso. */
export interface SecretRevealModalProps {
  token: ApiTokenCreated | null
  onClose: () => void
}

/**
 * Muestra el secreto una vez y obliga a reconocer que se ha guardado.
 *
 * El botón de cerrar dice "He guardado el token" en vez de "Cerrar". No es un detalle
 * de redacción: la diferencia real de este modal es si el usuario pulsa un botón que
 * dice lo que tiene que haber hecho, o uno que solo cierra una ventana. Con la
 * credencial ya perdida, el primer caso deja un rastro en su cabeza y el segundo no.
 *
 * El campo es de solo lectura y se puede seleccionar, no de solo lectura y oculto: un
 * token que hay que revelar con el ojo para copiarlo character a character es un token
 * que se copia mal.
 */
export function SecretRevealModal({ token, onClose }: SecretRevealModalProps) {
  const { t } = useTranslation('apiAccess')
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    if (!token) {
      return
    }
    try {
      await navigator.clipboard.writeText(token.raw_token)
      setCopied(true)
    } catch {
      // El portapapeles puede estar bloqueado por permisos o por contexto no seguro.
      // No se muestra un error: el campo es seleccionable y el usuario puede copiar a
      // mano, y un aviso de "no se pudo copiar" invites a repetir hasta que funcione.
      setCopied(false)
    }
  }

  if (!token) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="reveal-token-title"
        aria-describedby="reveal-token-warning"
      >
        <header className="modal-header">
          <div>
            <h2 id="reveal-token-title">{t('reveal.title')}</h2>
            <p className="modal-section-caption">{t('reveal.description', { name: token.name })}</p>
          </div>
        </header>

        <div className="modal-section">
          <p className="reveal-warning" id="reveal-token-warning" role="alert">
            {t('reveal.warning')}
          </p>

          <div className="form-field">
            <label htmlFor="reveal-token-value">{t('reveal.fieldLabel')}</label>
            <div className="reveal-row">
              <input
                id="reveal-token-value"
                className="text-input mono"
                type="text"
                readOnly
                value={token.raw_token}
                onFocus={(event) => event.currentTarget.select()}
              />
              <button
                className="secondary-button"
                type="button"
                onClick={() => void copy()}
              >
                {t(copied ? 'reveal.copied' : 'reveal.copy')}
              </button>
            </div>
          </div>

          <dl className="reveal-meta">
            <div>
              <dt>{t('reveal.metaPrefix')}</dt>
              <dd className="mono">{token.token_prefix}</dd>
            </div>
            <div>
              <dt>{t('reveal.metaScopes')}</dt>
              <dd>{token.scopes.length}</dd>
            </div>
            <div>
              <dt>{t('reveal.metaExpiry')}</dt>
              <dd>
                {token.expires_at
                  ? new Date(token.expires_at).toLocaleString()
                  : t('reveal.neverExpires')}
              </dd>
            </div>
          </dl>
        </div>

        <footer className="modal-footer">
          <button className="primary-button" type="button" onClick={onClose}>
            {t('reveal.acknowledge')}
          </button>
        </footer>
      </div>
    </div>
  )
}
