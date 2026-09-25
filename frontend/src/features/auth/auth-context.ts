import { createContext } from 'react'

import type { StoredUser } from '../../lib/session'
import type { LoginPayload, Organization, RegisterPayload } from '../../types/api'

export interface AuthContextValue {
  token: string | null
  user: StoredUser | null
  organizations: Organization[]
  selectedOrganizationId: string | null
  isLoading: boolean
  login: (payload: LoginPayload) => Promise<void>
  register: (payload: RegisterPayload) => Promise<void>
  logout: () => void
  selectOrganization: (organizationId: string) => void
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined)
