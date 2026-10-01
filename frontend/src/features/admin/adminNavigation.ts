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
  LifeBuoy,
  RadioTower,
  ScrollText,
  Server,
  BrainCircuit,
  BadgeDollarSign,
  Activity,
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
 * Va de lo general a lo concreto: primero el resumen, luego las seis secciones. Es el
 * orden en el que un operador de plataforma hace su ronda: mira cómo va, mira a quién,
 * mira a la gente, mira lo que se vendió, mira qué se tocó, ajusta los modelos y por
 * último atiende la cola de soporte.
 *
 * **Precios va entre Ventas y Auditoría, y no al final, a propósito.** Es la única sección
 * desde la que se **escribe** lo que la plataforma cobra, y va pegada a la de ventas porque es
 * la otra mitad de la misma pregunta: cuánto se vendió y a qué precio. Separarla al final —
 *que es donde se pondría cualquier cosa nueva por no estropear el orden— la escondería detrás
 * de cuatro secciones de solo lectura, que es exactamente donde se esconde una herramienta que
 * cambia la tarifa de todos los clientes a la vez. Va antes que Auditoría porque el motivo que
 * deja el cambio se lee después, en el histórico de la propia sección.
 *
 * **Operaciones va segunda, justo después del resumen, y no al final.** Es la sección que más se
 * usa cuando algo va mal: si hay un proceso colgado, es lo primero que se abre, y esconderlo
 * detrás de cuatro secciones de informe obliga a recorrerlas todas para llegar. Va antes que
 * Tenants porque Tenants y Usuarios son de administración —se tocan una vez al mes— y Operaciones
 * es de guardia: se abre cuando suena el aviso de un watchdog.
 *
 * Y va antes que Precios a propósito, porque las dos tocan dinero y el orden de urgencia es
 * inverso: primero desatascar lo que está colgado, después decidir cuánto cuesta.
 *
 * **Agentes va junto a Modelos LLM**, porque las dos son infraestructura de ejecución —qué motor
 * corre y quién lo dispara— y se revisan juntas al cambiar de proveedor.
 *
 * ## Por que estaba la ruta y no la entrada del menú
 *
 * ## Por que faltaba esta entrada y no era solo cosmetics
 *
 * Porque la ruta `/admin/agents` existía, la traducción existía y la entrada del array no. Una
 * sección alcanzable solo escribiendo la URL a mano no es una sección: es un callejón. Y era
 * exactamente el olvido que la prueba de sangrías no cazaba, porque esa prueba comprueba que la
 * ruta cuelgue de `/admin`, no que tenga entrada.
 *
 * Tickets va al final y no antes de *Venta* a propósito: la cola de soporte es trabajo en
 * curso que hay que atender **hoy**, y la venta es una decisión que se puede revisar
 * mañana. Poner la cola en medio obligaría a saltarándosela cada día.
 */
export const adminNavigation: readonly AdminNavigationItem[] = [
  { path: '/admin', labelKey: 'sections.overview', icon: BarChart3, end: true },
  { path: '/admin/operations', labelKey: 'sections.operations', icon: Activity },
  { path: '/admin/tenants', labelKey: 'sections.tenants', icon: Building2 },
  { path: '/admin/users', labelKey: 'sections.users', icon: Server },
  { path: '/admin/sales', labelKey: 'sections.sales', icon: CreditCard },
  { path: '/admin/pricing', labelKey: 'sections.pricing', icon: BadgeDollarSign },
  { path: '/admin/audit', labelKey: 'sections.audit', icon: ScrollText },
  { path: '/admin/agents', labelKey: 'sections.agents', icon: RadioTower },
  { path: '/admin/llm', labelKey: 'sections.llm', icon: BrainCircuit },
  { path: '/admin/tickets', labelKey: 'sections.tickets', icon: LifeBuoy },
]
