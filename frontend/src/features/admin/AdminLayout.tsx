import { ArrowLeft, LogOut } from 'lucide-react'
import { NavLink, Outlet, Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { LanguageSwitcher } from '../../components/LanguageSwitcher'
import { useAuth } from '../auth/useAuth'
import { adminNavigation } from './adminNavigation'

/**
 * Layout exclusivo de la consola de SuperAdmin.
 *
 * ## Por qué tiene su propio shell y no una sección del panel de cliente
 *
 * El panel de cliente se construye alrededor de un workspace: selector de organización en
 * la cabecera, bloqueo de plan Enterprise en cada entrada, y rutas que arrastran el tenant.
 * La consola no pertenece a ningún workspace y cruza todos. Compartir shell obligaría a
 * ocultar el selector para que no fuera engañoso, y un control que aparece y no hace nada
 * es peor que su ausencia.
 *
 * ## Por qué el pie tiene "Volver a la App" y no solo cerrar sesión
 *
 * Quien entra en la consola viene a hacer una tarea administrativa puntual. Cerrar sesión
 * le obliga a autenticarse otra vez para volver al trabajo que tenía abierto. El botón de
 * vuelta conserva la sesión, que es lo que se quiere, y el cierre sigue disponible para
 * cuando la máquina compartida lo exija.
 */
export function AdminLayout() {
  const { t } = useTranslation('admin')
  const { t: tCommon } = useTranslation('common')
  const { user, logout } = useAuth()
  const initials = (user?.full_name || tCommon('userInitials')).slice(0, 2).toUpperCase()

  return (
    <div className="app-shell admin-shell">
      <aside className="sidebar admin-sidebar" aria-label={t('sections.consoleLabel')}>
        <div className="sidebar-header">
          <div className="brand-lockup sidebar-brand">
            <div className="brand-mark" aria-hidden="true">
              <ShieldGlyph />
            </div>
            <div>
              <p className="eyebrow">{t('brand.eyebrow')}</p>
              <p className="brand-caption">{t('brand.caption')}</p>
            </div>
          </div>
        </div>

        <nav className="sidebar-navigation admin-navigation" aria-label={t('sections.consoleLabel')}>
          <p className="eyebrow navigation-label">{t('sections.consoleLabel')}</p>
          <ul>
            {adminNavigation.map((item) => (
              <li key={item.path}>
                <NavLink
                  className="nav-link"
                  to={item.path}
                  end={item.end === true}
                >
                  <item.icon size={17} aria-hidden="true" />
                  <span>{t(item.labelKey)}</span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="sidebar-footer">
          {user ? (
            <div className="user-profile">
              <div className="avatar" aria-hidden="true">
                {initials}
              </div>
              <div className="user-copy">
                <strong>{user.full_name || tCommon('userFallback')}</strong>
                <span>{user.email}</span>
              </div>
            </div>
          ) : null}
          {/*
            El botón de volver va **antes** del cierre y con su propia clase en vez de
            reutilizar `logout-button`: es la acción principal del pie para quien está de
            paso por aquí, y con la misma clase que "cerrar sesión" el operador acabaría
            cerrando sesión en lugar de volver a su trabajo.
          */}
          <Link className="admin-back-link" to="/dashboard">
            <ArrowLeft size={16} aria-hidden="true" />
            <span>{t('sections.backToApp')}</span>
          </Link>
          <button className="logout-button" type="button" onClick={logout}>
            <LogOut size={16} aria-hidden="true" />
            <span>{tCommon('signOut')}</span>
          </button>
        </div>
      </aside>

      <div className="content-shell">
        <header className="topbar">
          <div className="topbar-status">
            <span className="status-dot" aria-hidden="true" />
            <span>{t('brand.eyebrow')}</span>
          </div>
          <div className="topbar-actions">
            <LanguageSwitcher />
          </div>
        </header>
        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function ShieldGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor">
      <path d="M12 3l7 3v5c0 4.4-2.9 8.5-7 10-4.1-1.5-7-5.6-7-10V6l7-3z" strokeWidth="1.6" />
      <path d="M9.2 12.2l2 2 3.6-4" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}
