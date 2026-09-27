/**
 * Tipos de los endpoints de Ajustes del workspace activo.
 *
 * ## Por qué el `slug` no aparece en `OrganizationUpdate`
 *
 * No es que se olvide validarlo: el esquema del backend lo rechaza con `extra="forbid"`, y
 * es una decisión deliberada. El `slug` es la dirección pública del workspace y aparece en
 * enlaces ya compartidos y en el botón de compartir; renombrarlo los partiría. El nombre
 * cambia, la dirección no.
 *
 * Como el cliente no puede mandarlo, tampoco lo declara aquí. Un tipo que admitiera un campo
 * que el servidor rechaza sería una invitación a descubrirlo con un `422`.
 */

export type WorkspaceRole = 'admin' | 'member'

export interface OrganizationUpdatePayload {
  name: string
}

export interface WorkspaceMember {
  user_id: string
  email: string
  full_name: string
  role: WorkspaceRole
  /** Fecha de alta de la membresía activa, en ISO. */
  joined_at: string
  is_active: boolean
}

export interface WorkspaceMemberList {
  items: WorkspaceMember[]
  total: number
}

export interface MemberRolePayload {
  role: WorkspaceRole
}
