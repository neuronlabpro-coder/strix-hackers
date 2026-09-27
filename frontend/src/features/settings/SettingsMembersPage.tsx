import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../lib/api'
import { formatDate } from '../../lib/format'
import {
  getWorkspaceMembers,
  removeWorkspaceMember,
  updateMemberRole,
} from '../../lib/workspaceApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { WorkspaceMember, WorkspaceRole } from '../../types/workspace'
import { RemoveMemberDialog } from './RemoveMemberDialog'

/**
 * Vista *Miembros* de Ajustes.
 *
 * ## Por qué la lista se pide entera y no se pagina
 *
 * Un workspace con equipo grande tendría que paginar, y la tentación es añadir paginación.
 * No se ha hecho a propósito: la lista cabe en un `GET` sin parámetros, y es lo que hace
 * posible que el selector de rol y el botón de retirar aparezcan **por fila** sin abrir un
 * diálogo de detalle por persona. Paginarla obligaría a abrir un modal para cambiar un rol,
 * que es un clic más en la operación más frecuente de la pantalla.
 *
 * Cuando un workspace tenga cientos de miembros esto tendrá que cambiar, y está anotado
 * para que quien lo lea sepa que es una decisión con fecha, no un descuido.
 *
 * ## Por qué el `409` del último admin se traduce y no se traga
 *
 * El servidor devuelve `409` cuando un cambio dejaría el workspace sin ningún
 * administrador, y `404` cuando el `user_id` no es miembro de este workspace. Son errores
 * **distintos con causas distintas**: el `409` lleva a invitar a otro admin, y el `404` no
 * lleva a ningún sitio porque no hay nada que corregir.
 *
 * Un único «no se pudo hacer» para los dos sería un mensaje que no ayuda: quien intentaba
 * bajar de rol necesita saber que le falta un admin, y quien intentaba tocar a alguien de
 * otro workspace no debería ni enterarse de que existe.
 */
export function SettingsMembersPage() {
  const { t } = useTranslation('settings')
  const { token, user, organizations, selectedOrganizationId } = useAuth()
  const { notify } = useToast()

  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? null
  const esAdmin = organization?.role === 'admin' || user?.is_superuser === true

  const [pendingId, setPendingId] = useState<string | null>(null)
  const [removeTarget, setRemoveTarget] = useState<WorkspaceMember | null>(null)

  const organizationId = selectedOrganizationId
  const currentUserId = user?.id

  const fetcher = useCallback(
    async (key: string) => {
      const activeToken = token
      if (activeToken === null) {
        throw new Error('sin token')
      }
      return getWorkspaceMembers(activeToken, key)
    },
    [token],
  )

  const { data, isLoading, loadFailed, reload } = useAsyncResource(fetcher, organizationId)

  const members = useMemo(() => data?.items ?? [], [data])

  async function onRoleChange(member: WorkspaceMember, role: WorkspaceRole) {
    if (!token || !organizationId) return
    if (role === member.role) return
    setPendingId(member.user_id)
    try {
      const response = await updateMemberRole(token, organizationId, member.user_id, { role })
      notify(
        'success',
        t('members.roleUpdated', {
          name: member.full_name,
          role: t(role === 'admin' ? 'members.roleAdmin' : 'members.roleMember'),
        }),
      )
      reloadWith(response.items)
    } catch (caught) {
      // El `409` es el caso que merece nombre: el cambio es legítimo y lo que no permite es
      // el estado. El mensaje dice qué hacer, no solo que no se pudo.
      if (caught instanceof ApiError && caught.status === 409) {
        notify('error', t('members.lastAdminError'))
      } else {
        notify('error', t('members.roleUpdateError', { name: member.full_name }))
      }
    } finally {
      setPendingId(null)
    }
  }

  /**
   * Escribe la lista que el servidor acaba de devolver, sin volver a pedirla.
   *
   * Los dos endpoints de gestión **devuelven la lista actualizada** en lugar de un `204`.
   * Es a propósito: la respuesta es la nueva lista, ya filtrada por el servidor, y
   * volver a pedirla sería una segunda fuente de verdad para el mismo conjunto que puede
   * desincronizarse de la primera si alguien cambia algo entre las dos peticiones.
   */
  function reloadWith(items: WorkspaceMember[]) {
    setMiembrosLocales(items)
    reload()
  }

  const [miembrosLocales, setMiembrosLocales] = useState<WorkspaceMember[] | null>(null)
  const listaFinal = miembrosLocales ?? members

  async function onRemoveConfirmed() {
    if (!token || !organizationId || removeTarget === null) return
    const objetivo = removeTarget
    setPendingId(objetivo.user_id)
    try {
      const response = await removeWorkspaceMember(token, organizationId, objetivo.user_id)
      setRemoveTarget(null)
      notify('success', t('members.removeSuccess', { name: objetivo.full_name }))
      setMiembrosLocales(response.items)
      reload()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        notify('error', t('members.lastAdminError'))
      } else {
        notify('error', t('members.roleUpdateError', { name: objetivo.full_name }))
      }
    } finally {
      setPendingId(null)
    }
  }

  if (loadFailed && listaFinal.length === 0) {
    return (
      <section className="settings-section">
        <div className="empty-card">
          <h2>{t('members.loadError')}</h2>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="settings-section">
      <header className="settings-section-heading">
        <h1>{t('members.title')}</h1>
        <p>{t('members.subtitle')}</p>
      </header>

      {!esAdmin ? (
        <div className="panel">
          <p className="danger-note">{t('members.lockedHint')}</p>
        </div>
      ) : null}

      {isLoading && listaFinal.length === 0 ? (
        <p className="field-hint">{t('states.loading')}</p>
      ) : null}

      {listaFinal.length > 0 ? (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('members.table.member')}</th>
                <th scope="col">{t('members.table.role')}</th>
                <th scope="col">{t('members.table.joined')}</th>
                <th scope="col">{t('members.table.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {listaFinal.map((member) => {
                const isSelf = currentUserId !== undefined && member.user_id === currentUserId
                const busy = pendingId === member.user_id
                return (
                  <tr key={member.user_id}>
                    <td>
                      <div className="member-cell">
                        <strong>{member.full_name}</strong>
                        <span>{member.email}</span>
                      </div>
                    </td>
                    <td>
                      {/*
                        El selector de rol es un `<select>` y no un par de botones porque hay
                        dos destinos y un desplegable los lleva a los dos en el mismo sitio.

                        Se deshabilita para el propio usuario no porque el servidor lo prohíba
                        —el backend no distingue— sino porque bajarse de admin a uno mismo es
                        casi siempre un clic accidental, y el servidor lo impediría con un
                        `409` solo si fuera el último admin. Deshabilitarlo aquí evita el viaje
                        de ida y vuelta para descubrirlo.
                      */}
                      <select
                        value={member.role}
                        disabled={!esAdmin || busy || isSelf}
                        onChange={(event) =>
                          void onRoleChange(member, event.target.value as WorkspaceRole)
                        }
                        aria-label={t('members.changeRole')}
                      >
                        <option value="admin">{t('members.roleAdmin')}</option>
                        <option value="member">{t('members.roleMember')}</option>
                      </select>
                    </td>
                    <td>{formatDate(member.joined_at)}</td>
                    <td>
                      <div className="member-row-actions">
                        <button
                          className="secondary-button"
                          type="button"
                          disabled={!esAdmin || busy || isSelf}
                          onClick={() => setRemoveTarget(member)}
                        >
                          <span>{t('members.remove')}</span>
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {!isLoading && listaFinal.length === 0 && !loadFailed ? (
        <p className="field-hint">{t('members.empty')}</p>
      ) : null}

      <p className="field-hint">{t('members.roleHint')}</p>

      <RemoveMemberDialog
        member={removeTarget}
        onCancel={() => setRemoveTarget(null)}
        onConfirm={() => void onRemoveConfirmed()}
        working={pendingId === removeTarget?.user_id}
      />
    </section>
  )
}
