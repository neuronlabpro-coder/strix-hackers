import { useEffect, useState } from 'react'
import { CreditCard, RefreshCw, TrendingDown, Wallet } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'

import { deltaMagnitude, deltaTone, formatCredits, formatUsd } from '../../lib/format'
import type { CreditLedgerEntry } from '../../types/billing'
import { useAuth } from '../auth/useAuth'
import { useBilling } from './useBilling'

/**
 * Facturación y créditos del workspace.
 *
 * ## Por qué el catálogo de packs lo manda el servidor
 *
 * El backend valida la compra contra su propio catálogo y devuelve `422` para un paquete
 * que no exista. Si el panel llevara su lista, un desajuste entre las dos —un pack
 * retirado del servidor pero todavía en el cliente— sería un botón que el propio panel
 * ofreció y que el servidor rechazó. Aquí el panel pinta lo que le mandan, y cuando la
 * lista llega vacía lo dice y explica por qué, en vez de pintar un precio de su cuenta.
 */
export function BillingPage() {
  const { t } = useTranslation('billing')
  const { selectedOrganizationId } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const { summary, ledger, isLoading, loadFailed, purchasingCredits, refresh, buyPack } =
    useBilling()

  const paymentStatus = searchParams.get('status')

  /*
    El resultado del pago vuelve en la query string y se limpia de la URL al montarse.
    No se limpia antes de leerlo, ni se guarda en el estado: la única fuente es la URL,
    y dejarla puesta significa que recargar la página volvería a mostrar «pago
    completado» aunque el pago hubiera sido de hace una hora.
  */
  useEffect(() => {
    if (paymentStatus !== null) {
      setSearchParams({}, { replace: true })
    }
  }, [paymentStatus, setSearchParams])

  if (!selectedOrganizationId) {
    return (
      <section className="page-section">
        <div className="empty-card">
          <p>{t('states.error')}</p>
        </div>
      </section>
    )
  }

  if (loadFailed && summary === null) {
    return (
      <section className="page-section">
        <div className="empty-card">
          <h2>{t('states.error')}</h2>
          <button className="secondary-button" type="button" onClick={refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="page-section" aria-labelledby="billing-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('title')}</p>
          <h1 id="billing-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={refresh}
            disabled={isLoading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('states.retry')}</span>
          </button>
        </div>
      </div>

      {paymentStatus === 'paid' ? (
        <p className="banner-success" role="status">
          {t('payment.paid')}
        </p>
      ) : null}
      {paymentStatus === 'cancelled' ? (
        <p className="banner-warning" role="status">
          {t('payment.cancelled')}
        </p>
      ) : null}

      {summary === null ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : (
        <div className="metric-grid">
          <article className="metric-card metric-card-accent">
            <p className="eyebrow">
              <Wallet size={14} aria-hidden="true" /> {t('kpi.balance')}
            </p>
            <p className="metric-value">{formatCredits(summary.credit_balance)}</p>
            {/*
              La cifra en dólares va marcada como estimación y con el signo `≈`. El
              catálogo tiene descuento por volumen, así que no hay una paridad única: lo
              que se muestra es lo que costaría comprar esos créditos al precio unitario
              más barato. Sin el `≈` y sin el texto, el cliente leería un saldo en
              dólares y compararía su cuenta bancaria con él.
            */}
            <p className="metric-hint">
              {t('kpi.estimate', { usd: formatUsd(summary.credit_balance_usd) })}
            </p>
            <p className="metric-hint">{t('kpi.balanceHint')}</p>
          </article>
          <article className="metric-card">
            <p className="eyebrow">
              <TrendingDown size={14} aria-hidden="true" /> {t('kpi.spentThisMonth')}
            </p>
            <p className="metric-value">{formatCredits(summary.spent_this_month)}</p>
            <p className="metric-hint">
              {t('kpi.estimate', { usd: formatUsd(summary.spent_this_month_usd) })}
            </p>
            <p className="metric-hint">{t('kpi.spentThisMonthHint')}</p>
          </article>
          <article className="metric-card">
            <p className="eyebrow">
              <CreditCard size={14} aria-hidden="true" /> {t('kpi.purchasedThisMonth')}
            </p>
            <p className="metric-value">{formatCredits(summary.purchased_this_month)}</p>
            <p className="metric-hint">{t('kpi.purchasedThisMonthHint')}</p>
          </article>
        </div>
      )}

      <section className="content-card" aria-labelledby="billing-packs-title">
        <p className="eyebrow">{t('packs.title')}</p>
        <h2 id="billing-packs-title">{t('packs.caption')}</h2>
        {summary === null || summary.packs.length === 0 ? (
          <div className="empty-card">
            <p>{t('packs.empty')}</p>
            <p className="chart-empty">{t('packs.emptyCaption')}</p>
          </div>
        ) : (
          <div className="pack-grid">
            {summary.packs.map((pack) => (
              <article key={pack.credits} className="pack-card">
                <p className="pack-credits">
                  {t('packs.credits', { count: formatCredits(String(pack.credits)) })}
                </p>
                <p className="pack-price">{formatUsd(pack.amount_usd)}</p>
                <p className="pack-unit">
                  {t('packs.perCredit', { usd: formatUsd(pack.usd_per_credit) })}
                </p>
                <button
                  className="primary-button"
                  type="button"
                  disabled={purchasingCredits !== null}
                  onClick={() => void buyPack(pack.credits)}
                >
                  <span>
                    {purchasingCredits === pack.credits
                      ? t('packs.buying')
                      : t('packs.buy')}
                  </span>
                </button>
              </article>
            ))}
            {/*
              El pack a medida va **en la misma rejilla** que los otros, no debajo. Es la
              cuarta vía de comprar y el operador la recorre igual que las otras: ponerla
              aparte la convierte en un caso especial que parece menos/widgets.
            */}
            <CustomPackCard
              minimum={summary.custom_minimum}
              maximum={summary.custom_maximum}
              isBuying={purchasingCredits !== null}
              onBuy={buyPack}
            />
          </div>
        )}
        <p className="chart-empty">{t('packs.redirectNotice')}</p>
      </section>

      <section className="content-card" aria-labelledby="billing-ledger-title">
        <p className="eyebrow">{t('ledger.title')}</p>
        <h2 id="billing-ledger-title">{t('ledger.caption')}</h2>
        {ledger.length === 0 ? (
          <p className="chart-empty">{t('ledger.empty')}</p>
        ) : (
          <div className="table-wrapper">
            <table className="data-table">
              <caption className="visually-hidden">{t('ledger.title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('ledger.columns.date')}</th>
                  <th scope="col">{t('ledger.columns.reason')}</th>
                  <th scope="col">{t('ledger.columns.amount')}</th>
                  <th scope="col">{t('ledger.columns.balance')}</th>
                  <th scope="col">{t('ledger.columns.reference')}</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((entry) => (
                  <LedgerRow key={entry.id} entry={entry} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  )
}

/**
 * Campo libre del pack a medida.
 *
 * ## Por qué el precio se calcula en la tarjeta y no se pide al backend
 *
 * El catálogo está a la paridad declarada por el servidor, sin descuento por volumen, así
 * que el precio de cualquier cantidad es la cantidad. Mostrarlo mientras el usuario teclea
 * es lo que hace útil el campo: si el precio solo apareciera después de confirmar, la
 * tarjeta se parecería a un formulario de pedido en el que no se sabe cuánto va a costar.
 *
 * La paridad **se pide** en la respuesta del resumen y no se escribe aquí. Si mañana cambia,
 * esta tarjeta muestra el precio nuevo sin tocar una línea.
 * ## Por qué el botón se deshabilita en vez de dejar que el servidor rechace
 *
 * El mínimo cubre el coste fijo de la sesión de pago, y es una regla del servidor. La
 * tarjeta la refleja para que el usuario no descubra el límite Pulsando y leyendo un
 * `422`. El backend vuelve a validarla igual: el botón deshabilitado es cortesía, no la
 * garantía.
 */
function CustomPackCard({
  minimum,
  maximum,
  isBuying,
  onBuy,
}: {
  minimum: number
  maximum: number
  isBuying: boolean
  onBuy: (credits: number) => Promise<boolean>
}) {
  const { t } = useTranslation('billing')
  const [raw, setRaw] = useState('')

  // Solo dígitos. Se filtra en la entrada y no al enviar, para que el usuario pueda
  // escribir «250» sin que el campo le borre el carácter mientras teclea.
  const digits = raw.replace(/\D/g, '')
  const credits = digits === '' ? 0 : Number(digits)
  const isValid = credits >= minimum && credits <= maximum

  const price =
    isValid && digits !== '' ? formatUsd(String(credits)) : null

  return (
    <article className="pack-card">
      <p className="pack-credits">{t('packs.customTitle')}</p>
      {price === null ? (
        <>
          <p className="pack-price pack-price-placeholder">—</p>
          <p className="pack-unit">{t('packs.customCaption')}</p>
        </>
      ) : (
        <>
          <p className="pack-price">{price}</p>
          <p className="pack-unit">
            {t('packs.credits', { count: formatCredits(String(credits)) })}
          </p>
        </>
      )}
      <label className="form-field">
        <span className="">{t('packs.customLabel')}</span>
        <input
          className="text-input"
          value={raw}
          onChange={(event) => setRaw(event.target.value)}
          inputMode="numeric"
          placeholder={String(minimum)}
          maxLength={6}
        />
        <span className="form-hint">
          {isValid && digits !== '' ? t('packs.customHint') : t('packs.customInvalid', { min: minimum, max: maximum })}
        </span>
      </label>
      <button
        className="primary-button"
        type="button"
        disabled={!isValid || digits === '' || isBuying}
        onClick={() => void onBuy(credits)}
      >
        <span>
          {t('packs.customBuy', { count: formatCredits(String(credits)) })}
        </span>
      </button>
    </article>
  )
}

function LedgerRow({ entry }: { entry: CreditLedgerEntry }) {
  const { t } = useTranslation('billing')
  /*
    El signo se decide a partir de la cadena y el número se pinta **sin** el que trae. Un
    consumo es un delta negativo en la base, y `Intl` ya pondría el menos: anteponer otro
    daría «−−50,00». Por eso el color y el glifo los compone esta fila, y la magnitud sale
    de `deltaMagnitude`, que quita el signo del texto y deja que el formateador ponga los
    separadores.
  */
  const tone = deltaTone(entry.amount_delta)
  const magnitude = deltaMagnitude(entry.amount_delta)
  const sign = tone === 'negative' ? '−' : tone === 'positive' ? '+' : ''
  const reasonKey = `reason.${entry.reason}`

  return (
    <tr>
      <td>
        <span className="mono timestamp">{entry.created_at}</span>
      </td>
      <td>{t(reasonKey, { defaultValue: t('reason.unknown') })}</td>
      <td>
        <span className={`mono delta-${tone}`}>
          {sign}
          {magnitude}
        </span>
      </td>
      <td>
        <span className="mono">{formatCredits(entry.balance_after)}</span>
      </td>
      <td>
        <span className="mono table-secondary">
          {entry.reference_id ?? t('ledger.noReference')}
        </span>
      </td>
    </tr>
  )
}
