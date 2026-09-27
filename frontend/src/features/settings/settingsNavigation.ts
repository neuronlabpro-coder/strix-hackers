/**
 * Navegación interna de Ajustes.
 *
 * ## Por qué es una lista aparte y no un trozo de `navigation.ts`
 *
 * El panel de cliente navega **dentro** de un workspace: cada entrada es una vista de sus
 * datos. Ajustes es un submenú **de una pantalla**, con cinco entradas que comparten un
 * sub-sidebar y un contenido centrado. Meterlas en `navigation.ts` las pondría al mismo
 * nivel que Pentests o Issues, que es un sitio distinto: nadie abandona el análisis de
 * vulnerabilidades para ir a cambiar el nombre del workspace, y el sub-sidebar de 220px
 * solo tiene sentido con pocas secciones.
 *
 * ## Por qué `requiresEnterprise` es un dato y no una comprobación dentro del componente
 *
 * El candado de Audit Logs no se decide aquí. Este archivo no sabe el plan del workspace, y
 * una función `puedeVer(item)` que lo consultara mezclaría navegación con estado de sesión.
 * La lista declara **qué** entradas tienen requisito, y el layout decide **si** este
 * workspace lo cumple. Añadir una entrada de pago es añadir una propiedad, no tocar la
 * lógica de renderizado.
 *
 * La comprobación real sigue estando en el servidor: `/audit` de SuperAdmin es lo único que
 * cruza tenants, y el resto de la vista de auditoría de un workspace lo filtra el propio
 * tenant. El candado es información previa, no permiso.
 */

import { LifeBuoy, Receipt, ScrollText, Settings2, Users, type LucideIcon } from 'lucide-react'

export interface SettingsNavItem {
  /** Ruta absoluta dentro de la aplicación. Nunca relativa: el submenú vive en dos niveles. */
  path: string
  /** Clave del namespace `settings`. Nunca un literal: R1. */
  labelKey: string
  icon: LucideIcon
  /**
   * Con `end`, `/settings` no se marca activo cuando se está en `/settings/general`.
   *
   * Hace falta porque `/settings` es el índice **y** un enlace. Sin `end`, estar en
   * `General` dejaría las dos entradas iluminadas a la vez, que es el síntoma clásico de un
   * submenú mal hecho.
   */
  end?: boolean
  /**
   * Si la entrada exige plan Enterprise. El layout le pone un candado y un aviso al
   * pulsarla en vez de dejarla entrar y esconder el contenido dentro.
   *
   * Se marca la entrada en vez de ocultarla porque "existe pero no lo puedes usar" y "no
   * existe" son información distinta: la primera le dice al cliente que existe un producto
   * que no compró, que es lo que hace que una oferta funcione.
   */
  requiresEnterprise?: boolean
}

/**
 * Las cinco entradas, en el orden en que se leen.
 *
 * Empieza por *General* porque es la que se abre casi siempre, y termina por *Help &
 * Support* porque es la única que no es una pantalla de administración sino una vía de
 * contacto. Members va antes que Billing porque es lo que cambia la estructura del equipo,
 * que es una decisión anterior a la económica.
 */
export const settingsNavigation: readonly SettingsNavItem[] = [
  { path: '/settings/general', labelKey: 'sections.general', icon: Settings2, end: true },
  {
    path: '/settings/audit-logs',
    labelKey: 'sections.auditLogs',
    icon: ScrollText,
    requiresEnterprise: true,
  },
  { path: '/settings/members', labelKey: 'sections.members', icon: Users },
  { path: '/settings/billing', labelKey: 'sections.billing', icon: Receipt },
  { path: '/settings/support', labelKey: 'sections.support', icon: LifeBuoy },
]
