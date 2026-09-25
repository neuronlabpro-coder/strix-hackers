import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '../auth/useAuth'

/**
 * Guarda de ruta de la consola de SuperAdmin. Falla cerrado: si no hay sesion
 * devuelve al login y si el usuario no es superusuario devuelve al dashboard,
 * nunca renderiza la consola.
 */
export function RequireSuperuser({ children }: { children: React.ReactNode }) {
  const { token, user, isLoading } = useAuth()
  const location = useLocation()

  if (isLoading) {
    return null
  }
  if (!token) {
    return <Navigate to="/login" replace state={{ returnTo: location.pathname }} />
  }
  if (!user?.is_superuser) {
    return <Navigate to="/dashboard" replace />
  }
  return children
}
