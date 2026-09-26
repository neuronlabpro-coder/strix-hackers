/**
 * Tipos de la facturación del cliente.
 *
 * Todos los importes viajan como **cadena decimal**, no como número. El backend los
 * declara `Decimal` y Pydantic los serializa con cadena, y la razón de fondo es la misma
 * que en `credit_balance`: un número JSON es un binario en coma flotante donde `0.1` no
 * es exactamente `0.1`. Con `Numeric(18, 4)` los saldos tienen cuatro decimales, que es
 * justo donde la coma flotante empieza a mentir.
 */

export interface CreditPack {
  credits: number
  amount_usd: string
  /** Precio unitario ya calculado por el servidor, para que dos clientes no difieran. */
  usd_per_credit: string
}

export interface BillingSummary {
  credit_balance: string
  /**
   * **Estimación, no el valor del saldo.** Es lo que costaría comprar esta cantidad de
   * créditos al precio unitario más barato del catálogo.
   *
   * El catálogo aplica descuento por volumen —500 créditos a $0,038 y 15000 a $0,0266— así
   * que no hay una paridad crédito-dólar única. Convertir con una sola daría cifras que no
   * corresponden a ningún producto: 1000 créditos a la paridad del pack pequeño saldrían en
   * $26.315,79. El panel lo rotula como estimación y el usuario puede comprobarlo contra los
   * packs de la misma pantalla.
   */
  credit_balance_usd: string
  /** Precio unitario con el que se calculó la estimación, para que sea comprobable. */
  best_unit_price_usd: string
  spent_this_month: string
  purchased_this_month: string
  spent_this_month_usd: string
  period_start: string
  packs: CreditPack[]
}

export type LedgerReason =
  | 'SCAN_CONSUMPTION'
  | 'STRIPE_PURCHASE'
  | 'ADMIN_ADJUSTMENT'
  | 'SIGNUP_BONUS'
  | 'REFUND'

export interface CreditLedgerEntry {
  id: string
  amount_delta: string
  balance_after: string
  reason: LedgerReason | string
  reference_id: string | null
  /**
   * Fecha ya formateada por el backend, y por eso es `string` y no `string` ISO. El
   * esquema lo declara así a propósito: la hora de un asiento contable se muestra en la
   * zona del lector, no en UTC, y hacerlo en el servidor evita que dos pantallas del
   * mismo panel discrepen sobre cuándo ocurrió una compra.
   */
  created_at: string
}

export interface CheckoutSessionRequest {
  credits: number
  success_url: string
  cancel_url: string
}

export interface CheckoutSession {
  session_id: string
  /** Destino de Stripe Checkout. El panel redirige aquí y **no** muestra el precio. */
  url: string
  credits: number
  amount_usd: string
  currency: string
}
