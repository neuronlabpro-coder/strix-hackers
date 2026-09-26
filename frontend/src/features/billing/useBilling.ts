import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { getBillingSummary, getCreditLedger, createCheckoutSession } from '../../lib/adminApi'
import type { BillingSummary, CreditLedgerEntry } from '../../types/billing'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'

/**
 * Datos de la pantalla de facturación.
 *
 * ## Por qué el resumen y el historial van en un solo hook
 *
 * La pantalla pinta tarjetas KPI y una tabla de asientos a la vez, y **el saldo de las
 * tarjetas tiene que ser el mismo que el de la última fila de la tabla**. Si vinieran de
 * dos peticiones independientes, una segunda de desfase bastaría para que el último asiento
 * de la tabla no cuadrara con el saldo de arriba, y el operador leería eso como saldo
 * perdido.
 *
 * Las dos peticiones van en paralelo y en el mismo estado, así que comparten ciclo de
 * vida: si una falla, ambas se marcan y la vista lo dice una vez.
 *
 * ## Por qué el saldo del resumen manda sobre la suma del ledger
 *
 * El ledger es la fuente de verdad histórica, pero `credit_balance` es la caché que el
 * servicio mantiene **en la misma transacción** que escribe el asiento. Sumar la columna
 * en el cliente produciría la misma cifra con más trabajo y una oportunidad más de
 * equivocarse. El saldo se pinta como viene del servidor.
 */
export interface BillingState {
  summary: BillingSummary | null
  ledger: CreditLedgerEntry[]
  isLoading: boolean
  loadFailed: boolean
  /** Créditos de la compra que está redirigiendo al pago ahora mismo. */
  purchasingCredits: number | null
  refresh: () => void
  buyPack: (credits: number) => Promise<boolean>
}

/**
 * Resumen e historial, con la clave del sondeo a la que pertenecen.
 *
 * `key` es lo que permite derivar «estoy cargando» sin marcarlo a mano, igual que en
 * `useAdminPage`. Ver la nota de ese hook: la marca manual se queda en «cargando» para
 * siempre cuando un efecto se limpia antes de resolver.
 */
interface BillingSnapshot {
  key: string
  summary: BillingSummary | null
  ledger: CreditLedgerEntry[]
  failed: boolean
}

export function useBilling(): BillingState {
  const { t } = useTranslation('billing')
  const { token, selectedOrganizationId } = useAuth()
  const { notify } = useToast()
  const [snapshot, setSnapshot] = useState<BillingSnapshot | null>(null)
  const [purchasingCredits, setPurchasingCredits] = useState<number | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const isReady = Boolean(token && selectedOrganizationId)
  const requestKey = `${token}:${selectedOrganizationId}:${reloadToken}`

  useEffect(() => {
    if (token === null || selectedOrganizationId === null) {
      return
    }
    let isActive = true
    void Promise.all([
      getBillingSummary(token, selectedOrganizationId),
      getCreditLedger(token, selectedOrganizationId),
    ])
      .then(([summary, ledger]) => {
        if (isActive) {
          setSnapshot({ key: requestKey, summary, ledger, failed: false })
        }
      })
      .catch(() => {
        if (isActive) {
          // El fallo no borra el resumen anterior. Vaciarlo convertiría un problema de
          // conexión en "no tienes créditos", que es un saldo falso y cuesta un escaneo.
          setSnapshot((current) => ({
            key: requestKey,
            summary: current?.summary ?? null,
            ledger: current?.ledger ?? [],
            failed: true,
          }))
        }
      })
    return () => {
      isActive = false
    }
  }, [reloadToken, requestKey, selectedOrganizationId, token])

  const refresh = useCallback(() => {
    setReloadToken((current) => current + 1)
  }, [])

  const buyPack = useCallback(
    async (credits: number): Promise<boolean> => {
      if (token === null || selectedOrganizationId === null) {
        return false
      }
      setPurchasingCredits(credits)
      try {
        const session = await createCheckoutSession(
          token,
          selectedOrganizationId,
          credits,
          `${window.location.origin}/billing?status=paid`,
          `${window.location.origin}/billing?status=cancelled`,
        )
        // La redirección deja la página, así que `purchasingCredits` no se limpia a
        // propósito: si el pago se cancela y el navegador vuelve con el historial, el
        // botón sigue deshabilitado hasta que el componente se monte otra vez. Limpiarlo
        // antes de navegar mostraría el botón un instante y permitiría un doble clic.
        window.location.assign(session.url)
        return true
      } catch {
        notify('error', t('states.error'))
        setPurchasingCredits(null)
        return false
      }
    },
    [notify, selectedOrganizationId, t, token],
  )

  const isCurrent = snapshot !== null && snapshot.key === requestKey

  return {
    summary: isCurrent ? snapshot.summary : null,
    ledger: isCurrent ? snapshot.ledger : [],
    isLoading: isReady && !isCurrent,
    loadFailed: isCurrent && snapshot.failed,
    purchasingCredits,
    refresh,
    buyPack,
  }
}
