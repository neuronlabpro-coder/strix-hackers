import { useCallback, useMemo } from 'react'
import { Lock } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { getAuditLog } from '../../lib/api'
import { formatDateTime } from '../../lib/format'
import { useAuth } from '../auth/useAuth'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { AuditLogEntry } from '../../types/api'
import { PlanGate } from './PlanGate'

/**
 * Vista *Registro de auditoría* de Ajustes.
 *
 * ## Por qué usa `/api/v1/audit-log/` y no `/api/v1/admin/audit`
 *
 * Porque el endpoint de la consola exige `is_superuser` y esta vista la ve un cliente
 * Enterprise, que no lo es. Reutilizar la ruta de administración habría producido un `403`
 * en la pantalla exacta que la puerta de Enterprise acaba de dejar pasar: el plan se
 * cobraría y el rastro no se podría leer.
 *
 * La ruta de tenant filtra por organización en el servidor. La puerta del plan la decide el
 * **panel**, porque es una decisión comercial; el aislamiento lo decide el servidor. Un
 * workspace sin Enterprise no puede leer su propio rastro porque el panel no se lo enseña,
 * y si se manipulase la URL seguiría leyendo solo el suyo.
 *
 * ## Por qué el superusuario ve la vista sin plan
 *
 * Porque administra la plataforma y puede necesitar revisar el rastro de un workspace que
 * no es suyo desde el propio panel. La nota al pie lo dice en pantalla, porque una vista
 * visible sin explicación de por qué se ve parece a un fallo de la puerta.
 */
export function SettingsAuditPage() {
  const { t } = useTranslation('settings')
  const { token, user, organizations, selectedOrganizationId } = useAuth()

  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? null
  const esSuperusuario = user?.is_superuser === true
  const esEnterprise = organization?.plan_tier === 'ENTERPRISE'
  const tieneAcceso = esEnterprise || esSuperusuario

  const organizationId = selectedOrganizationId
  const organizationName = organization?.name ?? ''

  const fetcher = useCallback(
    async (key: string): Promise<AuditLogEntry[]> => {
      const activeToken = token
      if (activeToken === null) {
        throw new Error('sin token')
      }
      // `key` es el identificador del workspace y se pasa como filtro **siempre**. Es lo
      // que impide que la vista devuelva el rastro de todos los tenants: el endpoint acepta
      // el filtro como opcional, así que omitirlo sería una fuga entre workspaces. Por eso
      // la clave no es un adorno que el hook ignore, es el argumento de la petición.
      const page = await getAuditLog(activeToken, key, { limit: 100 })
      return page.items
    },
    [token],
  )

  // Sin acceso no se pide nada. Devolver `null` como clave es lo que detiene la petición, y
  // es mejor que pedir y filtrar en el cliente: una respuesta con datos de otro workspace
  // que se descartan después ya ha cruzado la frontera.
  const { data, isLoading, loadFailed, reload } = useAsyncResource<AuditLogEntry[]>(
    fetcher,
    tieneAcceso ? organizationId : null,
  )

  const entries = useMemo(() => data ?? [], [data])

  if (!tieneAcceso) {
    return (
      <PlanGate
        title={t('audit.gatedTitle')}
        body={t('audit.gatedBody')}
        items={[
          t('audit.gatedItems.immutable'),
          t('audit.gatedItems.complete'),
          t('audit.gatedItems.retention'),
          t('audit.gatedItems.export'),
        ]}
        actionLabel={t('audit.gatedAction')}
        actionHref="/settings/billing"
        footnote={t('audit.gatedFootnote')}
        badge={
          <>
            <Lock size={12} aria-hidden="true" />
            <span>ENTERPRISE</span>
          </>
        }
      />
    )
  }

  return (
    <section className="settings-section">
      <header className="settings-section-heading">
        <h1>{t('audit.title')}</h1>
        <p>{t('audit.subtitle')}</p>
      </header>

      <div className="panel">
        <p className="danger-note">{t('audit.appendOnlyNotice')}</p>
        <p className="field-hint">{organizationName}</p>
      </div>

      {isLoading && entries.length === 0 ? (
        <p className="field-hint">{t('states.loading')}</p>
      ) : null}

      {loadFailed ? (
        <div className="empty-card">
          <p>{t('audit.loadError')}</p>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : null}

      {!isLoading && !loadFailed && entries.length > 0 ? (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('audit.date')}</th>
                <th scope="col">{t('audit.action')}</th>
                <th scope="col">{t('audit.entity')}</th>
                <th scope="col">{t('audit.fromState')}</th>
                <th scope="col">{t('audit.toState')}</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{formatDateTime(entry.created_at)}</td>
                  <td>
                    <span className="badge badge-muted">{entry.action}</span>
                  </td>
                  <td>
                    <span className="provider-cell">{entry.entity_type}</span>
                  </td>
                  <td>
                    <span className="provider-cell">{entry.from_state ?? '—'}</span>
                  </td>
                  <td>
                    <span className="provider-cell">{entry.to_state ?? '—'}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {!isLoading && !loadFailed && entries.length === 0 ? (
        <p className="field-hint">{t('audit.empty')}</p>
      ) : null}

      <div className="settings-form-actions">
        <Link className="secondary-button" to="/settings/billing">
          <span>{t('audit.gatedAction')}</span>
        </Link>
      </div>
    </section>
  )
}
