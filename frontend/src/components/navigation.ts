import {
  BookOpen,
  Boxes,
  FolderGit2,
  Globe,
  GitPullRequest,
  KeyRound,
  LayoutDashboard,
  MessageSquare,
  Network,
  DatabaseSearch,
  PackageSearch,
  Plug,
  Radar,
  Settings,
  TestTube2,
  TriangleAlert,
  Wallet,
  type LucideProps,
} from 'lucide-react'
import type { ComponentType } from 'react'

import type { EnterpriseFeature } from '../features/enterprise/EnterpriseGateModal'

export type Icon = ComponentType<LucideProps>

export interface NavigationItem {
  labelKey: string
  path: string
  icon: Icon
  /**
   * Cuando está presente, el acceso pasa por el candado Enterprise. Un plan Enterprise
   * o un superusuario no lo ven y navegan directo a la ruta.
   */
  enterpriseFeature?: EnterpriseFeature
}

/**
 * Bloque principal. El orden no es un detalle de estilo: la posición
 * de un ítem en la navegación es jerarquía, y mover `Chat` por debajo de `Settings`
 * cambia qué el usuario considera tarea principal.
 */
export const primaryNavigation: NavigationItem[] = [
  { labelKey: 'dashboard', path: '/dashboard', icon: LayoutDashboard },
  { labelKey: 'pentests', path: '/pentests', icon: TestTube2 },
  { labelKey: 'issues', path: '/issues', icon: TriangleAlert },
  { labelKey: 'prReviews', path: '/pr-reviews', icon: GitPullRequest },
  {
    labelKey: 'supplyChain',
    path: '/supply-chain',
    icon: PackageSearch,
    enterpriseFeature: 'supplyChain',
  },
  {
    labelKey: 'containers',
    path: '/containers',
    icon: Boxes,
    enterpriseFeature: 'containers',
  },
  { labelKey: 'chat', path: '/chat', icon: MessageSquare },
]

/** Bloque de activos y configuración, separado del principal por una regla visual. */
export const assetNavigation: NavigationItem[] = [
  { labelKey: 'repositories', path: '/repositories', icon: FolderGit2 },
  { labelKey: 'domains', path: '/domains', icon: Globe },
  { labelKey: 'assetDiscovery', path: '/asset-discovery', icon: Radar },
  {
    labelKey: 'networks',
    path: '/networks',
    icon: Network,
    enterpriseFeature: 'networks',
  },
  { labelKey: 'knowledge', path: '/knowledge', icon: BookOpen },
  { labelKey: 'cve', path: '/cve', icon: DatabaseSearch },
  { labelKey: 'integrations', path: '/integrations', icon: Plug },
  { labelKey: 'apiAccess', path: '/api-access', icon: KeyRound },
  { labelKey: 'billing', path: '/billing', icon: Wallet },
  { labelKey: 'settings', path: '/settings', icon: Settings },
]

/**
 * Regla de acceso Enterprise.
 *
 * El superusuario salta el candado porque su trabajo es operar la plataforma en todos
 * sus tenants: un administrador global encerrado en un modal de venta no puede probar
 * la funcionalidad que se está vendiendo.
 */
export function hasEnterpriseAccess(
  planTier: string | null | undefined,
  isSuperuser: boolean,
): boolean {
  return planTier === 'ENTERPRISE' || isSuperuser
}

export function isNavigationItemLocked(
  item: NavigationItem,
  planTier: string | null | undefined,
  isSuperuser: boolean,
): boolean {
  return item.enterpriseFeature !== undefined && !hasEnterpriseAccess(planTier, isSuperuser)
}
