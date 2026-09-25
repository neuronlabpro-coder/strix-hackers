import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { getOrganizations, loginRequest, registerRequest } from '../../lib/api'
import {
  clearSession,
  readActiveOrganizationId,
  readSession,
  saveActiveOrganizationId,
  saveSession,
  type StoredUser,
} from '../../lib/session'
import type { LoginPayload, Organization, RegisterPayload } from '../../types/api'
import { AuthContext, type AuthContextValue } from './auth-context'

export function AuthProvider({ children }: { children: ReactNode }) {
  const initialSession = readSession()
  const [token, setToken] = useState<string | null>(initialSession?.token ?? null)
  const [user, setUser] = useState<StoredUser | null>(initialSession?.user ?? null)
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<string | null>(
    readActiveOrganizationId(),
  )
  const [isLoading, setIsLoading] = useState(Boolean(initialSession))

  const loadOrganizations = useCallback(async (activeToken: string) => {
    const loadedOrganizations = await getOrganizations(activeToken)
    setOrganizations(loadedOrganizations)
    const activeOrganizationId = loadedOrganizations[0]?.id ?? null
    setSelectedOrganizationId(activeOrganizationId)
    if (activeOrganizationId) {
      saveActiveOrganizationId(activeOrganizationId)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!token) {
      return () => {
        cancelled = true
      }
    }

    // oxlint-disable-next-line react/set-state-in-effect
    void loadOrganizations(token)
      .catch(() => {
        if (!cancelled) {
          clearSession()
          setToken(null)
          setUser(null)
          setOrganizations([])
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [loadOrganizations, token])

  const login = useCallback(async (payload: LoginPayload) => {
    setIsLoading(true)
    const response = await loginRequest(payload)
    const authenticatedUser: StoredUser = {
      email: payload.email,
      full_name: payload.email,
    }
    saveSession({ token: response.access_token, user: authenticatedUser })
    setToken(response.access_token)
    setUser(authenticatedUser)
  }, [])

  const register = useCallback(async (payload: RegisterPayload) => {
    setIsLoading(true)
    const response = await registerRequest(payload)
    if (response.verification_required) {
      setIsLoading(false)
      return response
    }

    const loginResponse = await loginRequest({
      email: payload.email,
      password: payload.password,
    })
    saveSession({ token: loginResponse.access_token, user: response.user })
    setToken(loginResponse.access_token)
    setUser(response.user)
    return response
  }, [])

  const logout = useCallback(() => {
    clearSession()
    setToken(null)
    setUser(null)
    setOrganizations([])
    setSelectedOrganizationId(null)
    setIsLoading(false)
  }, [])

  const selectOrganization = useCallback((organizationId: string) => {
    setSelectedOrganizationId(organizationId)
    saveActiveOrganizationId(organizationId)
  }, [])

  const contextValue = useMemo<AuthContextValue>(
    () => ({
      token,
      user,
      organizations,
      selectedOrganizationId,
      isLoading,
      login,
      register,
      logout,
      selectOrganization,
    }),
    [
      isLoading,
      login,
      logout,
      organizations,
      register,
      selectOrganization,
      selectedOrganizationId,
      token,
      user,
    ],
  )

  return <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>
}
