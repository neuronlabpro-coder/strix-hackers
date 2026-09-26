import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { getCurrentUserProfile, getOrganizations, loginRequest, registerRequest } from '../../lib/api'
import { isSessionInvalid } from '../../lib/session-errors'
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
  /*
    Un fallo **transitorio** en la carga inicial. La sesión sigue viva —el token está en su
    sitio— pero no se pudo leer la lista de workspaces, así que la aplicación no puede
    saber qué mostrar. Se distingue de "sin sesión" para que la pantalla no finja que el
    usuario no ha iniciado sesión y lo mande a identificarse de nuevo.
  */
  const [loadFailed, setLoadFailed] = useState(false)

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

    /*
      El estado de arranque distingue **tres** cosas, no dos. Antes había un solo `catch`
      que borraba la sesión ante cualquier fallo, y eso convertía un 502 o un corte de red
      en un cierre de sesión —que es lo que pasaba al volver de GitHub o GitLab, donde el
      usuario ha estado fuera de la aplicación y la red puede haber cambiado.

      Ahora el fallo se clasifica con `isSessionInvalid`: solo un 401 o un 403 —el token no
      vale, o el workspace ya no es del usuario— borra la sesión. Un 5xx, un 429 o un error
      de red dejan el token intacto y solo marcan `loadFailed`, para que la vista pueda
      ofrecer reintentar en vez de expulsar a alguien de la aplicación.
    */
    // oxlint-disable-next-line react/set-state-in-effect
    void loadOrganizations(token)
      .catch((error: unknown) => {
        if (cancelled) {
          return
        }
        if (isSessionInvalid(error)) {
          clearSession()
          setToken(null)
          setUser(null)
          setOrganizations([])
          return
        }
        setLoadFailed(true)
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
    setLoadFailed(false)
    try {
      const response = await loginRequest(payload)
      const profile = await getCurrentUserProfile(response.access_token)
      saveSession({ token: response.access_token, user: profile })
      setToken(response.access_token)
      setUser(profile)
    } finally {
      setIsLoading(false)
    }
  }, [])

  const register = useCallback(async (payload: RegisterPayload) => {
    setIsLoading(true)
    try {
      const response = await registerRequest(payload)
      if (response.verification_required) {
        return response
      }

      const loginResponse = await loginRequest({
        email: payload.email,
        password: payload.password,
      })
      saveSession({ token: loginResponse.access_token, user: response.user })
      setToken(loginResponse.access_token)
      setUser({
        ...response.user,
        is_superuser: response.user.is_superuser ?? false,
      })
      return response
    } finally {
      setIsLoading(false)
    }
  }, [])

  const logout = useCallback(() => {
    clearSession()
    setToken(null)
    setUser(null)
    setOrganizations([])
    setSelectedOrganizationId(null)
    setLoadFailed(false)
    setIsLoading(false)
  }, [])

  /**
   * Vuelve a leer los workspaces sin tocar el token.
   *
   * Es la acción que la aplicación ofrece cuando la carga inicial falló por una razón
   * transitoria. Reintenta **sin** cerrarse sesión, que es justo lo que el arranque ya no
   * hace por su cuenta: repetir es una decisión de la persona, no un efecto secundario de
   * un error de red.
   */
  const retrySession = useCallback(() => {
    if (!token) {
      return
    }
    setIsLoading(true)
    setLoadFailed(false)
    void loadOrganizations(token)
      .catch((error: unknown) => {
        if (isSessionInvalid(error)) {
          clearSession()
          setToken(null)
          setUser(null)
          setOrganizations([])
          return
        }
        setLoadFailed(true)
      })
      .finally(() => {
        setIsLoading(false)
      })
  }, [loadOrganizations, token])

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
      loadFailed,
      retrySession,
      login,
      register,
      logout,
      selectOrganization,
    }),
    [
      isLoading,
      loadFailed,
      login,
      logout,
      organizations,
      register,
      retrySession,
      selectOrganization,
      selectedOrganizationId,
      token,
      user,
    ],
  )

  return <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>
}
