import { Plus } from 'lucide-react'
import { Outlet, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { Sidebar } from '../components/Sidebar'
import { useAuth } from '../features/auth/useAuth'

export function ShellLayout() {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const {
    organizations,
    selectedOrganizationId,
    user,
    selectOrganization,
    logout,
  } = useAuth()

  return (
    <div className="app-shell">
      <Sidebar
        organizations={organizations}
        selectedOrganizationId={selectedOrganizationId}
        user={user}
        onSelectOrganization={selectOrganization}
        onCreateWorkspace={() => navigate('/settings')}
        onLogout={logout}
      />
      <div className="content-shell">
        <header className="topbar">
          <div className="topbar-status">
            <span className="status-dot" aria-hidden="true" />
            <span>{t('headerStatus')}</span>
          </div>
          <div className="topbar-actions">
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
