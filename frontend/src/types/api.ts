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
