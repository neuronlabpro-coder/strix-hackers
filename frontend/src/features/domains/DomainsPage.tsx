/**
 * Vista de dominios: alta, verificación de propiedad y borrado.
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
 */

import { useCallback, useMemo, useState } from 'react'
import { Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/useAuth'
import {
  createDomain,
  deleteDomain,
  DomainConflictError,
  listDomains,
  verifyDomain,
} from '../../lib/assetsApi'
import type { DomainListResponse, DomainVerificationMethod } from '../../types/assets'
import { formatDate } from '../../lib/format'
import { useAsyncResource } from '../shared/useAsyncResource'
import { useToast } from '../shared/toast-context'
import { AddDomainDialog } from './AddDomainDialog'

export function DomainsPage() {
  const { t } = useTranslation('domains')
  const { token, selectedOrganizationId } = useAuth()
  const organizationId = selectedOrganizationId
  const { notify } = useToast()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<DomainListResponse['items'][number] | null>(null)
  const [conflictMessage, setConflictMessage] = useState<string | null>(null)
  const [conflictIsOwn, setConflictIsOwn] = useState(false)
  const [verifyingId, setVerifyingId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  // La clave **es** el identificador del workspace, y no es un adorno que el hook
  // ignore: es lo que garantiza que los datos que hay en pantalla son los del workspace
  // de la cabecera. Cambiar de workspace cambia la clave, y con ella la petición.
  /**
   * Token y workspace ya estrechados, o `null` si aún no los hay.
   *
   * Los manejadores de acción de esta vista solo existen dentro de `ProtectedShell`, donde
   * los dos están siempre. Estrechar aquí en vez de repetir la comprobación en cada uno
   * deja que el tipo lo diga **una** vez, y convierte un `undefined` en un retorno
   * temprano en vez de una petición con cabeceras vacías que el servidor rechaza con un
   * `403` que no explica nada.
   *
   * El `fetcher` **no** usa esto: para leer, la clave **es** el identificador del
   * workspace, y eso garantiza que los datos en pantalla son los del workspace de la
   * cabecera, no el que hubiera en memoria al escribirse el manejador.
   */
  const sesion = useMemo(() => {
    if (token === null || organizationId === null) {
      return null
    }
    return { token, organizationId }
  }, [token, organizationId])

  const domains = useAsyncResource<DomainListResponse>(
    useCallback(
      async (key: string): Promise<DomainListResponse> => {
        const activeToken = token
        if (activeToken === null) {
          throw new Error('sin token')
        }
        return listDomains(activeToken, key)
      },
      [token],
    ),
    organizationId,
  )

  const items = useMemo(() => domains.data?.items ?? [], [domains.data])

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
      domains.reload()
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
      domains.reload()
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
      domains.reload()
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
          <button className="secondary-button" type="button" onClick={domains.reload}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('actions.refresh')}</span>
          </button>
          <button className="primary-button" type="button" onClick={() => setDialogOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            <span>{t('actions.add')}</span>
          </button>
        </div>
      </div>

      {domains.isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : domains.loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={domains.reload}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="empty-card">
          <h2>{t('states.emptyTitle')}</h2>
          <p>{t('states.emptyDescription')}</p>
        </div>
      ) : (
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
                        {t('status.verified')}
                      </span>
                    ) : (
                      <span className="badge badge-status-pending">{t('status.pending')}</span>
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
