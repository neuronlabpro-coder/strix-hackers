export interface StoredUser {
  email: string
  full_name: string
}

const tokenStorageKey = 'fenix_access_token'
const userStorageKey = 'fenix_user'
const organizationStorageKey = 'fenix_active_organization'

export interface StoredSession {
  token: string
  user: StoredUser
}

function canUseSessionStorage(): boolean {
  return typeof window !== 'undefined' && typeof window.sessionStorage !== 'undefined'
}

function isStoredUser(value: unknown): value is StoredUser {
  if (typeof value !== 'object' || value === null) {
    return false
  }

  const candidate = value as Record<string, unknown>
  return (
    typeof candidate.email === 'string' &&
    typeof candidate.full_name === 'string'
  )
}

export function readSession(): StoredSession | null {
  if (!canUseSessionStorage()) {
    return null
  }

  const token = window.sessionStorage.getItem(tokenStorageKey)
  const serializedUser = window.sessionStorage.getItem(userStorageKey)
  if (!token || !serializedUser) {
    return null
  }

  try {
    const user: unknown = JSON.parse(serializedUser)
    return isStoredUser(user) ? { token, user } : null
  } catch {
    clearSession()
    return null
  }
}

export function saveSession(session: StoredSession): void {
  if (!canUseSessionStorage()) {
    return
  }
  window.sessionStorage.setItem(tokenStorageKey, session.token)
  window.sessionStorage.setItem(userStorageKey, JSON.stringify(session.user))
}

export function clearSession(): void {
  if (!canUseSessionStorage()) {
    return
  }
  window.sessionStorage.removeItem(tokenStorageKey)
  window.sessionStorage.removeItem(userStorageKey)
  window.sessionStorage.removeItem(organizationStorageKey)
}

export function readActiveOrganizationId(): string | null {
  if (!canUseSessionStorage()) {
    return null
  }
  return window.sessionStorage.getItem(organizationStorageKey)
}

export function saveActiveOrganizationId(organizationId: string): void {
  if (canUseSessionStorage()) {
    window.sessionStorage.setItem(organizationStorageKey, organizationId)
  }
}
