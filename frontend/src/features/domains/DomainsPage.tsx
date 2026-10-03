/**
 * Vista de dominios: alta, verificación de propiedad, filtros y borrado.
 *
 * ## Por qué el botón de verificar existe aunque el alta no verifique
 *
 * Porque la propagación de DNS no es instantánea y a veces tarda horas. Verificar en el acto
 * del alta daría un `NO_TXT` en el 99 % de los casos y el usuario concluiría que la
 * plataforma está rota. Verificar es una acción aparte, repetible, que el usuario lanza
 * cuando su proveedor ya dice que el registro está publicado.
 *
 * ## Por qué el resultado de la verificación se muestra con el texto del servidor
 *
 * Porque el estado lo decide el DNS —`NO_TXT` y `NXDOMAIN` son cosas distintas y la acción
 * que corresponde a cada una también— y el backend lo sabe mejor que el panel. Lo que
 * llega es una **clave** de i18n, no un texto: el mensaje se pinta en el idioma activo y no
 * hay una cadena en español incrustada que se tenga que traducir a mano.
 *
 * ## Por qué el rango de fechas es de **alta** y no de verificación
 *
 * Porque `verified_at` es `NULL` en todos los dominios pendientes, y un filtro sobre una
 * columna anulable no recorta filas: las borra. En cuanto se tocara cualquiera de las dos
 * fechas, los dominios sin verificar desaparecerían de la tabla, que es justo lo que se
 * quiere ver cuando se pregunta «¿cuáles me quedan por publicar?». La columna de la tabla
 * sigue mostrando la fecha de verificación, y el filtro se anuncia como rango de alta para
 * que no se confundan.
 */

import { useMemo, useState } from 'react'
import { Plus, RefreshCw, Search, ShieldCheck, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/useAuth'
import {
  createDomain,
  deleteDomain,
  DomainConflictError,
  verifyDomain,
} from '../../lib/assetsApi'
import type { DomainVerificationMethod, VerifiedDomain } from '../../types/assets'
import { formatDate } from '../../lib/format'
import { Pagination } from '../shared/Pagination'
import { useToast } from '../shared/toast-context'
import { AddDomainDialog } from './AddDomainDialog'
import {
  EMPTY_QUERY,
  VERIFICATION_FILTERS,
  useDomains,
  type DomainsQuery,
} from './useDomains'

export function DomainsPage() {
  const { t } = useTranslation('domains')
  const { token, selectedOrganizationId } = useAuth()
  const organizationId = selectedOrganizationId
  const { notify } = useToast()

  const { page, isLoading, loadFailed, query, setQuery, setPage, reload } = useDomains()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<VerifiedDomain | null>(null)
  const [conflictMessage, setConflictMessage] = useState<string | null>(null)
  const [conflictIsOwn, setConflictIsOwn] = useState(false)
  const [verifyingId, setVerifyingId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  const items = useMemo(() => page?.items ?? [], [page])

  const hasFilters = Boolean(query.status || query.search.trim() || query.createdFrom || query.createdTo)

  function cambiar(campo: 'search' | 'createdFrom' | 'createdTo', valor: string): void {
    setQuery({ ...query, [campo]: valor } satisfies DomainsQuery)
  }

  /**
   * Token y workspace ya estrechados, o `null` si aún no los hay.
   *
   * Los manejadores de acción de esta vista solo existen dentro de `ProtectedShell`, donde
   * los dos están siempre. Estrechar aquí en vez de repetir la comprobación en cada uno
   * deja que el tipo lo diga **una** vez, y convierte un `undefined` en un retorno
   * temprano en vez de una petición con cabeceras vacías que el servidor rechaza con un
   * `403` que no explica nada.
   *
   * La **lectura** no usa esto: para el listado la clave de la petición es la que lleva el
   * identificador del workspace dentro, y eso garantiza que lo que hay en pantalla es del
   * workspace de la cabecera, no el que hubiera en memoria al escribirse el manejador.
   */
  const sesion = useMemo(() => {
    if (token === null || organizationId === null) {
      return null
    }
    return { token, organizationId }
  }, [token, organizationId])

  const closeDialog = () => {
    setDialogOpen(false)
    setCreated(null)
    setConflictMessage(null)
    setConflictIsOwn(false)
  }

  const submitDomain = async (name: string, method: DomainVerificationMethod) => {
    if (sesion === null) {
      return
    }
    setCreating(true)
    setConflictMessage(null)
    setConflictIsOwn(false)
    try {
      const dominio = await createDomain(sesion.token, sesion.organizationId, {
        domain_name: name,
        verification_method: method,
      })
      setCreated(dominio)
      reload()
    } catch (error) {
      if (error instanceof DomainConflictError) {
        // El motivo decide el mensaje, no al revés. Si el dominio ya es de este workspace
        // el botón correcto es abrirlo; si es de otro, no hay ninguna acción y solo cabe
        // explicarlo.
        setConflictIsOwn(error.motivo === 'ESTE_WORKSPACE')
        setConflictMessage(t(`conflict.${error.motivo}`))
      } else {
        notify('error', t('errors.create'))
      }
    } finally {
      setCreating(false)
    }
  }

  const runVerification = async (domainId: string) => {
    if (sesion === null) {
      return
    }
    setVerifyingId(domainId)
    try {
      const resultado = await verifyDomain(sesion.token, sesion.organizationId, domainId)
      notify(
        resultado.is_verified ? 'success' : 'info',
        t(resultado.message_key, { defaultValue: t(`verify.${resultado.outcome}`) }),
      )
      reload()
    } catch {
      notify('error', t('errors.verify'))
    } finally {
      setVerifyingId(null)
    }
  }

  const removeDomain = async (domainId: string) => {
    if (sesion === null) {
      return
    }
    setDeletingId(domainId)
    try {
      await deleteDomain(sesion.token, sesion.organizationId, domainId)
      notify('success', t('toasts.deleted'))
      reload()
    } catch {
      notify('error', t('errors.delete'))
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <section className="page-section" aria-labelledby="domains-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="domains-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <button className="secondary-button" type="button" onClick={reload}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('actions.refresh')}</span>
          </button>
          <button className="primary-button" type="button" onClick={() => setDialogOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            <span>{t('actions.add')}</span>
          </button>
        </div>
      </div>

      {/* Los cuatro filtros llevan etiqueta visible, incluido el buscador. Con etiquetas en
          todos, `align-items: start` de la barra los deja compartiendo línea de control; un
          buscador sin etiqueta sería una fila más corta y quedaría pegado a las etiquetas de
          al lado, que es justo lo que el `align-self: end` de
          `.filter-bar > .search-field` viene a arreglar en las pantallas que no llevan
          etiqueta. */}
      <div className="filter-bar">
        <div className="filter-field filter-field-search">
          <label htmlFor="domains-search">{t('filters.search')}</label>
          <span className="search-field">
            <Search size={16} aria-hidden="true" />
            <input
              id="domains-search"
              type="search"
              value={query.search}
              placeholder={t('filters.searchPlaceholder')}
              onChange={(event) => cambiar('search', event.target.value)}
            />
          </span>
        </div>
        <div className="filter-field">
          <label htmlFor="domains-status">{t('filters.status')}</label>
          <select
            id="domains-status"
            value={query.status ?? ''}
            onChange={(event) =>
              setQuery({
                ...query,
                status: (event.target.value || null) as DomainsQuery['status'],
              })
            }
          >
            <option value="">{t('filters.allStatuses')}</option>
            {VERIFICATION_FILTERS.map((estado) => (
              <option key={estado} value={estado}>
                {t(`status.${estado}`)}
              </option>
            ))}
          </select>
        </div>
        {/* El rango va sobre la fecha de **alta** del dominio, no sobre la de verificación que
            muestra la tabla. La explicación va en el `title` de las dos etiquetas, que es donde
            cabe sin romper la alineación de la barra: una línea de ayuda dentro del
            `.filter-field` añadiría altura a un solo campo y descuadraría la fila. */}
        <div className="filter-field">
          <label htmlFor="domains-created-from" title={t('filters.dateHint')}>
            {t('filters.dateFrom')}
          </label>
          <input
            id="domains-created-from"
            type="date"
            value={query.createdFrom}
            onChange={(event) => cambiar('createdFrom', event.target.value)}
          />
        </div>
        <div className="filter-field">
          <label htmlFor="domains-created-to" title={t('filters.dateHint')}>
            {t('filters.dateTo')}
          </label>
          <input
            id="domains-created-to"
            type="date"
            value={query.createdTo}
            onChange={(event) => cambiar('createdTo', event.target.value)}
          />
        </div>
        {hasFilters ? (
          <button
            className="ghost-button filter-bar-clear"
            type="button"
            onClick={() => setQuery(EMPTY_QUERY)}
          >
            <span>{t('actions.clearFilters')}</span>
          </button>
        ) : null}
      </div>

      {/* `isLoading` a secas y no `isLoading && page === null`: el hook conserva la página
          anterior mientras llega la nueva, y con la segunda forma se pintarían las filas del
          filtro anterior bajo el título del nuevo. */}
      {isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="empty-card">
          <h2>{hasFilters ? t('states.noResultsTitle') : t('states.emptyTitle')}</h2>
          <p>{hasFilters ? t('states.noResultsDescription') : t('states.emptyDescription')}</p>
        </div>
      ) : (
        <>
          <div className="table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('table.domain')}</th>
                  <th scope="col">{t('table.status')}</th>
                  <th scope="col">{t('table.assets')}</th>
                  <th scope="col">{t('table.txtRecord')}</th>
                  <th scope="col">{t('table.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((domain) => (
                  <tr key={domain.id}>
                    <th scope="row" className="mono">
                      {domain.domain_name}
                    </th>
                    <td>
                      {domain.is_verified ? (
                        <span className="badge badge-status-verified">
                          <ShieldCheck size={14} aria-hidden="true" />
                          {t('status.VERIFIED')}
                        </span>
                      ) : (
                        <span className="badge badge-status-pending">
                          {t('status.PENDING')}
                        </span>
                      )}
                      {domain.verified_at !== null ? (
                        <span className="assets-subtext">{formatDate(domain.verified_at)}</span>
                      ) : null}
                    </td>
                    <td className="mono">{domain.asset_count}</td>
                    <td className="mono assets-wrap">{domain.txt_record_name}</td>
                    <td>
                      <div className="assets-row-actions">
                        <button
                          className="secondary-button"
                          type="button"
                          onClick={() => void runVerification(domain.id)}
                          disabled={verifyingId === domain.id}
                        >
                          <span>
                            {verifyingId === domain.id
                              ? t('actions.verifying')
                              : t('actions.verifyNow')}
                          </span>
                        </button>
                        {domain.is_verified ? null : (
                          <button
                            className="assets-icon-action"
                            type="button"
                            onClick={() => void removeDomain(domain.id)}
                            disabled={deletingId === domain.id}
                            aria-label={t('actions.delete')}
                          >
                            <Trash2 size={16} aria-hidden="true" />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* La paginación va **debajo** de la tabla y habla el idioma del endpoint: `total`,
              `limit` y `offset` llegan en la respuesta y el componente no traduce nada de su
              cuenta. Solo aparece si hay más de una página. */}
          {page ? (
            <Pagination
              total={page.total}
              limit={page.limit}
              offset={page.offset}
              onOffsetChange={setPage}
              namespace="domains"
            />
          ) : null}
        </>
      )}

      <AddDomainDialog
        open={dialogOpen}
        created={created}
        creating={creating}
        conflictMessage={conflictMessage}
        conflictIsOwn={conflictIsOwn}
        onSubmit={(name, method) => void submitDomain(name, method)}
        onClose={closeDialog}
      />
    </section>
  )
}
