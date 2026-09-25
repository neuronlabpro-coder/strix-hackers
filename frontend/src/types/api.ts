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
