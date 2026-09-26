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
   * Equivalente en dólares del saldo.
   *
   * El catálogo está a la paridad declarada en la configuración (`credits_per_usd`), sin
   * descuento por volumen, así que esto es un número **comprobable**: 1000 créditos son
   * 1000 dólares, y el cliente puede contrastarlo con los packs de la misma pantalla.
   *
   * Cuando el catálogo tenía descuento —$0,038 en el pack pequeño y $0,0266 en el grande—
   * no existía un precio por crédito y este campo tenía que ser una estimación al mejor
   * precio. Con descuento, 1000 créditos salían en $26.315,79: una cifra que no
   * correspondía a ningún producto y que el usuario leía como su saldo.
   */
  credit_balance_usd: string
  /** La paridad usada. Viaja para que el panel no la vuelva a derivar por su cuenta. */
  credits_per_usd: string
  spent_this_month: string
  purchased_this_month: string
  spent_this_month_usd: string
  period_start: string
  packs: CreditPack[]
  /** Mínimo del pack a medida, **en créditos**. El número que teclea el usuario. */
  custom_minimum: number
  /** Máximo del pack a medida, en créditos. */
  custom_maximum: number
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
