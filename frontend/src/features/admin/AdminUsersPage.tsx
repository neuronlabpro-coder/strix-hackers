import { useCallback, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getAdminUsers } from '../../lib/adminApi'
import {activeLocale, formatCount} from '../../lib/format'
import type { AdminUser } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { PaginationBar } from './PaginationBar'
import { useAdminPage } from './useAdminPage'

/**
 * Listado global de usuarios.
 *
 * ## Por qué el hash de contraseña no se pide
 *
 * La respuesta la construye el servidor con una lista explícita de campos, así que
 * añadir una columna sensible a la tabla de usuarios no la expone. Esta vista solo decide
 * cómo pintar lo que llega, y la garantía de que no llega de más es del backend —donde
 * está el riesgo— y no de que esta pantalla se acuerde de qué campos no mostrar.
 *
 * ## Por qué las organizaciones van en una celda y no en columnas
 *
 * Un usuario puede pertenecer a varios workspaces. Una columna por workspace y un `JOIN`
 * repetirían la fila del usuario tantas veces como workspaces tenga, y una celda con los
 * nombres concatenados no obliga a mirar dos veces para saber a qué pertenece cada
 * cuenta.
 */
export function AdminUsersPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()

  const [search, setSearch] = useState('')
  const [onlySuperusers, setOnlySuperusers] = useState(false)

  const load = useCallback(
    (limit: number, offset: number) =>
      // El hook recibe `disabled` y no pide nada sin sesión; esta guarda solo evita pasar
      // un token inexistente y devuelve una página vacía en vez de lanzar la petición.
      token === null
        ? Promise.resolve({ items: [], total: 0 })
        : getAdminUsers(token, {
            search: search === '' ? null : search,
            onlySuperusers,
            limit,
            offset,
          }),
    [onlySuperusers, search, token],
  )

  const page = useAdminPage<AdminUser>(load, {
    pageSize: 25,
    filterKey: `${token}:${search}:${onlySuperusers}`,
    disabled: !token || user?.is_superuser !== true,
  })

  return (
    <section className="page-section" aria-labelledby="admin-users-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="admin-users-title">{t('users.title')}</h1>
          <p className="page-description">{t('users.description')}</p>
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
            placeholder={t('users.searchPlaceholder')}
          />
        </label>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={onlySuperusers}
            onChange={(event) => setOnlySuperusers(event.target.checked)}
          />
          <span>{t('users.onlySuperusers')}</span>
        </label>
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
          <p>{t('users.empty')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="visually-hidden">{t('users.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('users.columns.email')}</th>
                <th scope="col">{t('users.columns.name')}</th>
                <th scope="col">{t('users.columns.organizations')}</th>
                <th scope="col">{t('users.columns.superuser')}</th>
                <th scope="col">{t('users.columns.verified')}</th>
                <th scope="col">{t('users.columns.active')}</th>
                <th scope="col">{t('users.columns.created')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((row) => (
                <tr key={row.id}>
                  <th scope="row" className="cell-primary">
                    <span className="mono">{row.email}</span>
                  </th>
                  <td>{row.full_name}</td>
                  <td>
                    {row.organizations.length === 0 ? (
                      <span className="cell-muted">{t('users.noOrganizations')}</span>
                    ) : (
                      <span className="tag-list">
                        {row.organizations.map((name) => (
                          <span key={name} className="badge">
                            {name}
                          </span>
                        ))}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className={row.is_superuser ? 'badge badge-on' : 'badge'}>
                      {row.is_superuser ? t('users.yes') : t('users.no')}
                    </span>
                  </td>
                  <td>
                    <span className={row.email_verified ? 'badge badge-on' : 'badge terminal-warning'}>
                      {row.email_verified ? t('users.yes') : t('users.no')}
                    </span>
                  </td>
                  <td>
                    <span className={row.is_active ? 'badge badge-on' : 'badge terminal-error'}>
                      {row.is_active ? t('users.active') : t('users.inactive')}
                    </span>
                  </td>
                  <td>
                    <span className="mono timestamp">
                      {new Intl.DateTimeFormat(activeLocale(), { dateStyle: 'medium' }).format(
                        new Date(row.created_at),
                      )}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PaginationBar page={page} />

      <p className="chart-empty">{t('users.total', { total: formatCount(page.total) })}</p>
    </section>
  )
}
