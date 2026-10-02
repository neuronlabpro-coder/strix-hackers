import { useCallback, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getAdminAuditLog } from '../../lib/adminApi'
import { activeLocale, formatCount } from '../../lib/format'
import type { AdminAuditEntry } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { PaginationBar } from './PaginationBar'
import { useAdminPage } from './useAdminPage'

/**
 * Visor global del rastro de auditoría.
 *
 * ## Por qué esta vista solo lee
 *
 * La tabla de auditoría es *append-only* por R4: no hay endpoint que la modifique ni modo
 * de operación que lo haga. No es una decisión de interfaz tomada por prudencia, es una
 * propiedad del almacenamiento, y por eso la vista no tiene ni siquiera un botón de
 * "corregir": ofrecerlo y rechazarlo en el servidor haría que el operador perdiera el
 * tiempo escribiendo un texto que se pierde.
 *
 * ## Por qué el actor se muestra como «Sistema» y no vacío
 *
 * Muchas entradas las escribe la plataforma —una baja automática, un trabajo de
 * mantenimiento— y no una persona. Dejar la celda vacía haría que el operador leyera
 * "nadie lo hizo" en vez de "lo hizo la plataforma sin intervención humana", que es
 * justo la diferencia que un rastro forense necesita marcar.
 *
 * ## Por qué el filtro de acción es de coincidencia exacta
 *
 * El backend solo admite igualdad. Una búsqueda parcial sobre nombres de acción
 * devolvería entradas de otros eventos en una vista cuyo propósito es destacar una acción
 * concreta, y el operador leería como sospechosa una entrada que no lo era. Si algún día
 * hace falta la búsqueda parcial, se añade al servidor, no aquí.
 */
export function AdminAuditPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()

  const [search, setSearch] = useState('')
  const [action, setAction] = useState('')

  const load = useCallback(
    (limit: number, offset: number) =>
      // El hook recibe `disabled` y no pide nada sin sesión; esta guarda solo evita pasar
      // un token inexistente y devuelve una página vacía en vez de lanzar la petición.
      token === null
        ? Promise.resolve({ items: [], total: 0 })
        : getAdminAuditLog(token, {
            search: search === '' ? null : search,
            action: action === '' ? null : action,
            limit,
            offset,
          }),
    [action, search, token],
  )

  const page = useAdminPage<AdminAuditEntry>(load, {
    // La página de auditoría trae cincuenta filas por defecto en el servidor porque son
    // cortas y se leen Follow el scroll; veinticinco obligaría a paginar para ver una hora
    // de actividad.
    pageSize: 50,
    filterKey: `${token}:${search}:${action}`,
    disabled: !token || user?.is_superuser !== true,
  })

  return (
    <section className="page-section" aria-labelledby="admin-audit-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="admin-audit-title">{t('audit.title')}</h1>
          <p className="page-description">{t('audit.description')}</p>
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
          <span className="field-label">{t('tenants.filters.search')}</span>
          <input
            className="text-input"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('audit.searchPlaceholder')}
          />
        </label>
        <label className="field">
          <span className="field-label">{t('audit.columns.action')}</span>
          <input
            className="text-input mono"
            value={action}
            onChange={(event) => setAction(event.target.value)}
            placeholder={t('audit.actionPlaceholder')}
          />
        </label>
        {search !== '' || action !== '' ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() => {
              setSearch('')
              setAction('')
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
      ) : page.isLoading && page.items.length === 0 ? (
        <div className="console-skeleton" aria-hidden="true">
          {Array.from({ length: 6 }, (_, indice) => (
            <div className="console-skeleton-row" key={indice}>
              <div className="console-skeleton-bar" style={{ flex: '2 1 0' }} />
              <div className="console-skeleton-bar" style={{ flex: '1 1 0' }} />
              <div className="console-skeleton-bar" style={{ flex: '3 1 0' }} />
              <div className="console-skeleton-bar" style={{ flex: '1 1 0' }} />
            </div>
          ))}
        </div>
      ) : page.items.length === 0 ? (
        <div className="empty-card">
          <p>{t('audit.empty')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table console-table">
            <caption className="visually-hidden">{t('audit.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('audit.columns.created')}</th>
                <th scope="col">{t('audit.columns.action')}</th>
                <th scope="col">{t('audit.columns.entity')}</th>
                <th scope="col">{t('audit.columns.actor')}</th>
                <th scope="col">{t('audit.columns.tenant')}</th>
                <th scope="col">{t('audit.columns.transition')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    <span className="mono timestamp">
                      {new Intl.DateTimeFormat(activeLocale(), {
                        dateStyle: 'medium',
                        timeStyle: 'medium',
                      }).format(new Date(entry.created_at))}
                    </span>
                  </td>
                  <td>
                    {/*
                      La accion se pinta con su nombre y no con el valor del enum.

                      `UNSUPPORTED_USAGE_UNBILLED` no es un nombre, es un identificador: el
                      operador que lee el rastro tiene que saber que ese asiento corresponde a un
                      paso de chat que se entrego sin cobrar, y para eso hay que leer una frase.
                      La etiqueta vive en `admin.audit.actions`, una entrada por cada valor de
                      `AuditActionEnum`, y el backend y el frontend se comprueban el uno al otro
                      para que anadir una accion al enum sin traducirla sea un fallo y no una
                      cadena en crudo en pantalla.

                      El `title` deja el identificador a un hover, porque el rastro tiene que poder
                      casarse con un log del servidor y con una entrada del enum, y el nombre
                      traducido no sirve para eso.

                      Y el `defaultValue` es el propio valor: `AuditActionEnum` es un `StrEnum` de
                      una columna `Text`, de modo que una accion escrita por una version anterior
                      del codigo puede seguir en la tabla sin etiqueta. En ese caso se ensena el
                      identificador, que es lo unico que se puede decir con verdad, en vez de
                      inventar una etiqueta.
                    */}
                    <span className="badge badge-on mono" title={entry.action}>
                      {t(`audit.actions.${entry.action}`, { defaultValue: entry.action })}
                    </span>
                  </td>
                  <td>
                    <span className="mono table-secondary">
                      {entry.entity_type} / {entry.entity_id.slice(0, 8)}
                    </span>
                  </td>
                  <td>
                    {entry.actor_email ?? (
                      <span className="cell-muted">{t('audit.system')}</span>
                    )}
                  </td>
                  <td>
                    {entry.organization_name ?? (
                      <span className="cell-muted">{t('sales.noTenant')}</span>
                    )}
                  </td>
                  <td>
                    {entry.from_state === null && entry.to_state === null ? (
                      <span className="cell-muted">{t('audit.noTransition')}</span>
                    ) : (
                      <span className="mono table-secondary">
                        {entry.from_state ?? t('audit.noTransition')} → {entry.to_state ?? '?'}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PaginationBar page={page} />

      <p className="chart-empty">{t('audit.total', { total: formatCount(page.total) })}</p>
    </section>
  )
}
