import { useCallback, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { getAdminUsers, updateAdminUser } from '../../lib/adminApi'
import { activeLocale, formatCount } from '../../lib/format'
import type { AdminUser, AdminUserUpdate } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
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
 * ## Por qué el rol viaja con el nombre del workspace
 *
 * Un usuario puede ser `admin` en un workspace y `member` en otro. Una columna con un
 * único rol obligaría al operador a adivinar cuál, y degradar a alguien en el workspace
 * equivocado es un daño real.
 */
export function AdminUsersPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()
  const { notify } = useToast()

  const [search, setSearch] = useState('')
  const [onlySuperusers, setOnlySuperusers] = useState(false)
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set())
  const [confirming, setConfirming] = useState<AdminUser | null>(null)

  /**
   * El botón de limpiar aparece solo si hay algo que limpiar.
   *
   * Siempre visible, un botón que no hace nada es un botón que el operador aprende a ignorar.
   * Con dos filtros que se limpian con un clic, el criterio es `hayAlgoQueLimpiar`: o el texto
   * tiene algo que no sean espacios —el servidor descarta los espacios, y el botón debe
   * comparar contra el mismo criterio que la consulta— o la casilla está marcada.
   */
  const hayFiltros = search.trim() !== '' || onlySuperusers

  function limpiarFiltros(): void {
    setSearch('')
    setOnlySuperusers(false)
  }

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

  /**
   * Aplica un cambio y refresca la tabla en vez de parchear la fila.
   *
   * `is_superuser` decide si el propio operador puede seguir entrando, así que una fila
   * parcheada en local podría mostrar un estado que el servidor rechazó —quitarse el
   * superusuario al último devuelve `409`— y dejaría la pantalla mintiendo sobre el acceso
   * del que depende. Recargar cuesta una petición y devuelve la verdad.
   */
  const applyChange = useCallback(
    async (target: AdminUser, changes: AdminUserUpdate) => {
      if (token === null) {
        return
      }
      setPendingIds((current) => new Set(current).add(target.id))
      try {
        await updateAdminUser(token, target.id, changes)
        notify('success', t('users.messages.updated'))
        page.refresh()
      } catch {
        notify('error', t('users.messages.error'))
      } finally {
        setPendingIds((current) => {
          const next = new Set(current)
          next.delete(target.id)
          return next
        })
      }
    },
    [notify, page, t, token],
  )

  const onConfirm = useCallback(() => {
    if (confirming === null) {
      return
    }
    const objetivo = confirming
    setConfirming(null)
    void applyChange(objetivo, { is_active: !objetivo.is_active })
  }, [applyChange, confirming])

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
          <span className="field-label">{t('tenants.filters.search')}</span>
          <input
            className="text-input"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('users.searchPlaceholder')}
          />
        </label>
        {/*
            El descriptor es `filter-field-check`, no `checkbox-field`, y **sin** `filter-field`.

            Las dos clases existen y describen cosas distintas. `checkbox-field` es una **píldora
            suelta**: se alinea por su centro (`align-self: start`, `min-height: 32px`), lleva
            borde y fondo propios, y es lo que corresponde a una casilla que va por su cuenta —
            la de Operaciones. `filter-field-check` es lo contrario: no dibuja caja, y reserva
            `padding-top: 28px` con `min-height: 64px` para caerse en la **línea de control** de
            una barra cuyos campos llevan etiqueta encima.

            Aquí el campo de al lado es un `.field` con etiqueta, así que la barra tiene fila de
            rótulo y fila de control. Con la píldora, la casilla se quedaba en la fila de rótulo:
            medido, su centro estaba **29,1 px** por encima del centro del input, que es el
            desplazamiento que documentaba la lista de pendientes.

            Y sin `filter-field` a propósito, aunque en CVE la casilla sí lo lleva. Porque
            `.admin-shell .filter-bar > .filter-field` es (0,3,0) y le gana a `.filter-field-check`
            (0,1,0): le devuelve `flex-direction: column`, la casilla se iba encima de su texto y
            el campo medía 72 px en vez de 64. El `label` con `htmlFor` sustituye al `<label>`
            contenedor, que es lo que hace bien la accesibilidad aquí: el texto sigue siendo la
            etiqueta de la casilla y el área pulsable es la fila entera.
        */}
        <div className="filter-field-check">
          <input
            id="admin-users-only-superusers"
            type="checkbox"
            checked={onlySuperusers}
            onChange={(event) => setOnlySuperusers(event.target.checked)}
          />
          <label htmlFor="admin-users-only-superusers">{t('users.onlySuperusers')}</label>
        </div>
        {hayFiltros ? (
          <button className="secondary-button" type="button" onClick={limpiarFiltros}>
            <span>{t('users.filters.clear')}</span>
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
          <p>{hayFiltros ? t('users.emptyFiltered') : t('users.empty')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table console-table">
            <caption className="visually-hidden">{t('users.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('users.columns.email')}</th>
                <th scope="col">{t('users.columns.name')}</th>
                <th scope="col">{t('users.columns.roles')}</th>
                <th scope="col">{t('users.columns.superuser')}</th>
                <th scope="col">{t('users.columns.verified')}</th>
                <th scope="col">{t('users.columns.active')}</th>
                <th scope="col">{t('users.columns.created')}</th>
                <th scope="col">{t('tenants.columns.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((row) => {
                const isPending = pendingIds.has(row.id)
                return (
                  <tr key={row.id}>
                    <th scope="row" className="cell-primary">
                      <span className="mono">{row.email}</span>
                    </th>
                    <td>{row.full_name}</td>
                    <td>
                      {row.roles.length === 0 ? (
                        <span className="cell-muted">{t('users.noRoles')}</span>
                      ) : (
                        <span className="tag-list">
                          {row.roles.map((rol) => (
                            <span key={rol} className="badge">
                              {rol}
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
                    <td>
                      <div className="webhook-actions">
                        {/*
                          Desactivar pide confirmación y cambiar el superusuario no.

                          Desactivar corta el acceso de alguien que quizá solo comets un
                          error, y es reversible con el mismo botón. Quitarle el
                          superusuario a otra persona es una decisión de plataforma, y el
                          servidor ya la protege: si fuera el último, devuelve `409` y no
                          ocurre nada. Por eso no hay un diálogo que lo impida: lo que lo
                          impide es el servidor, y un aviso en pantalla que no coincide
                          con la regla real sería peor que no avisar.
                        */}
                        <button
                          className="link-button"
                          type="button"
                          disabled={isPending}
                          onClick={() => setConfirming(row)}
                        >
                          <span>
                            {row.is_active
                              ? t('users.actions.toggleInactive')
                              : t('users.actions.toggleActive')}
                          </span>
                        </button>
                        <button
                          className="link-button"
                          type="button"
                          disabled={isPending}
                          onClick={() =>
                            void applyChange(row, { is_superuser: !row.is_superuser })
                          }
                        >
                          <span>
                            {row.is_superuser
                              ? t('users.actions.toggleSuper')
                              : t('users.actions.grantSuper')}
                          </span>
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

      <p className="chart-empty">{t('users.total', { total: formatCount(page.total) })}</p>

      {confirming !== null ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setConfirming(null)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="user-confirm-title"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setConfirming(null)
              }
            }}
          >
            <header className="modal-header">
              <h2 id="user-confirm-title">
                {/*
                      Antes de esto habia un `.replace('{name}', ...)` a mano sobre el
                      resultado de `t()`. Funcionaba solo porque la traduccion estaba rota:
                      i18next interpola con llaves dobles, y con llaves simples las dejaba
                      tal cual, asi que el `replace` encontraba el texto. En cuanto las
                      llaves se han arreglado, ese `replace` ya no encontraria nada y el
                      titulo se quedaria con un `{{name}}` dentro.

                      El nombre se pasa como variable de `t()` y se hace por el camino que
                      llevan las otras 32 pantallas del panel.
                    */}
                {confirming.is_active
                  ? t('users.confirm.title', { name: confirming.full_name || confirming.email })
                  : t('users.confirmReactivate.title', {
                      name: confirming.full_name || confirming.email,
                    })}
              </h2>
              <button
                className="icon-button"
                type="button"
                onClick={() => setConfirming(null)}
                aria-label={t('actions.close')}
              >
                <span aria-hidden="true">×</span>
              </button>
            </header>
            <p className="modal-caption">
              {confirming.is_active
                ? t('users.confirm.caption')
                : t('users.confirmReactivate.caption')}
            </p>
            <footer className="modal-footer">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setConfirming(null)}
              >
                <span>{t('actions.cancel')}</span>
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={onConfirm}
              >
                <span>
                  {confirming.is_active
                    ? t('users.confirm.confirm')
                    : t('users.confirmReactivate.confirm')}
                </span>
              </button>
            </footer>
          </div>
        </div>
      ) : null}
    </section>
  )
}
