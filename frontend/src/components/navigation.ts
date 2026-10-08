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
   * Cuando está presente, el acceso depende de la feature resuelta por el backend.
   * El superusuario puede entrar en cualquier ruta de administración.
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

export function isNavigationItemLocked(
  item: NavigationItem,
  isSuperuser: boolean,
  features?: Record<string, boolean> | null,
): boolean {
  if (item.enterpriseFeature === undefined || isSuperuser) return false
  const key = {
    supplyChain: 'supply_chain',
    containers: 'container_scanning',
    networks: 'internal_network_scanning',
  }[item.enterpriseFeature]
  if (features && key in features) return !features[key]
  return true
}
