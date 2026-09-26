import { useCallback, useState } from 'react'
import { Coins, Power, RefreshCw, Tags } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  deactivateAdminOrganization,
  getAdminOrganizations,
  grantAdminCredits,
  updateAdminOrganizationPlan,
} from '../../lib/adminApi'
import {activeLocale, formatCredits} from '../../lib/format'
import type { AdminOrganization, PlanTierAdmin, TenantLifecycle } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { TenantActionDialog, TenantLifecycleBadge, type TenantAction } from './TenantActionDialog'
import { PaginationBar } from './PaginationBar'
import { useAdminPage } from './useAdminPage'

const PAGE_SIZE = 25

/**
 * Inventario global de tenants.
 *
 * ## Por qué los filtros viven en la URL
 *
 * No viven: viven en el estado de la página. La tentación era reflejarlos en la query
 * string para que el operador pudiera compartir un enlace, y es una tentación razonable
 * hasta que se pide "abre los tenants dados de baja del plan Enterprise", que son dos
 * filtros y un dato que llega por enganche manual. El enlace compartible, si algún día
 * hace falta, se añade como una opción explícita y no como efecto secundario de pintar
 * una tabla.
 *
 * ## Por qué cada cambio refresca la tabla en vez de parchear la fila
 *
 * Un cambio de plan, una inyección de créditos y una baja modifican contadores que otras
 * vistas también enseñan —el saldo, el número de miembros, el total de la página—. Parchar
 * solo la fila que cambió deja el resto desincronizado hasta el siguiente refresco, y en
 * una pantalla donde el operador va a tocar varios tenants seguidos, el estado que ve es
 * una mezcla de antes y después. Recargar cuesta una petición y devuelve la verdad.
 */
export function AdminTenantsPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()
  const { notify } = useToast()

  const [planFilter, setPlanFilter] = useState<PlanTierAdmin | ''>('')
  const [lifecycleFilter, setLifecycleFilter] = useState<TenantLifecycle | ''>('')
  const [search, setSearch] = useState('')
  const [action, setAction] = useState<TenantAction | null>(null)
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set())

  const filterKey = `${token}:${planFilter}:${lifecycleFilter}:${search}`

  const load = useCallback(
    (limit: number, offset: number) =>
      // `token` es `null` mientras carga la sesión. El hook recibe `disabled` y no pide
      // nada en ese estado, así que esta guarda solo evita pasar un token inexistente:
      // devolver una página vacía es más honesto que lanzar una petición sin credenciales.
      token === null
        ? Promise.resolve({ items: [], total: 0 })
        : getAdminOrganizations(token, {
            plan: planFilter === '' ? null : planFilter,
            lifecycle: lifecycleFilter === '' ? null : lifecycleFilter,
            search: search === '' ? null : search,
            limit,
            offset,
          }),
    [lifecycleFilter, planFilter, search, token],
  )

  const page = useAdminPage<AdminOrganization>(load, {
    pageSize: PAGE_SIZE,
    filterKey,
    disabled: !token || user?.is_superuser !== true,
  })

  const markPending = (tenantId: string, pending: boolean) => {
    setPendingIds((current) => {
      const next = new Set(current)
      if (pending) {
        next.add(tenantId)
      } else {
        next.delete(tenantId)
      }
      return next
    })
  }

  const onPlanChange = useCallback(
    async (tenantId: string, plan: PlanTierAdmin): Promise<boolean> => {
      if (token === null) {
        return false
      }
      markPending(tenantId, true)
      try {
        await updateAdminOrganizationPlan(token, tenantId, plan)
        notify('success', t('tenants.messages.planUpdated'))
        page.refresh()
        return true
      } catch {
        notify('error', t('states.error'))
        return false
      } finally {
        markPending(tenantId, false)
      }
    },
    [notify, page, t, token],
  )

  const onGrant = useCallback(
    async (tenantId: string, amount: string, note: string): Promise<string | null> => {
      if (token === null) {
        return null
      }
      markPending(tenantId, true)
      try {
        const result = await grantAdminCredits(token, tenantId, amount, note)
        notify('success', t('tenants.messages.creditsGranted'))
        page.refresh()
        return result.balance_after
      } catch {
        notify('error', t('states.error'))
        return null
      } finally {
        markPending(tenantId, false)
      }
    },
    [notify, page, t, token],
  )

  const onDeactivate = useCallback(
    async (tenantId: string): Promise<boolean> => {
      if (token === null) {
        return false
      }
      markPending(tenantId, true)
      try {
        await deactivateAdminOrganization(token, tenantId)
        notify('success', t('tenants.messages.deactivated'))
        page.refresh()
        return true
      } catch {
        notify('error', t('states.error'))
        return false
      } finally {
        markPending(tenantId, false)
      }
    },
    [notify, page, t, token],
  )

  return (
    <section className="page-section" aria-labelledby="admin-tenants-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="admin-tenants-title">{t('tenants.title')}</h1>
          <p className="page-description">{t('tenants.description')}</p>
        </div>
        <div className="page-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={page.refresh}
            disabled={page.isLoading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('overview.refresh')}</span>
          </button>
        </div>
      </div>

      <div className="filter-bar">
        <label className="field">
          <span className="">{t('tenants.filters.search')}</span>
          <input
            className="text-input"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('tenants.filters.searchPlaceholder')}
          />
        </label>
        <label className="field">
          <span className="">{t('tenants.filters.plan')}</span>
          <select
            className="select-input"
            value={planFilter}
            onChange={(event) => setPlanFilter(event.target.value as PlanTierAdmin | '')}
          >
            <option value="">{t('tenants.filters.allPlans')}</option>
            {(['FREE', 'PRO', 'ENTERPRISE'] as const).map((tier) => (
              <option key={tier} value={tier}>
                {t(`organizations.plan.${tier}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="">{t('tenants.filters.lifecycle')}</span>
          <select
            className="select-input"
            value={lifecycleFilter}
            onChange={(event) => setLifecycleFilter(event.target.value as TenantLifecycle | '')}
          >
            <option value="">{t('tenants.filters.allLifecycles')}</option>
            {(['active', 'deleted', 'deactivated'] as const).map((estado) => (
              <option key={estado} value={estado}>
                {t(`tenants.lifecycle.${estado}`)}
              </option>
            ))}
          </select>
        </label>
        {planFilter !== '' || lifecycleFilter !== '' || search !== '' ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() => {
              setPlanFilter('')
              setLifecycleFilter('')
              setSearch('')
            }}
          >
            <span>{t('tenants.filters.clear')}</span>
          </button>
        ) : null}
      </div>

      {page.loadFailed && page.items.length === 0 ? (
        <div className="empty-card">
          <h2>{t('states.error')}</h2>
          <button className="secondary-button" type="button" onClick={page.refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : page.items.length === 0 ? (
        <div className="empty-card">
          <p>{t('tenants.empty')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="visually-hidden">{t('tenants.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('tenants.columns.name')}</th>
                <th scope="col">{t('tenants.columns.plan')}</th>
                <th scope="col">{t('tenants.columns.credits')}</th>
                <th scope="col">{t('tenants.columns.members')}</th>
                <th scope="col">{t('tenants.columns.status')}</th>
                <th scope="col">{t('tenants.columns.created')}</th>
                <th scope="col">{t('tenants.columns.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((tenant) => {
                const isPending = pendingIds.has(tenant.id)
                return (
                  <tr key={tenant.id}>
                    <th scope="row" className="cell-primary">
                      {tenant.name}
                      <span className="mono table-secondary">{tenant.slug}</span>
                    </th>
                    <td>
                      <span className="badge">{t(`organizations.plan.${tenant.plan_tier}`)}</span>
                    </td>
                    <td>
                      <span className="mono">
                        {formatCredits(tenant.credit_balance)}
                      </span>
                    </td>
                    <td>
                      <span className="mono">
                        {t('tenants.members', { count: tenant.member_count })}
                      </span>
                    </td>
                    <td>
                      <TenantLifecycleBadge tenant={tenant} />
                    </td>
                    <td>
                      <span className="mono timestamp">
                        {new Intl.DateTimeFormat(activeLocale(), { dateStyle: 'medium' }).format(
                          new Date(tenant.created_at),
                        )}
                      </span>
                    </td>
                    <td>
                      {/*
                        Dar de baja se ofrece siempre, incluso a un tenant ya dado de baja.
                        Ocultarlo parece más limpio y deja al operador creyendo que la
                        baja se puede deshacer desde aquí, que no es el caso: la segunda
                        llamada devuelve `409`. Se muestra desactivado y el título explica
                        por qué.
                      */}
                      <div className="webhook-actions">
                        <button
                          className="link-button"
                          type="button"
                          disabled={isPending}
                          onClick={() => setAction({ kind: 'plan', tenant })}
                        >
                          <Tags size={15} aria-hidden="true" />
                          <span>{t('tenants.actions.plan')}</span>
                        </button>
                        <button
                          className="link-button"
                          type="button"
                          disabled={isPending || tenant.deleted_at !== null}
                          title={
                            tenant.deleted_at !== null
                              ? t('tenants.lifecycle.deleted')
                              : undefined
                          }
                          onClick={() => setAction({ kind: 'grant', tenant })}
                        >
                          <Coins size={15} aria-hidden="true" />
                          <span>{t('tenants.actions.grant')}</span>
                        </button>
                        <button
                          className="ghost-button ghost-button-danger"
                          type="button"
                          disabled={isPending || tenant.deleted_at !== null}
                          title={
                            tenant.deleted_at !== null
                              ? t('tenants.lifecycle.deleted')
                              : undefined
                          }
                          onClick={() => setAction({ kind: 'deactivate', tenant })}
                        >
                          <Power size={15} aria-hidden="true" />
                          <span>{t('tenants.actions.deactivate')}</span>
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <PaginationBar page={page} />

      <TenantActionDialog
        action={action}
        onClose={() => setAction(null)}
        onPlanChange={onPlanChange}
        onGrant={onGrant}
        onDeactivate={onDeactivate}
        balanceFormatter={(value) => formatCredits(value)}
      />
    </section>
  )
}
