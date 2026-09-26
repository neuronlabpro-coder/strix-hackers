export type OrganizationRole = 'admin' | 'member'
export type PlanTier = 'FREE' | 'PRO' | 'ENTERPRISE'

export interface Organization {
  id: string
  name: string
  slug: string
  plan_tier: PlanTier
  credit_balance: number
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

export type IssueStatus = 'OPEN' | 'IN_PROGRESS' | 'FIXED' | 'SNOOZED' | 'IGNORED'

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
  autofix_patch_diff: string | null
  updated_at: string
}

export interface AutofixRequest {
  review_id: string
}

export interface AutofixResponse {
  autofix_url: string
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

export interface AdminOrganization {
  id: string
  name: string
  slug: string
  plan_tier: PlanTierAdmin
  credit_balance: number
  created_at: string
  updated_at: string
}

export interface AdminOrganizationPage {
  items: AdminOrganization[]
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
