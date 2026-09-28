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

export interface VulnerabilityPage {
  items: VulnerabilityListItem[]
  total: number
  limit: number
  offset: number
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
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface PentestRunListItem extends PentestRun {
  findings: number
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
  markup_pct: string
  priority_order: number
  is_active: boolean
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
  markup_pct: string
  priority_order: number
  is_active: boolean
  use_case: LLMUseCase
}

export interface LLMModelUpdatePayload {
  display_name?: string
  base_cost_input_m?: string
  base_cost_output_m?: string
  markup_pct?: string
  priority_order?: number
  is_active?: boolean
  use_case?: LLMUseCase
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

export interface SeverityCount {
  severity: VulnerabilitySeverity
  total: number
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
  severity_distribution: SeverityCount[]
  repositories: DashboardRepository[]
  generated_at: string
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
  content: string
  created_at: string
  updated_at: string
}

export interface KnowledgeDocumentPage {
  documents: KnowledgeDocument[]
  total: number
}

export interface KnowledgeDocumentCreate {
  title: string
  doc_type: KnowledgeDocType
  /** El documento entero en formato OKF, con su frontmatter. */
  content: string
}
