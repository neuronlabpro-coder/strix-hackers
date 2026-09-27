import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  createCheckoutSession,
  createSubscriptionCheckout,
  getBillingSummary,
  getCreditLedger,
} from '../../lib/adminApi'
import { formatCredits, formatDateTime, formatUsd } from '../../lib/format'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { BillingSummary, CreditLedgerEntry } from '../../types/billing'
import { VolumeTierBar } from '../billing/VolumeTierBar'
import { creditsForSpend, deriveSpendRanges, tierIndexFor } from '../billing/volumePricing'

/** Lo que el endpoint de resumen y el de movimientos devuelven, pedidos a la vez. */
interface BillingPayload {
  summary: BillingSummary
  ledger: CreditLedgerEntry[]
}

/**
 * Vista *Facturación* de Ajustes: suscripción, packs, slider de volumen y movimientos.
 *
 * ## Por qué el slider manda **créditos** y no un importe
 *
 * Es la decisión de seguridad de toda la pantalla. El cuerpo de la petición lleva `credits`
 * y el importe lo calcula `price_for_credits` en el servidor. Si el panel mandara un
 * importe, el endpoint estaría aceptando que quien llama decida cuánto paga por cuánto —y
 * el backend rechaza tanto `amount` como `discount` con un `422`, así que tampoco se puede
 * intentar—.
 *
 * El slider mueve **dólares** porque es lo que el cliente quiere decidir, y traduce a
 * créditos en cada movimiento del control para poder mandar la cantidad. La traducción se
 * calcula en el panel para que el control responda al instante, y la vuelve a calcular el
 * servidor antes de cobrar.
 *
 * ## Por qué el input numérico y el slider comparten estado
 *
 * Es lo que hace que un control de volumen sirva. Con un solo `input type="number"` hay que
 * teclear cada prueba. Con un solo `range` no se puede escribir un importe exacto, que es la
 * mitad de lo que un cliente que ya sabe lo que quiere va a hacer.
 *
 * La sincronización es por estado —los dos leen el mismo `spend` y los dos escriben ahí— y
 * no por eventos. Una sincronización por `oninput` con vuelta al otro control se
 * realimenta y hace que el cursor salte al cambiar de unidades.
 */
export function SettingsBillingPage() {
  const { t } = useTranslation('billing')
  const { token, organizations, selectedOrganizationId } = useAuth()
  const { notify } = useToast()

  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? null
  const organizationId = selectedOrganizationId

  const [spend, setSpend] = useState<number | null>(null)
  const [spendText, setSpendText] = useState('')
  const [buying, setBuying] = useState(false)
  const [subscribing, setSubscribing] = useState(false)

  const fetcher = useCallback(
    async (key: string): Promise<BillingPayload> => {
      const activeToken = token
      if (activeToken === null) {
        throw new Error('sin token')
      }
      const [summary, ledger] = await Promise.all([
        getBillingSummary(activeToken, key),
        getCreditLedger(activeToken, key),
      ])
      return { summary, ledger }
    },
    [token],
  )

  const { data, isLoading, loadFailed, reload } = useAsyncResource<BillingPayload>(
    fetcher,
    organizationId,
  )

  const summary = data?.summary ?? null
  const ledger = useMemo(() => data?.ledger ?? [], [data])

  const ranges = useMemo(
    () => (summary === null ? [] : deriveSpendRanges(summary.volume)),
    [summary],
  )
  const minSpend = ranges[0]?.min ?? 0
  const maxSpend = ranges[ranges.length - 1]?.max ?? 0

  /**
   * El gasto arranca en el mínimo **cuando llega el resumen**, y no en un `useEffect`.
   *
   * `spend` es `null` hasta que hay escalera, y los dos valores de la interfaz leen de ahí.
   * Antes se usaba un `useEffect` que escribía estado al cambiar `minSpend`, lo que
   * arrancaba un render extra en cada respuesta y es justo lo que el lint del proyecto
   * prohíbe. Con `null` no hay efecto: el primer render pinta controles deshabilitados con
   * el valor por defecto y el siguiente pinta el mínimo real del catálogo.
   *
   * El mínimo no se escribe como constante sino que sale de la escalera del servidor, y por
   * eso no hay ningún número de catálogo en este fichero.
   */
  const spendEfectivo = spend ?? minSpend

  // `volume` es opcional desde que el tipo lo declara así, y la razón es que **se ha visto
  // faltar**: la respuesta llegó sin el bloque y la pantalla se quedó en negro con
  // `Cannot read properties of undefined (reading 'tiers')`. Guardar aquí es lo que impide que
  // vuelva a pasar, y ahora el compilador obliga a que esteguard exista en vez de confiar en
  // que alguien se acuerde.
  const volume = summary?.volume

  const tierIndex = volume === undefined ? -1 : tierIndexFor(spendEfectivo, ranges)
  const tier = volume === undefined || tierIndex < 0 ? null : volume.tiers[tierIndex]
  const credits = creditsForSpend(spendEfectivo, volume)
  const discountRate = tier === null ? 0 : Number(tier.discount)
  const unitPrice = tier === null ? 0 : Number(tier.usd_per_credit)
  const listUnit = volume === undefined ? 0 : Number(volume.list_usd_per_credit)
  const savings = spendEfectivo * discountRate
  const porDebajoDelMinimo = credits === 0
  const hayDescuento = discountRate > 0

  function onSliderChange(value: number) {
    setSpend(value)
    setSpendText(String(value))
  }

  function onTextChange(raw: string) {
    setSpendText(raw)
    const parsed = Number(raw)
    if (Number.isFinite(parsed)) {
      setSpend(Math.min(Math.max(parsed, minSpend), maxSpend))
    }
  }

  const successUrl = `${window.location.origin}/settings/billing?payment=success`
  const cancelUrl = `${window.location.origin}/settings/billing?payment=cancelled`

  async function onBuy(quantity: number) {
    if (!token || !organizationId || buying || quantity <= 0) return
    setBuying(true)
    try {
      const session = await createCheckoutSession(
        token,
        organizationId,
        quantity,
        successUrl,
        cancelUrl,
      )
      window.location.assign(session.url)
    } catch {
      setBuying(false)
      notify('error', t('states.purchaseError'))
    }
  }

  async function onSubscribe() {
    if (!token || !organizationId || subscribing) return
    setSubscribing(true)
    try {
      const session = await createSubscriptionCheckout(token, organizationId, successUrl, cancelUrl)
      window.location.assign(session.url)
    } catch {
      setSubscribing(false)
      notify('error', t('plan.subscriptionErrors'))
    }
  }

  if (loadFailed && summary === null) {
    return (
      <section className="settings-section">
        <div className="empty-card">
          <h2>{t('states.error')}</h2>
          <button className="secondary-button" type="button" onClick={reload}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      </section>
    )
  }

  if (summary === null) {
    return <p className="field-hint">{isLoading ? t('states.loading') : ''}</p>
  }

  return (
    <section className="settings-section">
      <header className="settings-section-heading">
        <h1>{t('title')}</h1>
        <p>{t('description')}</p>
      </header>

      <div className="kpi-grid">
        <div className="kpi-card">
          <p className="kpi-label">{t('kpi.balance')}</p>
          <p className="kpi-value">{formatCredits(summary.credit_balance)}</p>
          <p className="kpi-hint">{t('kpi.balanceHint')}</p>
          <p className="kpi-note">
            {t('kpi.equivalent', { usd: formatUsd(summary.credit_balance_usd) })}
          </p>
        </div>
        <div className="kpi-card">
          <p className="kpi-label">{t('kpi.spentThisMonth')}</p>
          <p className="kpi-value">{formatCredits(summary.spent_this_month)}</p>
          <p className="kpi-hint">{t('kpi.spentThisMonthHint')}</p>
        </div>
        <div className="kpi-card">
          <p className="kpi-label">{t('kpi.purchasedThisMonth')}</p>
          <p className="kpi-value">{formatCredits(summary.purchased_this_month)}</p>
          <p className="kpi-hint">{t('kpi.purchasedThisMonthHint')}</p>
        </div>
        <div className="kpi-card">
          <p className="kpi-label">{t('plan.current')}</p>
          <p className="kpi-value">{organization?.plan_tier ?? 'FREE'}</p>
          <p className="kpi-hint">
            {summary.subscription.is_current_plan ? t('plan.currentPlanNotice') : ''}
          </p>
        </div>
      </div>

      {/*
        La tarjeta de suscripción va **antes** que los packs.

        Un plan cambia lo que puedes hacer; los créditos son con qué pagas por ello. El
        orden inverso hace que quien va a contratar Pro tenga que pasar por alto una tabla
        de precios que ya no le va a aplicar.
      */}
      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('plan.subscriptionTitle')}</h2>
          <p>{t('plan.subscriptionCaption')}</p>
        </div>
        <p className="volume-unit-price">
          <strong>{t('plan.monthly', { usd: formatUsd(summary.subscription.monthly_usd) })}</strong>
        </p>
        <ul className="danger-list">
          <li>{t('plan.proBenefits.urgent')}</li>
          <li>{t('plan.proBenefits.volume')}</li>
          <li>{t('plan.proBenefits.history')}</li>
        </ul>
        <div className="settings-form-actions">
          <button
            className="primary-button"
            type="button"
            onClick={() => void onSubscribe()}
            disabled={summary.subscription.is_current_plan || subscribing}
          >
            <span>
              {summary.subscription.is_current_plan
                ? t('plan.currentPlanNotice')
                : subscribing
                  ? t('plan.subscribing')
                  : t('plan.subscribe')}
            </span>
          </button>
        </div>
        {summary.subscription.is_current_plan ? (
          <p className="field-hint">{t('plan.upgrade')}</p>
        ) : null}
      </div>

      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('packs.title')}</h2>
          <p>{t('packs.caption')}</p>
        </div>
        <div className="pack-grid">
          {summary.packs.map((pack) => (
            <div key={pack.credits} className="pack-card">
              <p className="pack-credits">
                {t('packs.credits', { count: formatCredits(pack.credits) })}
              </p>
              <p className="pack-price">{formatUsd(pack.amount_usd)}</p>
              <p className="pack-unit">{t('packs.perCredit', { usd: formatUsd(pack.usd_per_credit) })}</p>
              <button
                className="secondary-button"
                type="button"
                disabled={buying}
                onClick={() => void onBuy(pack.credits)}
              >
                <span>{buying ? t('packs.buying') : t('packs.buy')}</span>
              </button>
            </div>
          ))}
        </div>
        {summary.packs.length === 0 ? <p className="field-hint">{t('packs.emptyCaption')}</p> : null}
      </div>

      {/*
        El slider va después de los packs y no antes por una razón concreta: los packs son la
        compra que ya entendía quien llega aquí, y el slider es para quien quiere otra
        cantidad. Al revés, el primer control que se ve sería el más complejo.
      */}
      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('volume.title')}</h2>
          <p>{t('volume.caption')}</p>
        </div>

        {/*
          La barra se oculta entera si no hay escalera. Dibujarla vacía deja un hueco en la
          pantalla sin explicar por qué, y eso se lee como un fallo de la página. Ocultarla
          deja claro que aún no hay datos, que es la verdad.
        */}
        {volume === undefined ? null : (
          <VolumeTierBar pricing={volume} ranges={ranges} spend={spendEfectivo} />
        )}

        <div className="volume-controls">
          <div className="field">
            <label htmlFor="volume-slider">{t('volume.sliderLabel')}</label>
            <input
              id="volume-slider"
              type="range"
              min={minSpend}
              max={maxSpend}
              step={1}
              value={spendEfectivo}
              onChange={(event) => onSliderChange(Number(event.target.value))}
              aria-describedby="volume-amount-hint"
            />
          </div>
          <div className="field">
            <label htmlFor="volume-amount">{t('volume.amountLabel')}</label>
            <input
              id="volume-amount"
              type="number"
              inputMode="numeric"
              min={minSpend}
              max={maxSpend}
              step={1}
              value={spendText === '' ? String(spendEfectivo) : spendText}
              onChange={(event) => onTextChange(event.target.value)}
              onBlur={() => setSpendText(String(spendEfectivo))}
              aria-describedby="volume-amount-hint"
            />
            <p className="field-hint" id="volume-amount-hint">
              {t('volume.amountHint', { min: formatUsd(minSpend), max: formatUsd(maxSpend) })}
            </p>
          </div>
        </div>

        <dl className="volume-summary">
          <div className="volume-summary-item">
            <dt>{t('volume.unitPriceTitle')}</dt>
            <dd className={hayDescuento ? 'volume-price-discounted' : ''}>{formatUsd(unitPrice)}</dd>
          </div>
          <div className="volume-summary-item">
            <dt>{t('volume.listPriceTitle')}</dt>
            <dd>{formatUsd(listUnit)}</dd>
          </div>
          <div className="volume-summary-item">
            <dt>{t('volume.discountLabel')}</dt>
            <dd>
              {hayDescuento
                ? t('volume.tier', { pct: (discountRate * 100).toFixed(0) })
                : t('volume.noSavings')}
            </dd>
          </div>
          <div className="volume-summary-item volume-summary-item-wide">
            <dt>{t('volume.creditsTitle')}</dt>
            <dd className="volume-credits">
              {porDebajoDelMinimo
                ? t('volume.belowMinimum', { min: formatUsd(minSpend) })
                : formatCredits(credits)}
            </dd>
          </div>
          {hayDescuento ? (
            <div className="volume-summary-item volume-summary-item-wide">
              <dt>{t('volume.savingsTitle')}</dt>
              <dd className="volume-savings">
                {t('volume.savings', { usd: formatUsd(savings) })}
                <span className="volume-savings-pct">
                  {t('volume.savingsPct', { pct: (discountRate * 100).toFixed(0) })}
                </span>
              </dd>
            </div>
          ) : null}
        </dl>

        {porDebajoDelMinimo ? (
          <p className="field-error">{t('volume.belowMinimum', { min: formatUsd(minSpend) })}</p>
        ) : (
          <p className="field-hint">{t('volume.creditsNote')}</p>
        )}
        <p className="field-hint">{t('volume.savingsExplainer')}</p>

        <div className="settings-form-actions">
          <button
            className="primary-button"
            type="button"
            onClick={() => void onBuy(credits)}
            disabled={porDebajoDelMinimo || buying}
          >
            <span>
              {porDebajoDelMinimo
                ? t('volume.belowMinimum', { min: formatUsd(minSpend) })
                : buying
                  ? t('volume.buying')
                  : t('volume.buy', { usd: formatUsd(spendEfectivo) })}
            </span>
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="settings-section-heading">
          <h2>{t('ledger.title')}</h2>
          <p>{t('ledger.caption')}</p>
        </div>
        {ledger.length === 0 ? (
          <p className="field-hint">{t('ledger.empty')}</p>
        ) : (
          <div className="table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('ledger.columns.date')}</th>
                  <th scope="col">{t('ledger.columns.reason')}</th>
                  <th scope="col">{t('ledger.columns.amount')}</th>
                  <th scope="col">{t('ledger.columns.balance')}</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((entry) => (
                  <tr key={entry.id}>
                    <td>{formatDateTime(entry.created_at)}</td>
                    <td>{t(`reason.${entry.reason}`, { defaultValue: t('reason.unknown') })}</td>
                    <td>{formatCredits(entry.amount_delta)}</td>
                    <td>{formatCredits(entry.balance_after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
