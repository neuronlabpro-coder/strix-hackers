import { useCallback, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { renameWorkspace } from '../../lib/workspaceApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { DangerZoneDialog } from './DangerZoneDialog'

/**
 * Longitud mínima del nombre ya normalizado.
 *
 * El mismo número que comprueba el backend, y está aquí por una razón concreta: el
 * `min_length=1` de Pydantic mira la cadena **antes** de colapsar espacios, así que tres
 * espacios lo pasarían y llegarían a la base como un workspace sin nombre visible. El
 * validador del servidor lo comprueba después de normalizar; aquí se avisa antes de que la
 * persona escriba, no después de que pulse guardar.
 *
 * No es una regla duplicada que pueda divergir en la dirección peligrosa: si el backend
 * **subiera** el mínimo, esta constante se quedaría corta y seguiría funcionando. Lo que no
 * puede pasar es que el backend lo baje y el panel siga rechazando, y por eso el mensaje de
 * error del servidor **no** lleva el número incrustado: lo trae el servidor.
 */
const MIN_NAME_LENGTH = 3

/**
 * Vista *General* de Ajustes.
 *
 * ## Por qué el nombre se guarda con un botón y no al perder el foco
 *
 * Guardar en cada pulsación mandaría una petición por carácter. Guardar al perder el foco
 * manda una por cada visita a otro campo, que es casi lo mismo y además comparte camino con
 * un clic accidental. Un botón explícito hace una petición por decisión, y el error de
 * validación vuelve al campo donde se cometió.
 */
export function SettingsGeneralPage() {
  const { t } = useTranslation('settings')
  const { token, user, organizations, selectedOrganizationId } = useAuth()
  const { notify } = useToast()
  const navigate = useNavigate()

  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? null
  const esAdmin = organization?.role === 'admin' || user?.is_superuser === true

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dangerOpen, setDangerOpen] = useState(false)

  /**
   * El borrador vive aquí, **por workspace**.
   *
   * Antes era un `useEffect` que reiniciaba el campo al cambiar `organizationId`, y eso es
   * estado síncrono dentro de un efecto: dos renders por cada cambio de contexto. Se ha
   * sustituido por un `Map` que guarda un borrador por identificador, con el del workspace
   * activo **derivado** durante el render.
   *
   * La diferencia no es de estilo. Con el `useEffect`, cambiar de workspace borraba lo
   * tecleado en el anterior sin avisar: un nombre de veinte minutos desaparece al tocar el
   * selector por encima. Con el `Map`, cada workspace conserva su borrador y al volver a él
   * el texto sigue ahí, que es lo que espera quien trabaja con dos clientes abiertos.
   *
   * El `Map` se rellena al escribir, que es un evento de usuario, no un efecto: por eso no
   * hay ninguna escritura durante el render.
   */
  const [borradores, setBorradores] = useState<Record<string, string>>({})

  const organizationId = selectedOrganizationId
  const nombreGuardado = organization?.name ?? ''
  const name = organizationId === null ? nombreGuardado : (borradores[organizationId] ?? nombreGuardado)

  const normalized = name.trim().replace(/\s+/g, ' ')
  const dirty = normalized !== '' && normalized !== nombreGuardado
  const demasiadoCorto = normalized.length > 0 && normalized.length < MIN_NAME_LENGTH

  function onNameChange(value: string) {
    if (organizationId === null) return
    setBorradores((current) => ({ ...current, [organizationId]: value }))
    setError(null)
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || !organizationId || saving) return
    if (normalized.length < MIN_NAME_LENGTH) {
      setError(t('general.nameTooShort'))
      return
    }

    setSaving(true)
    setError(null)
    try {
      await renameWorkspace(token, organizationId, normalized)
      // El borrador se borra **después** del guardado, no antes. Si el `PATCH` falla, el
      // texto tiene que seguir en el campo: es lo que el usuario va a corregir.
      setBorradores((current) => {
        const siguiente = { ...current }
        delete siguiente[organizationId]
        return siguiente
      })
      notify('success', t('general.renameSuccess', { name: normalized }))
    } catch {
      // El `422` del nombre trae su propio mensaje del servidor, que es el bueno, pero
      // `ApiError` solo transporta el código: no hay forma de leer el `detail` sin duplicar
      // el cliente de red. Con un `catch` único y un mensaje propio se evita tener dos
      // ramas idénticas que difieren solo en un texto que no se está leyendo.
      setError(t('general.renameError'))
      notify('error', t('general.renameError'))
    } finally {
      setSaving(false)
    }
  }

  const onDeleted = useCallback(() => {
    notify('success', t('general.deleteSuccess', { name: organization?.name ?? '' }))
    navigate('/dashboard', { replace: true })
  }, [notify, navigate, organization?.name, t])

  if (!organization) {
    return (
      <section className="settings-section">
        <div className="empty-card">
          <h2>{t('notFound.title')}</h2>
          <p>{t('notFound.description')}</p>
        </div>
      </section>
    )
  }

  return (
    <section className="settings-section">
      <header className="settings-section-heading">
        <h1>{t('general.title')}</h1>
        <p>{t('general.subtitle')}</p>
      </header>

      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('general.workspaceSection')}</h2>
        </div>
        <form className="settings-form" onSubmit={onSubmit} noValidate>
          <div className="field">
            <label htmlFor="workspace-name">{t('general.workspaceNameLabel')}</label>
            <input
              id="workspace-name"
              name="name"
              type="text"
              value={name}
              maxLength={128}
              placeholder={t('general.workspaceNamePlaceholder')}
              onChange={(event) => onNameChange(event.target.value)}
              disabled={!esAdmin || saving}
              aria-describedby="workspace-name-hint"
              aria-invalid={error !== null || demasiadoCorto}
            />
            <p className="field-hint" id="workspace-name-hint">
              {t('general.workspaceNameHint')}
            </p>
            {error !== null ? <p className="field-error">{error}</p> : null}
            {error === null && demasiadoCorto ? (
              <p className="field-error">{t('general.nameTooShort')}</p>
            ) : null}
          </div>

          <div className="settings-form-row">
            <button
              className="primary-button"
              type="submit"
              disabled={!esAdmin || saving || !dirty || demasiadoCorto}
            >
              <span>{saving ? t('general.saving') : t('general.saveChanges')}</span>
            </button>
            {!esAdmin ? <p className="field-hint">{t('members.adminOnly')}</p> : null}
          </div>
        </form>
      </div>

      {/*
        El `slug` se enseña solo, como campo de lectura, y con su motivo al lado.

        Es información —«tu workspace vive en esta dirección»— y no un control. Enseñar algo
        que no se puede editar con un candado al lado sería un control deshabilitado, y esta
        vez sí hay algo que hacer con él: copiarlo. Un campo de solo lectura se puede
        seleccionar; un `<span>` no.
      */}
      <div className="panel">
        <div className="field">
          <label htmlFor="workspace-slug">{t('general.slugReadonlyLabel')}</label>
          <input
            id="workspace-slug"
            type="text"
            value={organization.slug}
            readOnly
            aria-describedby="workspace-slug-hint"
          />
          <p className="field-hint" id="workspace-slug-hint">
            {t('general.slugReadonlyHint')}
          </p>
        </div>
      </div>

      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('general.securitySection')}</h2>
        </div>
        <div className="locked-row">
          <div className="locked-row-copy">
            <strong>{t('general.twoFactorTitle')}</strong>
            <span>{t('general.twoFactorDescription')}</span>
            <span>{t('general.twoFactorDisabledReason')}</span>
          </div>
          {/*
            Botón deshabilitado con badge, no una nota y nada más.

            La decisión que importa: un control que existe pero no hace nada es peor que su
            ausencia, porque el usuario deduce que hay un problema. Con el botón visible y el
            badge al lado, el mensaje es «existe, todavía no», que es un hecho; con el botón
            en su sitio y la nota debajo, el mensaje es «está roto».

            El badge dice `Próximamente` y no `Enterprise` a propósito: la 2FA no es una
            función de pago, es una función que aún no está construida. Ponerle un precio
            sería prometer algo que no existe.
          */}
          <div className="settings-form-row">
            <span className="coming-soon-badge">{t('locked.comingSoon')}</span>
            <button className="secondary-button" type="button" disabled>
              <span>{t('general.twoFactorTitle')}</span>
            </button>
          </div>
        </div>
      </div>

      <DangerZoneDialog
        open={dangerOpen}
        workspaceName={organization.name}
        canDelete={esAdmin}
        onOpenChange={setDangerOpen}
        onDeleted={onDeleted}
      />
    </section>
  )
}
