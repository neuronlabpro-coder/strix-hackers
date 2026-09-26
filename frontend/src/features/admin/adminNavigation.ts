/**
 * Navegación de la consola de SuperAdmin.
 *
 * ## Por qué no reutiliza el `navigation.ts` del panel de cliente
 *
 * El panel de cliente navega **dentro de un workspace**: cada entrada es una vista de los
 * datos del tenant. La consola navega **sobre la plataforma**: cada entrada es una vista
 * transversal que cruza tenants. Reutilizar la misma lista obligaría a mezclar un array
 * con entradas de un plano y otro, y el filtro de superusuario tendría que depender de la
 * posición en la lista en vez de del tipo de entrada.
 *
 * Además, las rutas de la consola no aceptan `X-Organization-Id`. Un enlace a
 * `/admin/tenants` dentro del shell de cliente arrastraría el contexto de tenant, que el
 * servidor ignora pero que hace creer al operador que la vista está acotada.
 */

import {
  BarChart3,
  Building2,
  CreditCard,
  ScrollText,
  Server,
  BrainCircuit,
  type LucideIcon,
} from 'lucide-react'

export interface AdminNavigationItem {
  path: string
  /** Clave del namespace `admin`. Nunca un literal: R1. */
  labelKey: string
  icon: LucideIcon
  /** Con `end`, `/admin` no se marca activo cuando estás en `/admin/tenants`. */
  end?: boolean
}

/**
 * Orden de la consola.
 *
 * Va de lo general a lo concreto: primero el resumen, luego las cinco secciones. Es el
 * orden en el que un operador de plataforma hace su ronda: mira cómo va, mira a quién,
 * mira a la gente, mira lo que se vendió, mira qué se tocó y por último ajusta los modelos.
 */
export const adminNavigation: readonly AdminNavigationItem[] = [
  { path: '/admin', labelKey: 'sections.overview', icon: BarChart3, end: true },
  { path: '/admin/tenants', labelKey: 'sections.tenants', icon: Building2 },
  { path: '/admin/users', labelKey: 'sections.users', icon: Server },
  { path: '/admin/sales', labelKey: 'sections.sales', icon: CreditCard },
  { path: '/admin/audit', labelKey: 'sections.audit', icon: ScrollText },
  { path: '/admin/llm', labelKey: 'sections.llm', icon: BrainCircuit },
]
