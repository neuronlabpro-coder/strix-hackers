import { Coins, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { formatCredits } from '../../lib/format'
import { useAuth } from '../auth/useAuth'
import { useBilling } from './useBilling'

/**
 * Píldora de saldo en la barra superior.
 *
 * ## Por qué pide sus propios datos y no los de la pantalla de facturación
 *
 * Son dos vistas distintas con ciclos de vida distintos: esta se monta en todas las
 * pantallas del panel y esa solo en `/billing`. Compartir un contexto obligaría a la
 * píldora a depender de que alguien haya abierto facturación, y el saldo —que es lo que
 * decide si el usuario puede lanzar un escaneo— aparecería vacío en las pantallas donde
 * no se abrió.
 *
 * ## Por qué no se esconde cuando falla
 *
 * Un saldo que desaparece es un saldo que parece cero, y un cero en la barra de
 * herramientas significa "no puedes comprar escaneos". Ante un fallo de red se enseña el
 * estado de carga indefinido con la acción disponible: es un aviso de que el dato no está,
 * no una afirmación de que no hay créditos.
 *
 * ## Por qué el botón lleva a la pantalla y no abre un modal
 *
 * Recargar es una decisión de cuatro pasos —ver saldo, ver consumo, elegir pack, pagar— y
 * meterla en un modal desde la barra superior la reduce a la que se puede hacer sin
 * contexto. El enlace lleva a la vista completa, que es donde están los packs.
 */
export function CreditBalancePill() {
  const { t } = useTranslation('billing')
  const { selectedOrganizationId } = useAuth()
  const { summary, loadFailed } = useBilling()

  if (!selectedOrganizationId) {
    return null
  }

  return (
    <div className="credit-pill">
      <Link className="credit-pill-balance" to="/billing">
        <Coins size={16} aria-hidden="true" />
        <span className="visually-hidden">{t('topbar.balance')}</span>
        {summary === null ? (
          <span className="credit-pill-pending" aria-hidden="true">
            {loadFailed ? '—' : '···'}
          </span>
        ) : (
          <span className="mono">
            {t('packs.credits', { count: formatCredits(summary.credit_balance) })}
          </span>
        )}
      </Link>
      {/*
        Recargar es un enlace y no un botón: lleva a `/billing`, donde están los packs. Un
        botón que recargara el saldo dejaría al usuario mirando la misma barra superior
        con un número nuevo y sin forma de comprar nada.
      */}
      <Link className="credit-pill-topup" to="/billing">
        <Plus size={15} aria-hidden="true" />
        <span>{t('topbar.topUp')}</span>
      </Link>
    </div>
  )
}
