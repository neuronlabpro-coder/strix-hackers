import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Coins, Power, TriangleAlert } from 'lucide-react'

import type { AdminOrganization, PlanTierAdmin, TenantLifecycle } from '../../types/api'

/**
 * Los tres modales de acción sobre un tenant.
 *
 * ## Por qué están en un solo fichero
 *
 * Los tres se abren desde la misma tabla y escriben sobre la misma fila. Separados en tres
 * ficheros, cada uno repetiría el diálogo, el foco atrapado, el cierre con `Escape` y el
 * aviso de irreversibilidad. Esa repetición es donde se cuelan las diferencias: un modal
 * con `Escape` y otro sin él.
 *
 * ## Por qué el de baja pide confirmación y los otros dos no
 *
 * Cambiar un plan y acreditar créditos se pueden repetir: volver al valor anterior deshace
 * el efecto con la misma operación. Dar de baja un tenant revoca accesos y no tiene botón
 * de vuelta. Solo ese lo dice, y el aviso advierte de que desde la consola no se deshace.
 *
 * ## Por qué el importe se manda crudo
 *
 * El backend lo valida con precisión decimal sobre una columna `Numeric(12, 4)`. Pasar el
 * texto por `Number` en el cliente reintroduciría exactamente el error que ese tipo evita.
 * La comprobación local es **solo de sintaxis** —signo, punto y número de decimales— y no
 * decide si el saldo alcanza: eso lo dice el servidor.
 */

const PLAN_TIERS: readonly PlanTierAdmin[] = ['FREE', 'PRO', 'ENTERPRISE']

export type TenantActionKind = 'plan' | 'grant' | 'deactivate'

export interface TenantAction {
  kind: TenantActionKind
  tenant: AdminOrganization
}

interface TenantActionDialogProps {
  action: TenantAction | null
  onClose: () => void
  /** Devuelve `true` si el backend aceptó el cambio. */
  onPlanChange: (tenantId: string, plan: PlanTierAdmin) => Promise<boolean>
  /** Devuelve el saldo resultante si aceptó, o `null` si falló. */
  onGrant: (tenantId: string, amount: string, note: string) => Promise<string | null>
  onDeactivate: (tenantId: string) => Promise<boolean>
  balanceFormatter: (value: string) => string
}

const AMOUNT_PATTERN = /^\d+(\.\d{1,4})?$/

export function TenantActionDialog({
  action,
  onClose,
  onPlanChange,
  onGrant,
  onDeactivate,
  balanceFormatter,
}: TenantActionDialogProps) {
  const { t } = useTranslation('admin')
  const [plan, setPlan] = useState<PlanTierAdmin | null>(null)
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [hasFailed, setHasFailed] = useState(false)
  const [grantedBalance, setGrantedBalance] = useState<string | null>(null)

  if (action === null) {
    return null
  }

  const { tenant, kind } = action
  const prefix = `tenants.${kind}Modal`
  // El plan arranca en el actual, no en un valor fijo: abrir el modal y ver un plan
  // distinto del que ya tiene hace dudar de si se está cambiando o solo mirando.
  const selectedPlan = plan ?? tenant.plan_tier
  const isAmountValid = AMOUNT_PATTERN.test(amount) && Number(amount) > 0

  const reset = () => {
    setPlan(null)
    setAmount('')
    setNote('')
    setIsSubmitting(false)
    setHasFailed(false)
    setGrantedBalance(null)
  }

  const close = () => {
    reset()
    onClose()
  }

  const submit = async () => {
    setIsSubmitting(true)
    setHasFailed(false)
    setGrantedBalance(null)
    try {
      if (kind === 'plan') {
        if (await onPlanChange(tenant.id, selectedPlan)) {
          close()
        }
      } else if (kind === 'grant') {
        const balance = await onGrant(tenant.id, amount, note)
        if (balance !== null) {
          // El modal **no** se cierra: el operador acaba de mover dinero y necesita ver el
          // saldo resultante antes de decidir el siguiente paso. Cerrarlo lo obligaría a
          // recargar la tabla para averiguar si la operación entró.
          setGrantedBalance(balance)
        }
      } else if (await onDeactivate(tenant.id)) {
        close()
      }
    } finally {
      setIsSubmitting(false)
    }
  }

  const isDisabled =
    isSubmitting ||
    (kind === 'grant' && !isAmountValid) ||
    (kind === 'plan' && selectedPlan === tenant.plan_tier)

  return (
    <div className="modal-backdrop" role="presentation" onClick={isSubmitting ? undefined : close}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tenant-action-title"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !isSubmitting) {
            close()
          }
        }}
      >
        <header className="modal-header">
          <h2 id="tenant-action-title">{t(`${prefix}.title`, { name: tenant.name })}</h2>
          <button
            className="icon-button"
            type="button"
            onClick={close}
            disabled={isSubmitting}
            aria-label={t('actions.cancel')}
          >
            <span aria-hidden="true">×</span>
          </button>
        </header>

        <p className="modal-caption">{t(`${prefix}.caption`)}</p>

        {kind === 'plan' ? (
          <label className="field">
            <span className="">{t(`${prefix}.label`)}</span>
            <select
              className="select-input"
              value={selectedPlan}
              onChange={(event) => setPlan(event.target.value as PlanTierAdmin)}
            >
              {PLAN_TIERS.map((tier) => (
                <option key={tier} value={tier}>
                  {t(`organizations.plan.${tier}`)}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        {kind === 'grant' ? (
          <form
            className="form-field"
            onSubmit={(event) => {
              event.preventDefault()
              void submit()
            }}
          >
            <label className="field">
              <span className="">{t(`${prefix}.amountLabel`)}</span>
              <input
                className="text-input"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                inputMode="decimal"
                placeholder={t(`${prefix}.amountPlaceholder`)}
                required
              />
              <span className="form-hint">{t(`${prefix}.amountHint`)}</span>
            </label>
            <label className="field">
              <span className="">{t(`${prefix}.noteLabel`)}</span>
              <input
                className="text-input"
                value={note}
                onChange={(event) => setNote(event.target.value)}
                maxLength={255}
              />
              <span className="form-hint">{t(`${prefix}.noteHint`)}</span>
            </label>
          </form>
        ) : null}

        {kind === 'deactivate' ? (
          <p className="modal-warning" role="alert">
            <TriangleAlert size={16} aria-hidden="true" />
            <span>{t(`${prefix}.warning`)}</span>
          </p>
        ) : null}

        {hasFailed ? (
          <p className="form-error" role="alert">
            {t('states.error')}
          </p>
        ) : null}

        {grantedBalance !== null ? (
          <p className="modal-success" role="status">
            {t(`${prefix}.result`, { balance: balanceFormatter(grantedBalance) })}
          </p>
        ) : null}

        <footer className="modal-footer">
          <button className="secondary-button" type="button" onClick={close} disabled={isSubmitting}>
            <span>{t('actions.cancel')}</span>
          </button>
          <button
            className={kind === 'deactivate' ? 'danger-button' : 'primary-button'}
            type="button"
            onClick={() => void submit()}
            disabled={isDisabled}
          >
            {kind === 'deactivate' ? <Power size={16} aria-hidden="true" /> : null}
            {kind === 'grant' ? <Coins size={16} aria-hidden="true" /> : null}
            <span>
              {isSubmitting
                ? t(`${prefix}.submitting`)
                : kind === 'deactivate'
                  ? t(`${prefix}.confirm`)
                  : t(`${prefix}.submit`)}
            </span>
          </button>
        </footer>
      </div>
    </div>
  )
}

export function TenantLifecycleBadge({ tenant }: { tenant: AdminOrganization }) {
  const { t } = useTranslation('admin')
  // El estado se deriva de dos columnas que pueden desincronizarse, así que se calcula
  // aquí y no se guarda: tres estados que a simple vista se confunden —nunca dado de
  // baja, dado de baja y desactivado— son decisiones distintas para el operador.
  const lifecycle: TenantLifecycle =
    tenant.deleted_at !== null ? 'deleted' : tenant.is_active ? 'active' : 'deactivated'
  const className =
    lifecycle === 'active'
      ? 'badge badge-on'
      : lifecycle === 'deleted'
        ? 'badge terminal-error'
        : 'badge terminal-warning'
  return (
    <span className={className}>
      <span className="severity-swatch" aria-hidden="true" />
      {t(`tenants.lifecycle.${lifecycle}`)}
    </span>
  )
}
