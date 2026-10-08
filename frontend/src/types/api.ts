export type OrganizationRole = 'admin' | 'member'
export type PlanTier = 'FREE' | 'PRO' | 'ENTERPRISE'

export interface Organization {
  id: string
  name: string
  slug: string
  plan_tier: PlanTier
  /**
   * Saldo como **cadena decimal**, no como número. El backend lo declara `Decimal` y
   * Pydantic lo serializa con cadena. Declararlo `number` funcionaba por casualidad
   * porque `Intl.NumberFormat` convierte la cadena, pero cualquier aritmética sobre el
   * valor habría suspendido la comprobación de tipos justo donde importa: en el dinero.
   */
  credit_balance: string
  role: OrganizationRole
  created_at: string
  updated_at: string
}

export interface UserProfile {
  id: string
  email: string
  full_name: string
  is_superuser: boolean
}

export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

export interface RegisterResponse {
  user: UserProfile
  organization: Organization
  verification_required: boolean
  verification_token: string | null
}

export interface RegisterPayload {
  email: string
  password: string
  full_name: string
  organization_name: string
}

export interface EmailVerificationResponse {
  verified: boolean
}

export interface EmailResendResponse {
  accepted: boolean
  verification_token: string | null
}

export interface InvitationAcceptResponse {
  organization_id: string
  role: OrganizationRole
  accepted: boolean
}

export interface LoginPayload {
  email: string
  password: string
}

export type GitProvider = 'GITHUB' | 'GITLAB' | 'BITBUCKET' | 'GITEA'

export type RepositoryMonitoringStatus = 'NOT_TESTED' | 'TESTED' | 'SCANNING'

export type VulnerabilitySeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO'

/**
 * Estado de remediación de un hallazgo.
 *
 * `REMEDIATION_PROPOSED` es un estado aparte y no una forma de `FIXED` porque un PR
 * abierto no arregla nada: puede cerrarse sin fusionarse, o fusionarse y no
 * resolver el problema. Marcar el hallazgo como cerrado al abrir la propuesta haría que
 * el panel afirmara algo que todavía no ha pasado.
 */
export type IssueStatus =
  | 'OPEN'
  | 'IN_PROGRESS'
  | 'REMEDIATION_PROPOSED'
  | 'FIXED'
  | 'SNOOZED'
  | 'IGNORED'

export type ScanMode = 'QUICK' | 'STANDARD' | 'DEEP'

export type ScanStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'TIMED_OUT'
  | 'ABORTED'

export type TargetType = 'REPOSITORY' | 'DOMAIN' | 'API_SPEC'

export interface VulnerabilityListItem {
  id: string
  run_id: string
  title: string
  severity: VulnerabilitySeverity
  cvss_score: number
  cve_id: string | null
  affected_target: string
  status: IssueStatus
  discovered_at: string
}

/**
 * Reparto de hallazgos por severidad dentro del conjunto que casaba con el filtro.
 *
 * ## Por qué no se deduce de `items`
 *
 * Porque `items` es **una página**. Con 4 000 hallazgos y `limit` 25, contar severidades sobre
 * la página da la distribución de las 25 filas más recientes, y pintarla junto al total engaña
 * con la misma naturalidad con la que engaña una etiqueta: nadie ve el `limit` al mirar una
 * barra. Los desgloses llegan del servidor con los mismos filtros que la lista, así que son
 * del conjunto entero.
 *
 * Siempre vienen las cinco severidades, aunque valgan cero: el gráfico necesita el cero para
 * dibujar la fila, y filtrar las vacías en el cliente es una decisión que puede desaparecer
 * sin que nadie lo note.
 */
export interface SeverityCount {
  severity: VulnerabilitySeverity
  total: number
}

/** Lo mismo para el estado de remediación, con los seis estados del ciclo de vida. */
export interface StatusCount {
  status: IssueStatus
  total: number
}

export interface VulnerabilityPage {
  items: VulnerabilityListItem[]
  total: number
  limit: number
  offset: number
  /** Reparto por severidad de **todo** lo que casaba con el filtro, no solo de la página. */
  severity_breakdown: SeverityCount[]
  /** Reparto por estado de remediación de todo lo que casaba con el filtro. */
  status_breakdown: StatusCount[]
}

/**
 * Un día de la serie de hallazgos, desglosado por severidad.
 *
 * La API la devuelve con los días vacíos a cero y rellenados, porque un gráfico con huecos lee
 * como si faltaran datos: «no escaneé el martes» y «no consulté el martes» se ven igual.
 */
export interface FindingsTrendPoint {
  /** Fecha en ISO `YYYY-MM-DD`, en UTC. */
  dia: string
  total: number
  /** Siempre las cinco claves: un cero explícito y una ausencia son cosas distintas. */
  por_severidad: Record<VulnerabilitySeverity, number>
}

export interface VulnerabilityDetail extends VulnerabilityListItem {
  description: string
  affected_line: string | null
  poc_reproduction_raw: string
  /** El diff que escribió el motor durante el escaneo. Es evidencia forense e inmutable. */
  autofix_patch_diff: string | null
  /** El diff que generó la plataforma a partir de esa evidencia. Es un borrador. */
  remediation_patch_diff: string | null
  /** Enlace a la pull request de la propuesta de remediación. */
  remediation_pr_url: string | null
  updated_at: string
}

export interface AutofixRequest {
  review_id: string
}

export interface AutofixResponse {
  autofix_url: string
}

/**
 * Lo que queda tras generar la corrección y abrir la pull request.
 *
 * Devuelve el `status` además de la URL porque el hallazgo ha cambiado de estado y quien
 * llama necesita saberlo **sin** una segunda petición. Solo la URL obligaría al panel a
 * recargar la ficha para descubrir que el botón de generar propuesta ya no tiene sentido,
 * y un panel sin recargar sigue ofreciendo una acción que ya está hecha.
 */
export interface RemediationResponse {
  vulnerability_id: string
  remediation_pr_url: string
  status: IssueStatus
}


/**
 * Una superficie que el motor **no** pudo verificar, y por qué.
 *
 * `kind` y `detail` son los valores del motor, sin traducir: el texto lo escribió el motor y
 * traducirlo sería reescribir su conclusión. Lo que se traduce son los rótulos de la tarjeta.
 */
export interface ScanCoverageGap {
  kind: string
  surface: string
  risk_area: string
  detail: string
}

/**
 * Un agente del motor y cómo terminó.
 *
 * `agent_name` y `status` son valores del motor, sin traducir: el texto lo escribió el motor y
 * traducirlo sería reescribir su conclusión. Lo que se traduce son los rótulos de la tarjeta.
 */
export interface ScanCoverageAgent {
  agent_name: string
  status: string
}

/**
 * Lo que el motor revisó y lo que no pudo revisar.
 *
 * Es lo que separa «se escaneó y no había nada» de «el motor no pudo mirar esto». Sin esto, los
 * dos se ven igual en el panel y un escaneo con huecos se lee como un escaneo limpio.
 *
 * `caveats` son los avisos que el motor escribió sobre su propia ejecución; `gaps` son
 * superficies concretas que quedaron fuera. No son la misma cosa y la tarjeta no los mezcla.
 */
export interface ScanCoverage {
  findings_filed: number
  surfaces_reviewed: number
  complete: boolean
  scan_status: string
  exit_reason: string | null
  caveats: string[]
  gaps: ScanCoverageGap[]
  /**
   * Los agentes del motor y cómo terminaron.
   *
   * No estaba en el tipo y por eso no se veían: el backend los guardaba en la base y Pydantic los
   * descartaba al serializar. El run de referencia tiene tres agentes `completed`.
   */
  agents: ScanCoverageAgent[]
}

/**
 * Lo que costó un escaneo, con los **dos** importes.
 *
 * `provider_usd` es lo que el proveedor cobró de verdad y `catalogue_usd` lo que la plataforma
 * cree que costó. No son lo mismo: medido sobre el run real de este proyecto, el proveedor cobró
 * 1,85 USD y el catálogo estima 6,80 USD para los mismos tokens.
 *
 * `diverges` no es un porcentaje sino un sí o un no, porque la pregunta que se responde desde el
 * panel es «¿esto es el mismo número?» y decidir cuál de los dos es el referencia no le
 * corresponde a la interfaz. `null` cuando falta alguno de los dos: no se puede afirmar
 * divergencia sobre un dato que no existe.
 */
export interface ScanCost {
  provider_usd: number | null
  catalogue_usd: number | null
  delta_usd: number | null
  provider_tokens: number | null
  diverges: boolean | null
}

export interface PentestRun {
  id: string
  organization_id: string
  target_type: TargetType
  target_identifier: string
  scan_mode: ScanMode
  status: ScanStatus
  container_id: string | null
  exit_code: string | null
  error_message: string | null
  /**
   * `null` cuando el run no tiene dato de cobertura.
   *
   * No es lo mismo que una cobertura con `surfaces_reviewed: 0`, que **sí** afirma que no se
   * revisó nada: `null` significa que no hay dato, y la tarjeta tiene que pintar las dos cosas
   * distinto porque una es un límite del escaneo y la otra no.
   */
  coverage: ScanCoverage | null
  /**
   * Lo que costó el escaneo.
   *
   * `null` cuando el run no tiene ninguno de los dos importes: es un run anterior a la columna o
   * un modo de ejecución que no publica consumo. No es lo mismo que `provider_usd: 0`, que sí
   * afirmaría que el proveedor no cobró nada.
   */
  cost: ScanCost | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface PentestRunListItem extends PentestRun {
  findings: number
}

/**
 * Una comprobación del entorno de ejecución.
 *
 * `clave` es lo que se muestra y lo que se traduce; `motivo` es el **código** del fallo, no
 * un texto, y es `null` cuando la comprobación pasa. El reparto entre backend y panel es
 * deliberado: el backend no escribe frases en la base de datos porque el panel se traduce, y
 * una frase en español guardada saldría en inglés en la interfaz inglesa.
 *
 * `bloquea` no es «pasa» con otro nombre: es si el fallo **impide lanzar un escaneo desde el
 * proceso que respondió**. El cerco de salida desactivado falla y no bloquea —el escaneo sale,
 * pero con los permisos de red del host—, y mezclar los dos casos en un solo color haría que
 * el operador no supiera si puede escanear o si puede escanear con seguridad.
 */
export interface SandboxReadinessCheck {
  clave: string
  pasa: boolean
  motivo: string | null
  bloquea: boolean
}

export interface SandboxReadiness {
  listo: boolean
  /**
   * ¿Puede este proceso lanzar un escaneo?
   *
   * No es lo mismo que `listo`. Y el nombre no dice «despliegue» a propósito: estas
   * comprobaciones corrieron en el proceso que atiende la petición, que en Dokploy no es el
   * que lanza los escaneos. Afirmar sobre el despliegue entero sería repetir, en la dirección
   * contraria, el error que este diagnóstico vino a corregir.
   */
  bloquea_escaneo: boolean
  comprobaciones: SandboxReadinessCheck[]
}

export interface PentestRunPage {
  items: PentestRunListItem[]
  total: number
  limit: number
  offset: number
}

export interface PentestCreatePayload {
  target_type: TargetType
  target_identifier: string
  scan_mode: ScanMode
}

export type PlanTierAdmin = 'FREE' | 'PRO' | 'ENTERPRISE'

export type LLMUseCase = 'ALL' | 'QUICK_SCAN' | 'DEEP_PENTEST' | 'AUTOFIX'

export interface LLMUsageMetrics {
  runs: number
  prompt_tokens: number
  completion_tokens: number
  base_cost_usd: string
  net_profit_usd: string
}

export interface LLMModelConfig {
  id: string
  model_id: string
  display_name: string
  base_cost_input_m: string
  base_cost_output_m: string
  cached_input_cost_m: string | null
  provider: string | null
  context_limit_tokens: number | null
  output_limit_tokens: number | null
  markup_pct: string
  priority_order: number
  is_active: boolean
  is_default: boolean
  use_case: LLMUseCase
  created_at: string
  updated_at: string
  usage: LLMUsageMetrics
}

export interface LLMModelPage {
  items: LLMModelConfig[]
  total: number
  limit: number
  offset: number
}

export interface LLMModelCreatePayload {
  model_id: string
  display_name: string
  base_cost_input_m: string
  base_cost_output_m: string
  cached_input_cost_m?: string | null
  provider?: string | null
  context_limit_tokens?: number | null
  output_limit_tokens?: number | null
  markup_pct: string
  priority_order: number
  is_active: boolean
  is_default: boolean
  use_case: LLMUseCase
}

export interface LLMModelUpdatePayload {
  display_name?: string
  base_cost_input_m?: string
  base_cost_output_m?: string
  cached_input_cost_m?: string | null
  provider?: string | null
  context_limit_tokens?: number | null
  output_limit_tokens?: number | null
  markup_pct?: string
  priority_order?: number
  is_active?: boolean
  is_default?: boolean
  use_case?: LLMUseCase
}

export type CostLimitScope = 'ORGANIZACION' | 'OPERACION' | 'PLAN' | 'GLOBAL'
export type CostLimitOperation = 'PENTEST_QUICK' | 'PENTEST_DEEP' | 'PR_REVIEW' | 'CHAT'

export interface CostLimitPolicy {
  id: string
  scope: CostLimitScope
  organization_id: string | null
  operation: CostLimitOperation | null
  plan_tier: 'FREE' | 'PRO' | 'ENTERPRISE' | null
  max_budget_usd: string | null
  max_turns: number | null
  valid_from: string
  valid_until: string | null
  created_at: string
}

export type CostLimitCreatePayload = Omit<CostLimitPolicy, 'id' | 'created_at' | 'valid_from'> & {
  valid_from?: string | null
}

export interface CostLimitPreview {
  max_budget_usd: string
  max_turns: number
  presupuesto_origen: { nivel: CostLimitScope; regla_id: string | null }
  turnos_origen: { nivel: CostLimitScope; regla_id: string | null }
}

export interface PentestProduct {
  id: string
  slug: string
  scan_mode: 'QUICK' | 'STANDARD' | 'DEEP' | null
  name_es: string
  name_en: string
  description_es: string
  description_en: string
  price_label_es: string
  price_label_en: string
  price_min_usd: string | null
  price_max_usd: string | null
  credits_required: string | null
  max_budget_usd: string | null
  max_turns: number | null
  features: string[]
  limits: Record<string, number>
  is_active: boolean
}

export type PentestProductUpdate = Partial<Omit<PentestProduct, 'id' | 'slug'>>

export interface EnterpriseAgreement {
  id: string
  organization_id: string
  price_monthly_usd: string | null
  seats: number | null
  included_credits: string
  discount_pct: string
  max_budget_usd: string | null
  max_turns: number | null
  features: Record<string, boolean>
  limits: Record<string, number>
  special_operations: string[]
  valid_from: string
  valid_until: string | null
  created_at: string
}

export type EnterpriseAgreementCreate = Omit<EnterpriseAgreement, 'id' | 'organization_id' | 'created_at' | 'valid_from'> & {
  valid_from?: string | null
}

export interface TriageResponse {
  id: string
  status: IssueStatus
  updated_at: string
  changed: boolean
}

export type CVESeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'

export interface CVESummary {
  cve_id: string
  severity: CVESeverity
  cvss_score: string
  epss_score: string | null
  is_kev: boolean
  published_at: string
  description: string
}

export type CVEDetail = CVESummary

export interface CVEPage {
  items: CVESummary[]
  total: number
  limit: number
  offset: number
}

export interface CVEYearsResponse {
  years: number[]
}

export interface CVESearchParams {
  query?: string
  severity?: CVESeverity
  is_kev_only?: boolean
  year?: number
  limit?: number
  offset?: number
}

export type AuditAction = 'STATUS_CHANGED' | 'REPOSITORY_POLICY_UPDATED' | 'REPOSITORY_CONNECTED' | 'REPOSITORY_DISCONNECTED'
export interface AuditLogEntry {
  id: string
  organization_id: string
  actor_user_id: string | null
  action: AuditAction
  entity_type: string
  entity_id: string
  from_state: string | null
  to_state: string | null
  created_at: string
}

export interface AuditLogPage {
  items: AuditLogEntry[]
  total: number
  limit: number
  offset: number
}

export interface PRReviewSummary {
  id: string
  repository_id: string
  repository_name: string
  run_id: string | null
  pr_number: number
  pr_title: string
  pr_author: string
  source_branch: string
  target_branch: string
  short_sha: string
  status: PRReviewStatus
  issues_caught_critical: number
  issues_caught_high: number
  merge_blocked: boolean
  finished_at: string | null
  created_at: string
}

export interface PRReviewPage {
  items: PRReviewSummary[]
  total: number
  limit: number
  offset: number
}

export interface PRReviewMetrics {
  total: number
  clean: number
  blocking: number
  issues_critical: number
  issues_high: number
}

export type PRReviewStatus = 'QUEUED' | 'SCANNING' | 'PASSED' | 'FAILED' | 'ERROR'

export type KnowledgeCategory =
  | 'INJECTION'
  | 'XSS'
  | 'AUTH'
  | 'CRYPTOGRAPHY'
  | 'SECRET_EXPOSURE'
  | 'DESERIALIZATION'
  | 'SSRF'
  | 'PATH_TRAVERSAL'
  | 'LOGIC'
  | 'DEPENDENCY'

export type KnowledgeSeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'

export interface KnowledgeSummary {
  id: string
  reference_code: string
  title: string
  category: KnowledgeCategory
  severity: KnowledgeSeverity
  risk_summary: string
  owasp_category: string
}

export interface KnowledgeDetail extends KnowledgeSummary {
  vulnerable_example: string
  secure_example: string
  mitigation: string
  updated_at: string
}

export interface KnowledgePage {
  items: KnowledgeSummary[]
  total: number
  limit: number
  offset: number
}

// --------------------------------------------------------------------------- //
// Chat con agentes (MENU-MAP §5).
// --------------------------------------------------------------------------- //

/**
 * Quién escribió un mensaje.
 *
 * `system` está aunque el panel nunca lo muestre: el backend lo filtra del historial porque las
 * instrucciones se montan aparte en cada prompt, y un tipo que no lo contemplara describiría mal
 * lo que el servidor puede devolver.
 */
export type ChatRole = 'user' | 'assistant' | 'system'

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  tokens_in: number
  tokens_out: number
  credits_cost: number
  model_id: string | null
  created_at: string
  /**
   * Que el cobro de este paso no se pudo verificar porque el proveedor no publicó su consumo.
   *
   * Distingue **cobro cero** de **cobro no medido**, y los dos muestran `credits_cost: 0`. Sin
   * este campo, un fallo de medición del proveedor se leería al usuario como un ahorro, que es
   * justo la lectura que hace que deje de fiarse del saldo.
   */
  consumo_no_verificable: boolean
}

export interface ChatConversation {
  id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface ChatConversationDetail {
  conversation: ChatConversation
  messages: ChatMessage[]
}

export interface ChatConversationPage {
  conversations: ChatConversation[]
  total: number
}

/** Un documento del workspace que se ha inyectado en el prompt de un paso. */
export interface ChatContextSource {
  id: string
  title: string
  doc_type: string
  description: string
  score: number
}

export interface ChatStepResponse {
  message: ChatMessage
  context_sources: ChatContextSource[]
}

/**
 * Lo que el usuario ha acotado para un paso.
 *
 * `credenciales_de_contexto` **no** transporta credenciales: solo declara que el texto pegado
 * lleva material sensible, para que el modelo no lo repita. Los valores viajan dentro de
 * `content`, que es un campo de texto libre y no invites a rellenar con un secreto con nombre
 * propio.
 */
export interface ChatContextOptions {
  credenciales_de_contexto: boolean
  dominios: string[]
  repositorios: string[]
}

export interface ChatMessageCreate {
  content: string
  context_options?: ChatContextOptions
}

export type OnboardingStepKey = 'connect_git' | 'import_repository' | 'run_first_scan'

export interface OnboardingStep {
  key: OnboardingStepKey
  completed: boolean
}

export interface OnboardingStatus {
  steps: OnboardingStep[]
  completed_steps: number
  total_steps: number
  is_complete: boolean
}

export type TenantLifecycle = 'active' | 'deleted' | 'deactivated'

export interface AdminOrganization {
  id: string
  name: string
  slug: string
  plan_tier: PlanTierAdmin
  /**
   * Saldo del tenant como **cadena decimal**, no como número.
   *
   * El backend lo declara `Decimal` y Pydantic lo serializa con cadena: `"42.5"`. Un
   * número JSON es un binario en coma flotante donde `0.1` no es exactamente `0.1`, así
   * que 42,10 créditos viajarían como `42.099999999999994` y el saldo que se ve en pantalla
   * no cuadraría con el del ledger. Para pintar hay que pasar por `formatCredits`.
   */
  credit_balance: string
  is_active: boolean
  deleted_at: string | null
  created_at: string
  updated_at: string
  member_count: number
}

export interface AdminOrganizationPage {
  items: AdminOrganization[]
  total: number
  limit: number
  offset: number
}

export interface AdminCreditGrantResult {
  organization_id: string
  granted: string
  balance_after: string
  ledger_entry_id: string
}

export type AdminMetricFormat = 'currency' | 'credits' | 'count'

export interface AdminMetric {
  key: string
  /** Cadena decimal, por la misma razón que `credit_balance`. */
  value: string
  format: AdminMetricFormat
  /** Clave de i18n del texto que explica qué mide esta métrica. */
  hint_key: string
}

export interface AdminOverview {
  metrics: AdminMetric[]
  infrastructure: InfrastructureHealth
  generated_at: string
}

export interface AdminUser {
  id: string
  email: string
  full_name: string
  is_superuser: boolean
  is_active: boolean
  email_verified: boolean
  created_at: string
  /**
   * Workspaces con su rol, en el formato `"Nombre (admin)"`.
   *
   * El rol viaja **con** el nombre porque un usuario puede ser `admin` en un workspace y
   * `member` en otro, y una columna con un único rol obligaría al operador a adivinar cuál.
   */
  roles: string[]
  organization_count: number
  /** Los nombres sin el rol, derivados en el servidor. */
  organizations: string[]
}

export interface AdminUserUpdate {
  /** Ausente = no tocar. `false` **sí** es un valor. */
  is_active?: boolean
  is_superuser?: boolean
}

export interface AdminUserPage {
  items: AdminUser[]
  total: number
  limit: number
  offset: number
}

/**
 * Un agente de escaneo, visto desde la consola de plataforma.
 *
 * ## Por que `organization_id` y `organization_name` van los dos
 *
 * Porque hacen falta los dos para cosas distintas. El identificador es lo que permite filtrar
 * y enlazar; el nombre es lo que un operador lee. Un UUID en una lista de cuarenta agentes no
 * dice nada de quien es cada uno y obliga a abrir la fila para averiguarlo.
 *
 * ## Por que `connected` lo decide el servidor
 *
 * Porque la ventana de "conectado" es un criterio, y un criterio que se evalua en dos sitios
 * con dos relojes es un criterio que un dia no coincide. Si el backend cambiara el plazo y el
 * frontend no, la insignia de la consola y el KPI del panel dirian cosas distintas del mismo
 * agente en la misma pantalla.
 */
export interface AdminAgent {
  id: string
  name: string
  organization_id: string
  organization_name: string
  token_prefix: string
  status: 'ACTIVE' | 'REVOKED'
  platform_hint: string | null
  agent_version: string | null
  enrolled_at: string
  last_seen_at: string | null
  connected: boolean
}

export interface AdminAgentPage {
  items: AdminAgent[]
  total: number
  limit: number
  offset: number
  /** La ventana de vida, en segundos, para poder decirla en la pantalla. */
  ventana_de_vida: number
}


export interface AdminSale {
  id: string
  event_id: string
  event_type: string
  session_id: string | null
  organization_id: string | null
  organization_name: string | null
  /** `null` cuando el evento no acreditó créditos. No es `0`: un `0` sería una venta. */
  credits_granted: string | null
  /**
   * Importe cobrado en centavos, o `null` cuando el evento no fue un cobro.
   *
   * `null` y no `0` porque Stripe no cobra cero: un cero en una columna de importes se
   * vería como una venta de $0,00. Los eventos anteriores a la columna que la añadió
   * muestran «no registrado», que es exactamente lo cierto.
   */
  amount_cents: number | null
  created_at: string
}

export interface AdminSalePage {
  items: AdminSale[]
  total: number
  limit: number
  offset: number
  /** Suma de los créditos **de la página**, no del histórico. */
  total_credits: string
  /** Suma de los importes conocidos **de la página**. */
  total_amount_cents: number
}

export interface AdminAuditEntry {
  id: string
  organization_id: string | null
  organization_name: string | null
  actor_user_id: string | null
  actor_email: string | null
  action: string
  entity_type: string
  entity_id: string
  from_state: string | null
  to_state: string | null
  created_at: string
}

export interface AdminAuditPage {
  items: AdminAuditEntry[]
  total: number
  limit: number
  offset: number
}

export interface DependencyHealth {
  status: 'online' | 'offline'
  latency_ms: number
}

export interface InfrastructureHealth {
  status: 'healthy' | 'degraded'
  database: DependencyHealth
  cache: DependencyHealth
  checked_at: string
}

export interface Repository {
  id: string
  provider: GitProvider
  remote_repo_id: string
  name: string
  full_name: string
  clone_url: string
  default_branch: string
  pr_reviews_enabled: boolean
  is_active: boolean
  webhook_registered: boolean
  created_at: string
  updated_at: string
}

export interface RepositoryPage {
  items: Repository[]
  total: number
  limit: number
  offset: number
}

export interface RemoteRepository {
  remote_repo_id: string
  name: string
  full_name: string
  clone_url: string
  default_branch: string
  is_private: boolean
  already_connected: boolean
}

export interface RemoteRepositoryPage {
  items: RemoteRepository[]
  total: number
  limit: number
  offset: number
  provider: GitProvider
  /**
   * La búsqueda que el servidor aplicó de verdad.
   *
   * ## Por qué es `undefined` y no `string | null` aquí
   *
   * Porque un backend más antiguo que el campo no manda nada, que es justo el caso que este
   * campo existe para detectar: si se declarara `string | null`, el `undefined` de una respuesta
   * vieja y el `null` de «no hubo búsqueda» se confundirían, y la comprobación de que el
   * servidor filtra no distinguiría «no filtró» de «me dio la espalda con el formato». Se
   * declara como opcional a propósito, y la comparación de abajo no la da por buena nunca.
   */
  busqueda_aplicada?: string | null
}

export interface RepositoryConnectPayload {
  provider: GitProvider
  remote_repo_id: string
  pr_reviews_enabled: boolean
}

export interface OAuthAuthorizationResponse {
  authorization_url: string
  expires_in: number
}

/**
 * Un permiso de la API pública, tal y como lo expone el backend.
 *
 * `isPrivileged` viene del catálogo, no se deduce en el cliente: la lista de permisos
 * que exigen confirmación la decide el backend, y recalcularla en el panel haría que
 * ambas cosas se desincronizasen en cuanto se añadiera un scope.
 */
export interface ApiScopeDefinition {
  scope: string
  action: string
  group: string
  label_key: string
  is_privileged: boolean
  /** Si el permiso entra en el juego por defecto del token nuevo. Lo declara el backend. */
  is_default: boolean
}

/** Los 47 scopes, agrupados por recurso y en el orden en que los muestra el panel. */
export interface ApiScopeGroup {
  group: string
  scopes: ApiScopeDefinition[]
}

export interface ApiScopeCatalog {
  groups: ApiScopeGroup[]
  total: number
}

/** De quién es el token. Los valores van en minúscula porque son los del API. */
export type ApiTokenType = 'personal' | 'service_key'

export interface ApiToken {
  id: string
  name: string
  /**
   * De quién es el token. El backend lo persiste y lo devuelve; el panel no lo deduce
   * de los scopes, porque un token de servicio con un solo scope de lectura sigue siendo
   * de servicio.
   */
  token_type: ApiTokenType
  token_prefix: string
  scopes: string[]
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
}

/**
 * Respuesta del alta, y la única vez que existe el secreto en el navegador.
 *
 * Es un tipo aparte de `ApiToken` a propósito: el campo `raw_token` no puede confundirse
 * con un dato de listado, donde no existe. Si se fusionaran, un `ApiToken` fingido en
 * cualquier parte del panel dejaria pensar que hay un secreto disponible.
 */
export interface ApiTokenCreated extends ApiToken {
  raw_token: string
}

export interface ApiTokenPage {
  items: ApiToken[]
  total: number
  include_revoked: boolean
}

export interface ApiTokenCreatePayload {
  name: string
  scopes: string[]
  /**
   * Días hasta la caducidad. El backend impone un techo de 365.
   *
   * `0` significa **sin caducidad**, y no "caduca hoy". El backend lo traduce a
   * `expires_at = null` con un validador que además rechaza el `0` con un `422` que no
   * menciona la caducidad, que es el resultado que tendría un selector HTML cuya primera
   * opción es "Sin expiración".
   */
  expires_in_days: number
  /** De quién es el token. El backend lo persiste y no lo deduce de los scopes. */
  token_type: ApiTokenType
}

/**
 * Un endpoint de webhook saliente.
 *
 * `is_auto_disabled` lo calcula el backend y llega aquí ya resuelto. No se deduce en el
 * cliente porque la diferencia importa: un endpoint pausado por el usuario y uno apagado
 * por diez fallos consecutivos requieren acciones distintas, y un panel que los muestra
 * igual deja al usuario sin manera de saber por qué dejó de llegarse.
 */
export interface WebhookEndpoint {
  id: string
  url: string
  description: string | null
  event_types: string[]
  is_active: boolean
  consecutive_failures: number
  created_at: string
  updated_at: string
  is_auto_disabled?: boolean
}

export interface WebhookCreated extends WebhookEndpoint {
  /** Secreto `whsec_` para firmar. Solo se devuelve aquí; no es recuperable. */
  signing_secret: string
}

export interface WebhookEventDefinition {
  event_type: string
  group: string
  action: string
  label_key: string
  description_key: string
}

export interface WebhookEventGroup {
  group: string
  events: WebhookEventDefinition[]
}

export interface WebhookEventCatalog {
  groups: WebhookEventGroup[]
  total: number
}

export interface WebhookDelivery {
  id: string
  endpoint_id: string
  event_type: string
  /** Cuerpo exacto que se envió. El backend lo devuelve para poder diagnosticar. */
  payload: Record<string, unknown>
  status_code: number | null
  response_body: string | null
  execution_time_ms: number | null
  attempt: number
  error_message: string | null
  delivered_at: string
}

export interface WebhookDeliveryPage {
  items: WebhookDelivery[]
  total: number
  limit: number
  offset: number
  response_truncated?: boolean
}

export interface WebhookPingResult {
  delivered: boolean
  status_code: number | null
  execution_time_ms: number | null
  error_message: string | null
  delivery_id: string
}

export interface RepositoryConnectResponse {
  repository: Repository
  webhook_registered: boolean
  created: boolean
}

export interface PersonalTokenConnectPayload {
  provider: GitProvider
  token: string
  name: string
}

/**
 * Identidad de la cuenta conectada, sin rastro del secreto.
 *
 * `account_login` es lo que el panel muestra antes de sincronizar nada: conectar el
 * repositorio equivocado es el error caro, y se detecta en cuanto se ve de quién es
 * la credencial en lugar de descubrirlo un escaneo después.
 */
export interface PersonalTokenConnectResponse {
  provider: GitProvider
  account_login: string
  account_display_name: string | null
  account_email: string | null
  replaced_existing: boolean
}

export interface RepositoryUpdatePayload {
  pr_reviews_enabled?: boolean
  default_branch?: string
  is_active?: boolean
}

export interface DashboardRepository {
  id: string
  provider: GitProvider
  name: string
  full_name: string
  is_active: boolean
  pr_reviews_enabled: boolean
  webhook_registered: boolean
  status: RepositoryMonitoringStatus
  open_vulnerabilities: number
  last_tested_at: string | null
}

export interface DashboardSummary {
  security_score: number
  open_issues: number
  total_issues: number
  fix_rate: number
  prs_reviewed: number
  prs_reviewed_total: number
  pentests_total: number
  repositories_monitored: number
  /** Hallazgos **abiertos** por severidad. Solo abiertos: los corregidos no son carga viva. */
  severity_distribution: SeverityCount[]
  /** Hallazgos por estado de remediación, incluidos los corregidos. */
  status_distribution: StatusCount[]
  /**
   * Producción diaria de hallazgos de la ventana, con los días vacíos a cero.
   *
   * La calcula el servidor porque la lista de issues está paginada: un «hallazgos por día»
   * armado en el navegador solo podría mirar una página de veinticinco filas, y daría dos
   * mentiras a la vez.
   */
  findings_trend: FindingsTrendPoint[]
  repositories: DashboardRepository[]
  generated_at: string
}

/** Lo que encontró una ejecución concreta, para el detalle del escaneo. */
export interface PentestFindingsBreakdown {
  total: number
  severity_distribution: SeverityCount[]
  status_distribution: StatusCount[]
}

// --------------------------------------------------------------------------- //
// Supply Chain: inventario de dependencias declaradas en los manifiestos
// (MENU-MAP 6.1).
// --------------------------------------------------------------------------- //

export type Ecosystem = 'NPM' | 'PYPI' | 'GO' | 'CARGO' | 'MAVEN' | 'COMPOSER' | 'OTHER'

/**
 * Una dependencia directa del manifiesto de un repositorio.
 *
 * ## Por que `has_vulnerabilities` es `boolean | null` y no `boolean`
 *
 * Porque son tres estados y con un booleano solo caben dos. `null` significa **no comprobado**,
 * que no es lo mismo que `false`: `false` es "se ha comprobado y esta limpio".
 *
 * El proyecto no tiene hoy una fuente de vulnerabilidades por paquete —`cve_records` guarda el
 * CVE, su severidad y una descripcion, pero no que paquetes afecta—, asi que todo lo indexado
 * esta en `null`. Si se tipara como `boolean`, el panel no podria distinguir "limpio" de "no lo
 * sabemos", y ensenaria un verde que nadie ha verificado. Ver el encabezado de
 * `backend/apps/supply_chain/models.py`.
 */
export interface SupplyChainPackage {
  id: string
  name: string
  /** Lo que el manifiesto **declara**, que puede ser un rango: `^4.17.21`, `>=2 <3`. */
  version: string
  ecosystem: Ecosystem
  license: string | null
  has_vulnerabilities: boolean | null
  cve_ids: string[]
  is_dev_dependency: boolean
  manifest_path: string | null
  first_seen_at: string
  last_seen_at: string
  repository_id: string
  repository_name: string
}

export interface SupplyChainPackagePage {
  items: SupplyChainPackage[]
  total: number
  limit: number
  offset: number
}

/** Los numeros de cabecera. `unchecked` es lo que no se sabe, y va aparte a proposito. */
export interface SupplyChainSummary {
  total_dependencies: number
  vulnerable: number
  clean: number
  unchecked: number
  by_ecosystem: Partial<Record<Ecosystem, number>>
  repositories_indexed: number
}

export interface SupplyChainIndexRequest {
  manifest_path: string
  content: string
}

export interface SupplyChainIndexResult {
  inserted: number
  updated: number
  discarded: number
  total: number
}

// --------------------------------------------------------------------------- //
// Documentos del workspace en formato OKF (MENU-MAP 7.1).
// --------------------------------------------------------------------------- //

/**
 * Que **es** este texto, para que el motor sepa si puede usarlo como contexto de analisis o
 * solo como referencia de lectura.
 *
 * No es una taxonomia de severidad ni de familia de fallo: es otra pregunta, y por eso no se
 * cruza con la del catalogo tecnico de debilidades CWE que vive en la misma pantalla.
 */
export type KnowledgeDocType =
  | 'DOCUMENTATION'
  | 'BUSINESS_RULE'
  | 'API_SPEC'
  | 'ARCHITECTURE'

export interface KnowledgeDocument {
  id: string
  title: string
  doc_type: KnowledgeDocType
  /**
   * Descripción del frontmatter, ya extraída por el servidor.
   *
   * **No** está en el listado: el listado devuelve la cabecera para no meter varios megabytes de
   * especificaciones de API en una tabla, y el cuerpo llega al pedir un documento concreto.
   *
   * Antes de que existiera, la tarjeta la sacaba de `content` del listado, que no viene, y la
   * pantalla reventaba con `undefined.replace(...)` en cuanto había un documento.
   */
  description: string
  /** Solo en el detalle de un documento concreto. Ausente en el listado. */
  content?: string
  created_at: string
  updated_at: string
}

export interface KnowledgeDocumentPage {
  documents: KnowledgeDocument[]
  total: number
  /**
   * Tamaño de página y desplazamiento **que aplicó el servidor**.
   *
   * No se calculan en el cliente: la barra de paginación los necesita para dibujar el resumen
   * y para decidir si hay páginas, y si los inventara cada pantalla lo haría de una forma que
   * el servidor no cumple.
   */
  limit: number
  offset: number
}

export interface KnowledgeDocumentCreate {
  title: string
  doc_type: KnowledgeDocType
  /** El documento entero en formato OKF, con su frontmatter. */
  content: string
}

// --------------------------------------------------------------------------- //
// Resultado de sincronizar un repositorio (Supply Chain).
// --------------------------------------------------------------------------- //

/**
 * Lo que se indexó al sincronizar.
 *
 * `manifests_missing` está en el tipo y no se deduce: un repositorio de Python sin `go.mod` es
 * una sincronización correcta con menos datos, y sin la lista de ausencias el panel no puede
 * distinguir "no tenía dependencias" de "no le preguntamos por el ecosistema equivocado".
 */
export interface SupplyChainSyncResult {
  manifests_found: string[]
  manifests_missing: string[]
  packages_inserted: number
  packages_updated: number
  packages_discarded: number
  /** Manifiestos que se descargaron y no se pudieron parsear. No son fallos de la sincronización. */
  errors: string[]
}

// --------------------------------------------------------------------------- //
// Precios de plataforma
// --------------------------------------------------------------------------- //
//
// ## Por que los importes viajan como cadena y no como numero
//
// Porque el backend los declara `Decimal` con ocho decimales, y un numero JSON es un binario
// en coma flotante donde `0.1` no es exactamente `0.1`. La paridad del credito es el caso
// peor: es un cociente, y a `0.01234567` la coma flotante le ha perdido el ultimo digito
// antes de llegar aqui. El panel los pinta; no los suma nunca en el cliente.
//
// ## Por que el descuento es una fraccion y no un porcentaje
//
// Porque el backend lo multiplica: el precio de un credito es `(1 - descuento) / paridad`. Un
// porcentaje obligaria a dividir por cien en el cliente, que es donde se equivocarian las dos
//implementaciones. `0.10` es el 10 %, y el `CHECK` de la base lo limita a `[0, 1]`.

/** Un paquete de creditos comercializable. */
export interface PlatformCreditPack {
  id: string
  credits: number
  amount_usd: string
  is_active: boolean
  display_order: number
}

/** Un tramo de la escalera de descuento por volumen. */
export interface PlatformVolumeTier {
  id: string
  spend_min_usd: string
  /** Descuento como fraccion: `0.10` es el 10 %. */
  discount: string
  is_active: boolean
  display_order: number
}

/** Los siete precios de la fila unica. */
export interface PlatformPricing {
  credits_per_usd: string
  scan_credit_cost: string
  quick_scan_credit_multiplier: string
  low_credit_balance_threshold: string
  custom_spend_minimum_usd: string
  custom_spend_maximum_usd: string
  pro_subscription_monthly_usd: string
  updated_at: string | null
}

/** El catalogo completo, en una sola lectura. */
export interface PlatformPricingDetail {
  precios: PlatformPricing
  packs: PlatformCreditPack[]
  tiers: PlatformVolumeTier[]
  credits_per_usd: string
  minimo_creditos_comerciales: number
  maximo_creditos_comerciales: number
  /** `false` cuando los precios vienen del codigo y no de la base. */
  desde_la_base: boolean
}

/**
 * Campos a cambiar. Todos opcionales, porque `null` significa «no tocar» y el backend
 * distingue el absences de un `false`, que sí es un cambio.
 */
export interface PlatformPricingUpdatePayload {
  credits_per_usd?: string
  scan_credit_cost?: string
  quick_scan_credit_multiplier?: string
  low_credit_balance_threshold?: string
  custom_spend_minimum_usd?: string
  custom_spend_maximum_usd?: string
  pro_subscription_monthly_usd?: string
  motivo: string
}

export interface PlatformCreditPackCreatePayload {
  credits: number
  amount_usd: string
  is_active?: boolean
  display_order?: number
  motivo: string
}

/** `credits` no se edita: es la identidad del pack en la traza y en las ventas. */
export interface PlatformCreditPackUpdatePayload {
  amount_usd?: string
  is_active?: boolean
  display_order?: number
  motivo: string
}

export interface PlatformVolumeTierCreatePayload {
  spend_min_usd: string
  discount: string
  is_active?: boolean
  display_order?: number
  motivo: string
}

/** `spend_min_usd` no se edita: moverlo sin mover sus vecinos abre huecos en la escalera. */
export interface PlatformVolumeTierUpdatePayload {
  discount?: string
  is_active?: boolean
  display_order?: number
  motivo: string
}

/** Un asiento del historico de precios. De solo lectura, por R4. */
export interface PlatformPriceChange {
  clave: string
  valor_anterior: string | null
  valor_nuevo: string | null
  actor_user_id: string | null
  motivo: string | null
  changed_at: string
}

export interface PlatformPriceChangePage {
  items: PlatformPriceChange[]
  total: number
  limit: number
  offset: number
}

// --------------------------------------------------------------------------- //
// Consola de operaciones
// --------------------------------------------------------------------------- //

/**
 * Un escaneo, con el nombre de su organizacion.
 *
 * ## Por que la duracion es `string` y no `number`
 *
 * Porque viene de un `Decimal` del backend y la regla del proyecto es que los decimales viajan
 * como cadena. Un `number` aqui haria que «0,1» y «0,10» se comparen igual en el cliente cuando
 * el backend los distingue, que es exactamente el fallo que ya se corrigio en la paridad.
 */
export interface ScanOperacion {
  id: string
  organization_id: string
  organizacion: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'TIMED_OUT' | 'ABORTED'
  scan_mode: string
  target_type: string
  target_identifier: string
  container_id: string | null
  cleanup_pending: boolean
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  /** Minutos que lleva vivo si sigue en curso. Es lo que hace ordenable la lista. */
  duracion_minutos: string
}

/**
 * Por que se cancela un escaneo. Decide si se devuelve el dinero.
 *
 * ## Por que el enum tiene que coincidir con el del backend
 *
 * Porque los motivos `INFRASTRUCTURE_*` **devuelven la reserva** y los demas cobran. Si aqui se
 * anvara el nombre de uno, la pantalla dejaria de avisar del dinero y el operador cancelaria sin
 * saber lo que va a pasar en la cuenta del cliente. El test `i18n` no cubre esto, asi que el
 * enganche es el propio compilador de TypeScript: si el backend anade un valor, este union deja
 * de compilar en cuanto se use.
 */
export type AbortReason =
  | 'CLIENT_CANCELLED'
  | 'INFRASTRUCTURE_STUCK'
  | 'INFRASTRUCTURE_FAILED'
  | 'INFRASTRUCTURE_ORPHANED'
  | 'DUPLICATE'
  | 'POLICY_VIOLATION'

export interface ScanOperacionPage {
  items: ScanOperacion[]
  total: number
  limit: number
  offset: number
}

export interface CancelarScanResponse {
  id: string
  status: string
  motivo: AbortReason
  tarea_revocada: boolean
  limpieza_pendiente: boolean
  /**
   * `null` significa «este motivo no devuelve». **No es `0`**: `0` seria «se cobro y no habia
   * nada que devolver», que es otra cosa y no debe pintarse igual.
   */
  devuelto_creditos: string | null
  nota: string
}

export interface LimpiarContenedorResponse {
  id: string
  contenedor: string
  contenedor_ya_no_existia: boolean
  limpieza_pendiente: boolean
  nota: string
}

export interface ReviewOperacion {
  id: string
  organization_id: string
  organizacion: string
  status: 'QUEUED' | 'SCANNING' | 'PASSED' | 'FAILED' | 'ERROR'
  pr_number: number
  pr_title: string | null
  source_branch: string | null
  target_branch: string | null
  issues_caught_critical: number
  issues_caught_high: number
  merge_blocked: boolean
  run_id: string | null
  created_at: string
  finished_at: string | null
}

export interface ReviewOperacionPage {
  items: ReviewOperacion[]
  total: number
  limit: number
  offset: number
}

export interface ContenedorOperacion {
  run_id: string
  organization_id: string
  organizacion: string
  container_id: string
  nombre_esperado: string
  status: string
  cleanup_pending: boolean
  started_at: string | null
}

export interface ContenedorPage {
  items: ContenedorOperacion[]
  total: number
  limit: number
  offset: number
}

export interface JobOperacion {
  id: string
  organization_id: string
  organizacion: string
  kind: string
  target: string
  status: 'QUEUED' | 'CLAIMED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  agent_id: string | null
  lease_expires_at: string | null
  attempt_count: number
  error_message: string | null
  created_at: string
}

/**
 * Un precio pactado con una organización concreta.
 *
 * ## Por qué `vigente` viene del servidor y no se deduce en el panel
 *
 * Porque la regla de cuál pactado manda depende del reloj, y el panel tendría que
 * reimplementarla para acertar. Marcándolo en la respuesta, la pantalla solo pinta. Y son
 * **tres** estados, no dos: vigente, futuro y caducado. Un `bool` de «caducado» no alcanza,
 * porque un pactado aún vigente y uno que empieza dentro de un mes se parecerían en la
 * lista y el comercial los leería igual.
 */
export interface OrganizationPriceOverride {
  id: string
  organization_id: string
  operacion: string
  /** Solo lo usa `CREDIT_PACK_AMOUNT`, y es la cantidad de créditos del pack. */
  alcance: string | null
  valor: string
  motivo: string
  valido_desde: string
  valido_hasta: string | null
  created_by: string | null
  created_at: string
  vigente: boolean
}

/** Lo que devuelve el `POST` al pactar: el pactado nuevo y a quién deja de sustituir. */
export interface PactadoPrecio extends OrganizationPriceOverride {
  sustituye_id: string | null
  valor_sustituido: string | null
  motivo_sustituido: string | null
}

export interface OrganizationPricingDetail {
  organization_id: string
  /** Lo que rigen a esta organización, con lo pactado encima de la plataforma. */
  precios: PlatformPricing
  /** El precio de plataforma sin pactar nada, para poder comparar. */
  precios_de_plataforma: PlatformPricing
  overrides: OrganizationPriceOverride[]
}

/** El cuerpo de un pactado. La organización va en la ruta, nunca aquí. */
export interface OrganizationPriceOverrideWrite {
  operacion: string
  alcance?: string | null
  valor: string
  motivo: string
  /** Vacío significa «y ya», que es lo que se quiere casi siempre. */
  valido_desde?: string | null
  valido_hasta?: string | null
}

export interface JobOperacionPage {
  items: JobOperacion[]
  total: number
  limit: number
  offset: number
}
