import { API_BASE_URL } from '../config'
import type {
  AdminOrganizationPage,
  AuditLogPage,
  AutofixRequest,
  AutofixResponse,
  CVEDetail,
  CVEPage,
  CVESearchParams,
  CVEYearsResponse,
  DashboardSummary,
  EmailResendResponse,
  EmailVerificationResponse,
  GitProvider,
  InfrastructureHealth,
  InvitationAcceptResponse,
  IssueStatus,
  KnowledgeCategory,
  KnowledgeDetail,
  KnowledgePage,
  KnowledgeSeverity,
  LLMModelConfig,
  LLMModelCreatePayload,
  LLMModelPage,
  LLMModelUpdatePayload,
  LoginPayload,
  OAuthAuthorizationResponse,
  OnboardingStatus,
  Organization,
  PentestCreatePayload,
  PentestRun,
  PentestRunPage,
  PRReviewMetrics,
  PRReviewPage,
  PRReviewStatus,
  RegisterPayload,
  RegisterResponse,
  RemoteRepositoryPage,
  Repository,
  RepositoryConnectPayload,
  RepositoryConnectResponse,
  RepositoryUpdatePayload,
  ScanMode,
  ScanStatus,
  TargetType,
  TokenResponse,
  TriageResponse,
  UserProfile,
  VulnerabilityDetail,
  VulnerabilityPage,
  VulnerabilitySeverity,
} from '../types/api'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number) {
    super('api_request_failed')
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
  organizationId?: string,
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  if (organizationId) {
    headers.set('X-Organization-Id', organizationId)
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  })

  if (!response.ok) {
    throw new ApiError(response.status)
  }

  if (response.status === 204 || response.headers.get('Content-Length') === '0') {
    return undefined as T
  }

  const payload: unknown = await response.json()
  return payload as T
}

export function loginRequest(payload: LoginPayload): Promise<TokenResponse> {
  return request<TokenResponse>('/api/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function registerRequest(payload: RegisterPayload): Promise<RegisterResponse> {
  return request<RegisterResponse>('/api/v1/auth/register', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function verifyEmailRequest(token: string): Promise<EmailVerificationResponse> {
  return request<EmailVerificationResponse>('/api/v1/auth/verify-email', {
    method: 'POST',
    body: JSON.stringify({ token }),
  })
}

export function resendVerificationRequest(email: string): Promise<EmailResendResponse> {
  return request<EmailResendResponse>('/api/v1/auth/resend-verification', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export function getCurrentUserProfile(token: string): Promise<UserProfile> {
  return request<UserProfile>('/api/v1/auth/me', {}, token)
}

export function getOrganizations(token: string): Promise<Organization[]> {
  return request<Organization[]>('/api/v1/organizations/me', {}, token)
}

export function acceptInvitationRequest(
  token: string,
  accessToken: string,
): Promise<InvitationAcceptResponse> {
  return request<InvitationAcceptResponse>(
    '/api/v1/invitations/accept',
    {
      method: 'POST',
      body: JSON.stringify({ token }),
    },
    accessToken,
  )
}

export function getDashboardSummary(
  token: string,
  organizationId: string,
): Promise<DashboardSummary> {
  return request<DashboardSummary>('/api/v1/dashboard/summary', {}, token, organizationId)
}

export function getOAuthAuthorizationUrl(
  token: string,
  organizationId: string,
  provider: GitProvider,
): Promise<OAuthAuthorizationResponse> {
  return request<OAuthAuthorizationResponse>(
    `/api/v1/repositories/oauth/${provider.toLowerCase()}/authorize`,
    {},
    token,
    organizationId,
  )
}

export function getRemoteRepositories(
  token: string,
  organizationId: string,
  provider: GitProvider,
): Promise<RemoteRepositoryPage> {
  const query = new URLSearchParams({ provider, limit: '50', offset: '0' })
  return request<RemoteRepositoryPage>(
    `/api/v1/repositories/remote?${query.toString()}`,
    {},
    token,
    organizationId,
  )
}

export function connectRepository(
  token: string,
  organizationId: string,
  payload: RepositoryConnectPayload,
): Promise<RepositoryConnectResponse> {
  return request<RepositoryConnectResponse>(
    '/api/v1/repositories/connect',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

export function updateRepository(
  token: string,
  organizationId: string,
  repositoryId: string,
  payload: RepositoryUpdatePayload,
): Promise<Repository> {
  return request<Repository>(
    `/api/v1/repositories/${repositoryId}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

export function disconnectRepository(
  token: string,
  organizationId: string,
  repositoryId: string,
): Promise<void> {
  return request<void>(
    `/api/v1/repositories/${repositoryId}`,
    { method: 'DELETE' },
    token,
    organizationId,
  )
}

export interface VulnerabilityQuery {
  limit?: number
  offset?: number
  severity?: VulnerabilitySeverity
  status?: IssueStatus
  target?: string
  search?: string
}

export function getVulnerabilities(
  token: string,
  organizationId: string,
  query: VulnerabilityQuery = {},
): Promise<VulnerabilityPage> {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset !== undefined) params.set('offset', String(query.offset))
  if (query.severity) params.set('severity', query.severity)
  if (query.status) params.set('status', query.status)
  if (query.target) params.set('target', query.target)
  if (query.search) params.set('search', query.search)
  return request<VulnerabilityPage>(
    `/api/v1/vulnerabilities/?${params.toString()}`,
    {},
    token,
    organizationId,
  )
}

export function getVulnerability(
  token: string,
  organizationId: string,
  vulnerabilityId: string,
): Promise<VulnerabilityDetail> {
  return request<VulnerabilityDetail>(
    `/api/v1/vulnerabilities/${vulnerabilityId}`,
    {},
    token,
    organizationId,
  )
}

export function createFixPullRequest(
  token: string,
  organizationId: string,
  vulnerabilityId: string,
  payload: AutofixRequest,
): Promise<AutofixResponse> {
  return request<AutofixResponse>(
    `/api/v1/vulnerabilities/${vulnerabilityId}/create-fix-pr`,
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

/**
 * Triaje de un hallazgo. El backend solo admite `status` y rechaza con `422`
 * cualquier otro campo (R4), así que este cliente nunca envía evidencias.
 */
export function triageVulnerability(
  token: string,
  organizationId: string,
  vulnerabilityId: string,
  status: IssueStatus,
): Promise<TriageResponse> {
  return request<TriageResponse>(
    `/api/v1/vulnerabilities/${vulnerabilityId}`,
    { method: 'PATCH', body: JSON.stringify({ status }) },
    token,
    organizationId,
  )
}

export interface AuditQuery {
  limit?: number
  offset?: number
  entityType?: string
  entityId?: string
}

export function getAuditLog(
  token: string,
  organizationId: string,
  query: AuditQuery = {},
): Promise<AuditLogPage> {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset !== undefined) params.set('offset', String(query.offset))
  if (query.entityType !== undefined) params.set('entity_type', query.entityType)
  if (query.entityId !== undefined) params.set('entity_id', query.entityId)
  return request<AuditLogPage>(
    `/api/v1/audit-log/?${params.toString()}`,
    {},
    token,
    organizationId,
  )
}

export function getRepositoryReviews(
  token: string,
  organizationId: string,
  repositoryId: string,
  limit = 25,
  offset = 0,
): Promise<PRReviewPage> {
  return request<PRReviewPage>(
    `/api/v1/repositories/${repositoryId}/reviews?limit=${limit}&offset=${offset}`,
    {},
    token,
    organizationId,
  )
}

export interface KnowledgeQuery {
  limit?: number
  offset?: number
  category?: KnowledgeCategory
  severity?: KnowledgeSeverity
  search?: string
}

export function getKnowledgeEntries(
  token: string,
  organizationId: string,
  query: KnowledgeQuery = {},
): Promise<KnowledgePage> {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset !== undefined) params.set('offset', String(query.offset))
  if (query.category !== undefined) params.set('category', query.category)
  if (query.severity !== undefined) params.set('severity', query.severity)
  if (query.search !== undefined) params.set('search', query.search)
  return request<KnowledgePage>(
    `/api/v1/knowledge/?${params.toString()}`,
    {},
    token,
    organizationId,
  )
}

export function getKnowledgeEntry(
  token: string,
  organizationId: string,
  entryId: string,
): Promise<KnowledgeDetail> {
  return request<KnowledgeDetail>(`/api/v1/knowledge/${entryId}`, {}, token, organizationId)
}

export function getOnboardingStatus(
  token: string,
  organizationId: string,
): Promise<OnboardingStatus> {
  return request<OnboardingStatus>('/api/v1/onboarding/status', {}, token, organizationId)
}

// --------------------------------------------------------------------------- //
// Revisiones de pull request de toda la organización (MENU-MAP §4).
//
// El listado global es un endpoint distinto del por repositorio a propósito: la vista
// global no conoce de antemano qué repositorio quiere el usuario, y filtrar en el
// cliente obligaría a traer todos los repositorios para descartar filas ajenas al filtro.
// --------------------------------------------------------------------------- //

export interface PRReviewQuery {
  status?: PRReviewStatus
  repositoryId?: string
  limit?: number
  offset?: number
}

export function getPRReviews(
  token: string,
  organizationId: string,
  query: PRReviewQuery = {},
): Promise<PRReviewPage> {
  const params = new URLSearchParams()
  if (query.status) params.set('status', query.status)
  if (query.repositoryId) params.set('repository_id', query.repositoryId)
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset !== undefined) params.set('offset', String(query.offset))
  const encoded = params.toString()
  return request<PRReviewPage>(
    `/api/v1/pr-reviews/${encoded ? `?${encoded}` : ''}`,
    {},
    token,
    organizationId,
  )
}

export function getPRReviewMetrics(
  token: string,
  organizationId: string,
): Promise<PRReviewMetrics> {
  return request<PRReviewMetrics>('/api/v1/pr-reviews/metrics', {}, token, organizationId)
}

export interface PentestQuery {
  limit?: number
  offset?: number
  status?: ScanStatus
  target_type?: TargetType
  scan_mode?: ScanMode
  search?: string
}

export function getPentests(
  token: string,
  organizationId: string,
  query: PentestQuery = {},
): Promise<PentestRunPage> {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset !== undefined) params.set('offset', String(query.offset))
  if (query.status) params.set('status', query.status)
  if (query.target_type) params.set('target_type', query.target_type)
  if (query.scan_mode) params.set('scan_mode', query.scan_mode)
  if (query.search) params.set('search', query.search)
  return request<PentestRunPage>(
    `/api/v1/pentests/?${params.toString()}`,
    {},
    token,
    organizationId,
  )
}

export function getPentestRun(
  token: string,
  organizationId: string,
  runId: string,
): Promise<PentestRun> {
  return request<PentestRun>(`/api/v1/pentests/${runId}`, {}, token, organizationId)
}

export function createPentest(
  token: string,
  organizationId: string,
  payload: PentestCreatePayload,
): Promise<PentestRun> {
  return request<PentestRun>(
    '/api/v1/pentests/',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

export function abortPentest(
  token: string,
  organizationId: string,
  runId: string,
): Promise<PentestRun> {
  return request<PentestRun>(
    `/api/v1/pentests/${runId}/abort`,
    { method: 'POST' },
    token,
    organizationId,
  )
}

export function getAdminOrganizations(
  token: string,
  organizationId: string,
  limit = 50,
  offset = 0,
): Promise<AdminOrganizationPage> {
  return request<AdminOrganizationPage>(
    `/api/v1/admin/organizations?limit=${limit}&offset=${offset}`,
    {},
    token,
    organizationId,
  )
}

export function getInfrastructureHealth(
  token: string,
  organizationId: string,
): Promise<InfrastructureHealth> {
  return request<InfrastructureHealth>(
    '/api/v1/admin/health',
    {},
    token,
    organizationId,
  )
}

export function getLLMModels(
  token: string,
  organizationId: string,
): Promise<LLMModelPage> {
  return request<LLMModelPage>('/api/v1/admin/llm/?limit=100', {}, token, organizationId)
}

export function createLLMModel(
  token: string,
  organizationId: string,
  payload: LLMModelCreatePayload,
): Promise<LLMModelConfig> {
  return request<LLMModelConfig>(
    '/api/v1/admin/llm/',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

export function updateLLMModel(
  token: string,
  organizationId: string,
  modelId: string,
  payload: LLMModelUpdatePayload,
): Promise<LLMModelConfig> {
  return request<LLMModelConfig>(
    `/api/v1/admin/llm/${modelId}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

// --------------------------------------------------------------------------- //
// Catálogo CVE de referencia.
//
// R3 no aplica: el catálogo es público y no pertenece a ningún tenant. La cabecera
// `X-Organization-Id` se envía igualmente porque el endpoint exige contexto de
// tenant para autorizar la petición, no para filtrar los datos.
// --------------------------------------------------------------------------- //

function buildCveSearch(params: CVESearchParams): string {
  const search = new URLSearchParams()
  if (params.query) search.set('query', params.query)
  if (params.severity) search.set('severity', params.severity)
  if (params.is_kev_only) search.set('is_kev_only', 'true')
  if (params.year !== undefined) search.set('year', String(params.year))
  if (params.limit !== undefined) search.set('limit', String(params.limit))
  if (params.offset !== undefined) search.set('offset', String(params.offset))
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export function searchCVE(
  token: string,
  organizationId: string,
  params: CVESearchParams,
): Promise<CVEPage> {
  return request<CVEPage>(
    `/api/v1/cve/search${buildCveSearch(params)}`,
    {},
    token,
    organizationId,
  )
}

export function getTrendingKEV(
  token: string,
  organizationId: string,
  limit = 10,
): Promise<CVEPage> {
  return request<CVEPage>(`/api/v1/cve/trending-kev?limit=${limit}`, {}, token, organizationId)
}

export function getCVERecord(
  token: string,
  organizationId: string,
  cveId: string,
): Promise<CVEDetail> {
  // El identificador va codificado: un `CVE-` mal formado con barra se interpretaría
  // como otra ruta y devolvería un 404 en lugar del detalle normalizado.
  return request<CVEDetail>(
    `/api/v1/cve/${encodeURIComponent(cveId)}`,
    {},
    token,
    organizationId,
  )
}

export function getCVEYears(token: string, organizationId: string): Promise<CVEYearsResponse> {
  return request<CVEYearsResponse>('/api/v1/cve/years', {}, token, organizationId)
}
