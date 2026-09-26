import { useEffect, useRef, useState } from 'react'
import {
  BrainCircuit,
  ChevronDown,
  CircleDot,
  LogOut,
  Plus,
  Server,
  ShieldCheck,
} from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import type { Organization } from '../types/api'
import {
  EnterpriseGateModal,
  type EnterpriseFeature,
} from '../features/enterprise/EnterpriseGateModal'
import {
  assetNavigation,
  hasEnterpriseAccess,
  isNavigationItemLocked,
  primaryNavigation,
  type NavigationItem,
} from './navigation'

interface SidebarProps {
  organizations: Organization[]
  selectedOrganizationId: string | null
  user: { email: string; full_name: string; is_superuser: boolean } | null
  onSelectOrganization: (organizationId: string) => void
  onCreateWorkspace: () => void
  onLogout: () => void
}

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
  const { t: tEnterprise } = useTranslation('enterprise')
  const { t: tAdmin } = useTranslation('admin')
  const { t: tLlm } = useTranslation('llm')
  const [isWorkspaceMenuOpen, setIsWorkspaceMenuOpen] = useState(false)
  const [lockedFeature, setLockedFeature] = useState<EnterpriseFeature | null>(null)
  const workspaceSelectorRef = useRef<HTMLDivElement>(null)
  const selectedOrganization = organizations.find(
    (organization) => organization.id === selectedOrganizationId,
  )
  const displayName = user?.full_name || tCommon('userFallback')
  const displayEmail = user?.email || tCommon('userFallback')
  const initials = (user?.full_name || tCommon('userInitials')).slice(0, 2).toUpperCase()

  const enterpriseAccess = hasEnterpriseAccess(
    selectedOrganization?.plan_tier,
    user?.is_superuser === true,
  )

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

  const renderItem = (item: NavigationItem) => {
    if (isNavigationItemLocked(item, selectedOrganization?.plan_tier, enterpriseAccess)) {
      return (
        <li key={item.path}>
          <button
            className="nav-link locked-nav-link"
            type="button"
            onClick={() => setLockedFeature(item.enterpriseFeature ?? null)}
          >
            <item.icon size={17} aria-hidden="true" />
            <span>{tNavigation(item.labelKey)}</span>
            <span className="locked-tag">{tEnterprise('locked')}</span>
          </button>
        </li>
      )
    }
    return (
      <li key={item.path}>
        <NavLink className="nav-link" to={item.path}>
          <item.icon size={17} aria-hidden="true" />
          <span>{tNavigation(item.labelKey)}</span>
        </NavLink>
      </li>
    )
  }

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
                  </button>
                </li>
              ))
            ) : null}
            <li className="workspace-menu-create">
              <button
                className="workspace-option"
                type="button"
                onClick={() => {
                  onCreateWorkspace()
                  setIsWorkspaceMenuOpen(false)
                }}
              >
                <Plus size={14} aria-hidden="true" />
                <span>{tCommon('createWorkspace')}</span>
              </button>
            </li>
          </ul>
        ) : null}
      </div>

      <nav className="sidebar-navigation" aria-label={tCommon('mainNavigation')}>
        <p className="eyebrow navigation-label">{tCommon('mainNavigation')}</p>
        <ul>{primaryNavigation.map(renderItem)}</ul>

        <div className="navigation-divider" role="separator" />

        <p className="eyebrow navigation-label navigation-label-spaced">
          {tCommon('assetsNavigation')}
        </p>
        <ul>{assetNavigation.map(renderItem)}</ul>
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
        {user?.is_superuser ? (
          <>
            <NavLink className="nav-link" to="/admin" end>
              <Server size={16} aria-hidden="true" />
              <span>{tAdmin('nav')}</span>
            </NavLink>
            <NavLink className="nav-link" to="/admin/llm">
              <BrainCircuit size={16} aria-hidden="true" />
              <span>{tLlm('nav')}</span>
            </NavLink>
          </>
        ) : null}
        <button className="logout-button" type="button" onClick={onLogout}>
          <LogOut size={16} aria-hidden="true" />
          <span>{tCommon('signOut')}</span>
        </button>
      </div>

      {/*
        El modal se anula en el render en lugar de cerrarse con un efecto. Si el
        superusuario cambia de workspace a uno FREE con el modal abierto, el efecto
        tardaría un render en cerrarlo y la persona vería un modal de venta sobre una
        organización que ya tiene acceso. Derivar en render no tiene esa ventana.
      */}
      <EnterpriseGateModal
        feature={enterpriseAccess ? null : lockedFeature}
        onClose={() => setLockedFeature(null)}
      />
    </aside>
  )
}
