export interface StoredUser {
  /**
   * Identificador del usuario.
   *
   * Lo que se guarda en la sesión es el perfil que devuelve `/auth/me`, y ese perfil **ya**
   * traía el `id`: no se guardaba porque hasta ahora no lo necesitaba nadie. La vista de
   * miembros y el hilo de tickets lo necesitan para distinguir "esto es mío" de "esto es
   * de otro", y comparando por correo se resolvería con menos cambios.
   *
   * Se comparó esa opción y es peor por una razón concreta: el correo no es una clave
   * estable. Si un usuario cambia su correo, la fila de la tabla de miembros y la del
   * `sessionStorage` dejan de coincidir y "mi propio mensaje" aparece como ajeno. El `id`
   * no cambia nunca.
   *
   * Es **opcional** a propósito. Las sesiones ya guardadas antes de este campo no lo
   * tienen, y exigirlo en el validador las rechazaría todas: el usuario cerraría sesión de
   * golpe y perdería el trabajo abierto. Con el campo opcional, una sesión vieja sigue
   * funcionando y el panel degrada de "esto es mío" a "no lo sé", que es el fallo honesto.
   */
  id?: string
  email: string
  full_name: string
  is_superuser: boolean
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
  // El `id` se acepta si viene y se tolera si no. Ver la nota de `StoredUser.id`: una
  // sesión sin él es válida y funciona, solo que sin poder identificar al usuario actual.
  if (candidate.id !== undefined && typeof candidate.id !== 'string') {
    return false
  }
  return (
    typeof candidate.email === 'string' &&
    typeof candidate.full_name === 'string' &&
    typeof candidate.is_superuser === 'boolean'
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
