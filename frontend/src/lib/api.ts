import { API_BASE_URL } from '../config'
import type {
  DashboardSummary,
  EmailResendResponse,
  EmailVerificationResponse,
  GitProvider,
  InvitationAcceptResponse,
  LoginPayload,
  OAuthAuthorizationResponse,
  Organization,
  RegisterPayload,
  RegisterResponse,
  RemoteRepositoryPage,
  Repository,
  RepositoryConnectPayload,
  RepositoryConnectResponse,
  RepositoryUpdatePayload,
  TokenResponse,
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
