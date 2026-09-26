import { createContext } from 'react'

import type { StoredUser } from '../../lib/session'
import type { LoginPayload, Organization, RegisterPayload, RegisterResponse } from '../../types/api'

export interface AuthContextValue {
  token: string | null
  user: StoredUser | null
  organizations: Organization[]
  selectedOrganizationId: string | null
  isLoading: boolean
  /**
   * La carga inicial falló por una razón **transitoria** —red, `5xx`, `429`— y la sesión
   * sigue viva.
   *
   * Se distingue de "no hay sesión" a propósito. Antes, un fallo de red se traducía en
   * cerrar sesión, y la aplicación no tenía forma de expresar "no puedo leerme los
   * workspaces ahora mismo": lo único que sabía era que algo había fallado, y la respuesta
   * era expulsar al usuario. Con este estado la respuesta es reintentar.
   */
  loadFailed: boolean
  /** Relee los workspaces conservando el token. */
  retrySession: () => void
  login: (payload: LoginPayload) => Promise<void>
  register: (payload: RegisterPayload) => Promise<RegisterResponse>
  logout: () => void
  selectOrganization: (organizationId: string) => void
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined)
