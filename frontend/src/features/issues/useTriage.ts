import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { triageVulnerability } from '../../lib/api'
import type { IssueStatus, VulnerabilityListItem } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'

export interface TriageResult {
  ok: boolean
  changed: boolean
}

/**
 * Triaje de un hallazgo con actualización optimista y reversión.
 *
 * R4 impide que el cliente altere las evidencias, así que la mutación solo
 * envía `status`. Si la API rechaza el cambio, la vista devuelve la tarjeta a su
 * columna original y avisa por notificación: un tablero que muestra un estado que
 * el servidor no tiene sería peor que uno que no permitiese mover.
 */
export function useTriage() {
  const { t } = useTranslation('triage')
  const { token, selectedOrganizationId } = useAuth()
  const { notify } = useToast()
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set())

  const triage = useCallback(
    async (
      finding: VulnerabilityListItem,
      nextStatus: IssueStatus,
    ): Promise<TriageResult> => {
      if (!token || !selectedOrganizationId) {
        return { ok: false, changed: false }
      }
      if (finding.status === nextStatus) {
        return { ok: true, changed: false }
      }
      setPendingIds((current) => new Set(current).add(finding.id))
      try {
        const response = await triageVulnerability(
          token,
          selectedOrganizationId,
          finding.id,
          nextStatus,
        )
        if (response.changed) {
          notify('success', t('saved'))
        }
        return { ok: true, changed: response.changed }
      } catch {
        notify('error', t('error'))
        return { ok: false, changed: false }
      } finally {
        setPendingIds((current) => {
          const next = new Set(current)
          next.delete(finding.id)
          return next
        })
      }
    },
    [notify, selectedOrganizationId, t, token],
  )

  const isPending = useCallback(
    (findingId: string) => pendingIds.has(findingId),
    [pendingIds],
  )

  return { triage, isPending }
}
