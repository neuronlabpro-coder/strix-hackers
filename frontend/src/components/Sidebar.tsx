import { useEffect, useRef, useState, type ComponentType } from 'react'
import {
  BookOpen,
  ChevronDown,
  CircleDot,
  FolderGit2,
  GitPullRequest,
  LayoutDashboard,
  LogOut,
  Plus,
  Settings,
  ShieldCheck,
  TestTube2,
  TriangleAlert,
  type LucideProps,
} from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import type { Organization } from '../types/api'
import { LanguageSwitcher } from './LanguageSwitcher'

type Icon = ComponentType<LucideProps>

interface NavigationItem {
  labelKey: string
  path: string
  icon: Icon
}

interface SidebarProps {
  organizations: Organization[]
  selectedOrganizationId: string | null
  user: { email: string; full_name: string } | null
  onSelectOrganization: (organizationId: string) => void
  onCreateWorkspace: () => void
  onLogout: () => void
}

const primaryNavigation: NavigationItem[] = [
  { labelKey: 'dashboard', path: '/dashboard', icon: LayoutDashboard },
  { labelKey: 'pentests', path: '/pentests', icon: TestTube2 },
  { labelKey: 'issues', path: '/issues', icon: TriangleAlert },
  { labelKey: 'prReviews', path: '/pr-reviews', icon: GitPullRequest },
]

const assetNavigation: NavigationItem[] = [
  { labelKey: 'repositories', path: '/repositories', icon: FolderGit2 },
  { labelKey: 'knowledge', path: '/knowledge', icon: BookOpen },
  { labelKey: 'settings', path: '/settings', icon: Settings },
]

export function Sidebar({
  organizations,
  selectedOrganizationId,
  user,
  onSelectOrganization,
  onCreateWorkspace,
  onLogout,
}: SidebarProps) {
  const { t: tCommon } = useTranslation('common')
  const { t: tNavigation } = useTranslation('navigation')
  const [isWorkspaceMenuOpen, setIsWorkspaceMenuOpen] = useState(false)
  const workspaceSelectorRef = useRef<HTMLDivElement>(null)
  const selectedOrganization = organizations.find(
    (organization) => organization.id === selectedOrganizationId,
  )
  const displayName = user?.full_name || tCommon('userFallback')
  const displayEmail = user?.email || tCommon('userFallback')
  const initials = (user?.full_name || tCommon('userInitials')).slice(0, 2).toUpperCase()

  useEffect(() => {
    if (!isWorkspaceMenuOpen) {
      return
    }

    const handleOutsidePointer = (event: PointerEvent) => {
      if (
        workspaceSelectorRef.current &&
        !workspaceSelectorRef.current.contains(event.target as Node)
      ) {
        setIsWorkspaceMenuOpen(false)
      }
    }

    document.addEventListener('pointerdown', handleOutsidePointer)
    return () => document.removeEventListener('pointerdown', handleOutsidePointer)
  }, [isWorkspaceMenuOpen])

  return (
    <aside className="sidebar" aria-label={tCommon('organizationSelector')}>
      <div className="sidebar-header">
        <div className="brand-lockup sidebar-brand">
          <div className="brand-mark" aria-hidden="true">
            <ShieldCheck size={18} />
          </div>
          <div>
            <p className="eyebrow">{tCommon('appName')}</p>
            <p className="brand-caption">{tCommon('appDescription')}</p>
          </div>
        </div>
        <LanguageSwitcher />
      </div>

      <div className="workspace-selector" ref={workspaceSelectorRef}>
        <button
          className="workspace-trigger"
          type="button"
          onClick={() => setIsWorkspaceMenuOpen((isOpen) => !isOpen)}
          aria-haspopup="listbox"
          aria-expanded={isWorkspaceMenuOpen}
          aria-label={tCommon('selectOrganization')}
        >
          <span className="workspace-trigger-copy">
            <span className="eyebrow">{tNavigation('workspace')}</span>
            <strong>{selectedOrganization?.name || tCommon('noWorkspace')}</strong>
          </span>
          <ChevronDown size={16} aria-hidden="true" />
        </button>
        {isWorkspaceMenuOpen ? (
          <ul className="workspace-menu" role="listbox" aria-label={tCommon('currentOrganization')}>
            {organizations.length ? (
              organizations.map((organization) => (
                <li key={organization.id}>
                  <button
                    className={
                      organization.id === selectedOrganizationId
                        ? 'workspace-option workspace-option-selected'
                        : 'workspace-option'
                    }
                    type="button"
                    role="option"
                    aria-selected={organization.id === selectedOrganizationId}
                    onClick={() => {
                      onSelectOrganization(organization.id)
                      setIsWorkspaceMenuOpen(false)
                    }}
                  >
                    <span>{organization.name}</span>
                    <span className="workspace-role">{organization.role}</span>
                  </button>
                </li>
              ))
            ) : (
              <li className="workspace-empty">{tCommon('noOrganizations')}</li>
            )}
          </ul>
        ) : null}
        <button
          className="create-workspace-button"
          type="button"
          onClick={() => {
            setIsWorkspaceMenuOpen(false)
            onCreateWorkspace()
          }}
        >
          <Plus size={15} aria-hidden="true" />
          <span>{tCommon('createWorkspace')}</span>
        </button>
      </div>

      <nav className="sidebar-navigation" aria-label={tCommon('mainNavigation')}>
        <p className="eyebrow navigation-label">{tCommon('mainNavigation')}</p>
        <ul>
          {primaryNavigation.map((item) => (
            <li key={item.path}>
              <NavLink className="nav-link" to={item.path}>
                <item.icon size={17} aria-hidden="true" />
                <span>{tNavigation(item.labelKey)}</span>
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="eyebrow navigation-label navigation-label-spaced">
          {tCommon('assetsNavigation')}
        </p>
        <ul>
          {assetNavigation.map((item) => (
            <li key={item.path}>
              <NavLink className="nav-link" to={item.path}>
                <item.icon size={17} aria-hidden="true" />
                <span>{tNavigation(item.labelKey)}</span>
              </NavLink>
            </li>
          ))}
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
        <button className="logout-button" type="button" onClick={onLogout}>
          <LogOut size={16} aria-hidden="true" />
          <span>{tCommon('signOut')}</span>
        </button>
      </div>
    </aside>
  )
}
