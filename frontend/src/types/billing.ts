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
   * El catálogo parte de la paridad declarada en la configuración (`credits_per_usd`) y el
   * descuento por volumen solo se aplica a las **compras**, no al saldo que ya se tiene.
   * Por eso esto sigue siendo un número **comprobable**: 1000 créditos son 1000 dólares.
   *
   * La distinción es deliberada. Un saldo no se "revende", así que no puede llevar
   * descuento; los siguientes créditos que se compren, sí. Un cliente con 1000 créditos
   * que compra 1000 más los paga a $0,75, y los que ya tenía siguen valiendo $1,00. Si el
   * descuento se aplicara también al saldo, la cifra dejaría de ser un hecho y pasaría a
   * ser una estimación que habría que explicar en soporte.
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
  /**
   * La escalera de descuento por volumen, para el slider.
   *
   * Opcional a propósito. El backend la incluye siempre, pero declararla obligatoria
   * desactivó la única defensa que tenía este código: el compilador. Cuando la respuesta
   * llegaba sin ese bloque, el panel se quedó en negro con
   * `Cannot read properties of undefined (reading 'tiers')` y nada en la compilación lo
   * avisó, porque el tipo prometía algo que en ejecución no estaba.
   *
   * Con el `?` alrededor, TypeScript obliga a cada consumidor a decidir qué hacer cuando no
   * llega, que es la decisión que faltaba. Ver `deriveSpendRanges`.
   */
  volume?: VolumePricing
  /** La oferta de suscripción Pro, para el botón de suscripción. */
  subscription: SubscriptionOffer
}

export interface VolumeTier {
  /**
   * Primer crédito del tramo.
   *
   * La escalera la elige el **gasto**, así que este número no es el umbral: es cuántos
   * créditos entran por el primer dólar del tramo. El panel los usa para pintar la barra y
   * no para decidir el descuento —de eso se encarga el servidor—, así que que no coincidan
   * con `spend_min` es lo esperado.
   */
  minimum_credits: number
  /** Último crédito del tramo. El último tramo llega hasta `maximum_credits` del catálogo. */
  maximum_credits: number
  /** Descuento como fracción: `0.10` es el 10%. */
  discount: string
  /** Precio unitario de este tramo, ya calculado por el servidor. */
  usd_per_credit: string
}

export interface VolumePricing {
  tiers: VolumeTier[]
  minimum_credits: number
  maximum_credits: number
  /** Paridad sin descuento, para calcular el ahorro en la barra. */
  list_usd_per_credit: string
}

export interface SubscriptionOffer {
  plan_tier: string
  monthly_usd: string
  /** Si el workspace ya está en este plan, el botón se pinta activo en vez de ofrecerse. */
  is_current_plan: boolean
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

/**
 * Modo de pago de una sesión de Stripe Checkout.
 *
 * `credits` es una recarga puntual que acredita saldo. `subscription` contrata el plan Pro
 * y **no** acredita nada: cambia el plan. Son dos cosas distintas y mezclarlas haría que el
 * cliente esperara saldo por pagar la cuota.
 */
export type CheckoutMode = 'credits' | 'subscription'

export interface CheckoutSessionRequest {
  mode: CheckoutMode
  /** Obligatorio salvo en modo `subscription`. El servidor rechaza mandarlo en suscripción. */
  credits: number
  success_url: string
  cancel_url: string
}

export interface CheckoutSession {
  session_id: string
  /** Destino de Stripe Checkout. El panel redirige aquí y **no** muestra el precio. */
  url: string
  mode: CheckoutMode
  /** `0` en una suscripción, que no acredita saldo. */
  credits: number
  amount_usd: string
  currency: string
}
