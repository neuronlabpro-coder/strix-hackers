import { ArrowLeft, CircleDot, LogOut, Lock } from 'lucide-react'
import { Link, NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../features/auth/useAuth'
import { settingsNavigation } from '../features/settings/settingsNavigation'

/**
 * Columna izquierda de Ajustes, en el hueco donde vive el sidebar principal.
 *
 * ## Por qué este componente **sustituye** al `Sidebar` y no se añade debajo
 *
 * Porque tener los dos no es "dos menús", es un menú partido: 260 px de navegación general
 * más 220 px de secciones de Ajustes se comen 480 px de los ~1440 de una pantalla, y el texto
 * de la columna de la izquierda queda con el hueco justo para partir «Nombre del workspace»
 * en dos líneas pegadas al rótulo. El contenido se estrecha, la tabla de miembros se
 * descuadra y la pantalla de Ajustes deja de parecer una pantalla y parece un encaje.
 *
 * ## Por qué sustituye en el shell y no dentro de `SettingsLayout`
 *
 * Porque el sidebar principal es `position: fixed` y ocupa una columna **fuera** del flujo del
 * contenido. Un submenú dentro de `SettingsLayout` vive dentro de esa columna y se apila al
 * lado, que es exactamente el problema. La sustitución tiene que ocurrir donde se decide qué
 * hay en la columna izquierda: el shell.
 *
 * ## Por qué el pie se repite aquí
 *
 * El pie con el perfil y el cierre de sesión es lo que hace que el usuario sepa que sigue
 * dentro de la aplicación y que puede salir. Si Ajustes lo sustituye por completo, un usuario
 * encerrado en cinco pantallas sin salida visible tiene que|subir al navegador para
 * cerrarla. Por eso el pie se replica en vez de delegar.
 */
export function SettingsSidebar() {
  const { t } = useTranslation('settings')
  const { t: tCommon } = useTranslation('common')
  const { user, organizations, selectedOrganizationId, logout } = useAuth()

  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? null
  const esSuperusuario = user?.is_superuser === true
  const esEnterprise = organization?.plan_tier === 'ENTERPRISE'

  const displayName = user?.full_name || tCommon('userFallback')
  const displayEmail = user?.email || tCommon('userFallback')
  const initials = (user?.full_name || tCommon('userInitials')).slice(0, 2).toUpperCase()

  /**
   * Si esta entrada se puede abrir.
   *
   * El candado informa; no autoriza. El servidor filtra por tenant de todos modos, así que
   * ocultar la entrada no protege nada y sí esconde un producto que el cliente no compró.
   */
  const bloqueada = (item: (typeof settingsNavigation)[number]): boolean =>
    item.requiresEnterprise === true && !esEnterprise && !esSuperusuario

  return (
    <aside className="sidebar settings-sidebar" aria-label={t('sections.title')}>
      <div className="sidebar-header settings-sidebar-header">
        <Link className="settings-back-link" to="/dashboard">
          <ArrowLeft size={15} aria-hidden="true" />
          <span>{t('sections.backToPanel')}</span>
        </Link>
        <p className="settings-sidebar-title">{t('sections.title')}</p>
      </div>

      <nav className="sidebar-navigation settings-sidebar-navigation">
        <ul>
          {settingsNavigation.map((item) => {
            const bloqueadaAhora = bloqueada(item)
            if (bloqueadaAhora) {
              /*
                Un `<span>` y no un `<NavLink>` cuando está bloqueada.
                Un enlace con `aria-disabled` seguiría siendo enfocable y navegable con el
                teclado, y quien navegue con teclado acabaría en una vista que dice que no
                tiene acceso, que es respuesta a una pregunta que no ha hecho. Un `span` no
                es enfocable y no hay a dónde ir.
              */
              return (
                <li key={item.path}>
                  <span
                    className="nav-link locked-nav-link"
                    title={t('locked.tooltip')}
                  >
                    <item.icon size={17} aria-hidden="true" />
                    <span>{t(item.labelKey)}</span>
                    <Lock size={13} aria-hidden="true" />
                  </span>
                </li>
              )
            }
            return (
              <li key={item.path}>
                <NavLink
                  className="nav-link settings-nav-link"
                  to={item.path}
                  end={item.end === true}
                >
                  <item.icon size={17} aria-hidden="true" />
                  <span>{t(item.labelKey)}</span>
                </NavLink>
              </li>
            )
          })}
        </ul>
      </nav>

      <div className="sidebar-footer">
        <div className="user-profile">
          <div className="avatar" aria-hidden="true">
            {initials}
          </div>
          <div className="user-copy">
            <strong>{displayName}</strong>
            <span>{displayEmail}</span>
          </div>
          <CircleDot size={12} className="status-dot" aria-hidden="true" />
        </div>
        <button className="logout-button" type="button" onClick={logout}>
          <LogOut size={16} aria-hidden="true" />
          <span>{tCommon('signOut')}</span>
        </button>
      </div>
    </aside>
  )
}
