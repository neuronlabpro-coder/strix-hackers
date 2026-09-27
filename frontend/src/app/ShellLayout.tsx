import { Plus } from 'lucide-react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { LanguageSwitcher } from '../components/LanguageSwitcher'
import { SettingsSidebar } from '../components/SettingsSidebar'
import { Sidebar } from '../components/Sidebar'
import { useAuth } from '../features/auth/useAuth'
import { CreditBalancePill } from '../features/billing/CreditBalancePill'

export function ShellLayout() {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const location = useLocation()
  const {
    organizations,
    selectedOrganizationId,
    user,
    selectOrganization,
    logout,
  } = useAuth()

  /**
   * ¿Estamos dentro de Ajustes?
   *
   * Se decide aquí y no dentro de `SettingsLayout` porque la columna izquierda la pinta el
   * shell: el sidebar principal es `position: fixed` y ocupa una caja **fuera** del flujo del
   * contenido. Un menú de Ajustes dentro de `SettingsLayout` se apila al lado de esa caja en
   * vez de ocuparla, y de ahí venía el doble sidebar que comprimía el contenido a media
   * pantalla.
   *
   * La comparación es por **prefijo de segmento** y no con `startsWith` a secas: si se
   * comparara la cadena entera, `/settings-something` entraría también. Hoy no existe esa
   * ruta, y cuando exista este sería el momento de romper.
   */
  const enAjustes =
    location.pathname === '/settings' || location.pathname.startsWith('/settings/')

  return (
    <div className="app-shell">
      {enAjustes ? (
        <SettingsSidebar />
      ) : (
        <Sidebar
          organizations={organizations}
          selectedOrganizationId={selectedOrganizationId}
          user={user}
          onSelectOrganization={selectOrganization}
          onCreateWorkspace={() => navigate('/settings')}
          onLogout={logout}
        />
      )}
      <div className="content-shell">
        <header className="topbar">
          <div className="topbar-status">
            <span className="status-dot" aria-hidden="true" />
            <span>{t('headerStatus')}</span>
          </div>
          <div className="topbar-actions">
            {/*
              El conmutador de idioma vive arriba y no junto al logo. En la cabecera del
              sidebar competía con el nombre del producto por el mismo corner, y en
              pantallas estrechas el bloque de marca se comía el ancho que necesita el
              selector de workspace, que es un control con función y no un ajuste.
            */}
            <LanguageSwitcher />
            <CreditBalancePill />
            <span className="topbar-workspace">{t('workspaceReady')}</span>
            <button className="primary-button" type="button" onClick={() => navigate('/pentests')}>
              <Plus size={17} aria-hidden="true" />
              <span>{t('newPentest')}</span>
            </button>
          </div>
        </header>
        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
