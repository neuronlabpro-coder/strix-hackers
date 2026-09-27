/**
 * Cliente de los endpoints de Ajustes del workspace activo.
 *
 * ## Por qué ninguna función acepta un `organizationId` como parámetro de negocio
 *
 * Todas las rutas toman el tenant de la **cabecera** `X-Organization-Id`, que es lo que el
 * shell de cliente ya envía. No hay `organizationId` en ninguna URL ni en ningún cuerpo,
 * así que no hay ningún valor que el cliente pueda manipular para preguntar por otro
 * workspace. Es R3 resuelto por imposibilidad, no por validación.
 *
 * Por eso estas funciones reciben `organizationId` solo para ponerlo en la cabecera, igual
 * que el resto del cliente de la plataforma, y no para construir una ruta.
 */

import { API_BASE_URL } from '../config'
import type {
  MemberRolePayload,
  OrganizationUpdatePayload,
  WorkspaceMemberList,
} from '../types/workspace'
import { ApiError } from './api'

async function tenantRequest<T>(
  path: string,
  init: RequestInit,
  token: string,
  organizationId: string,
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Authorization', `Bearer ${token}`)
  // Solo se pone si hay valor. Mandar la cabecera **vacia** no es lo mismo que no mandarla:
  headers.set('X-Organization-Id', organizationId)

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status)
  }
  return (await response.json()) as T
}

/**
 * Renombra el workspace activo.
 *
 * El `slug` no se manda porque el esquema lo rechaza. Ver el tipo `OrganizationUpdate`.
 */
export function renameWorkspace(
  token: string,
  organizationId: string,
  name: string,
): Promise<{ name: string; slug: string }> {
  return tenantRequest<{ name: string; slug: string }>(
    '/api/v1/organizations/me',
    { method: 'PATCH', body: JSON.stringify({ name } satisfies OrganizationUpdatePayload) },
    token,
    organizationId,
  )
}

/**
 * Los miembros activos del workspace.
 *
 * Lo puede pedir cualquier miembro, no solo el admin: es la lista del equipo, no un
 * inventario de la empresa.
 */
export function getWorkspaceMembers(
  token: string,
  organizationId: string,
): Promise<WorkspaceMemberList> {
  return tenantRequest<WorkspaceMemberList>(
    '/api/v1/organizations/me/members',
    { method: 'GET' },
    token,
    organizationId,
  )
}

/**
 * Cambia el rol de un miembro.
 *
 * Devuelve `409` si el cambio dejaría el workspace sin ningún administrador. El error no es
 * un `403` porque la petición es legítima: lo que no permite es el estado, y se resuelve
 * invocando a otro admin.
 */
export function updateMemberRole(
  token: string,
  organizationId: string,
  userId: string,
  payload: MemberRolePayload,
): Promise<WorkspaceMemberList> {
  return tenantRequest<WorkspaceMemberList>(
    `/api/v1/organizations/me/members/${userId}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    token,
    organizationId,
  )
}

/**
 * Retira a un miembro.
 *
 * La fila **no** se borra: se desactiva. Por eso se devuelve la lista en vez de un `204`, y
 * por eso un segundo `DELETE` del mismo usuario da `404` y no `200` idempotente — la fila
 * existe pero ya no está, y desde el punto de vista de "miembros activos" no hay nada que
 * retirar dos veces.
 */
export function removeWorkspaceMember(
  token: string,
  organizationId: string,
  userId: string,
): Promise<WorkspaceMemberList> {
  return tenantRequest<WorkspaceMemberList>(
    `/api/v1/organizations/me/members/${userId}`,
    { method: 'DELETE' },
    token,
    organizationId,
  )
}

/**
 * Da de baja el workspace activo.
 *
 * Es la única función de este módulo que lleva el `organizationId` en la **ruta**, y no es
 * una inconsistencia: el `DELETE` está sobre `/organizations/{id}` y no sobre `/me`. El
 * servidor exige que el `id` de la URL coincida con el del contexto y devuelve `404` si no,
 * así que mandar un identificador distinto de la cabecera no baja el workspace equivocado:
 * no baja ninguno.
 *
 * ## Por qué un `404` no se considera error
 *
 * Porque la organización deja de existir en cuanto la baja se aplica, y cualquier petición
 * posterior con sus cabeceras devolverá `403` por no resolver contexto. Los dos códigos son
 * el resultado esperado de la operación, no un fallo de ella, y tratarlos como error haría
 * que el cliente creyera que la baja no se aplicó —cuando justamente sí— e intentara de
 * nuevo.
 *
 * La función devuelve `void` en lugar del informe de la baja: el panel navega fuera de
 * inmediato y el informe —revocaciones, cancelaciones, avisos— no tiene dónde mostrarse. El
 * rastro queda en la auditoría del servidor, que es donde se consulta.
 */
export async function deactivateWorkspace(
  token: string,
  organizationId: string,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/organizations/${organizationId}`, {
    method: 'DELETE',
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'X-Organization-Id': organizationId,
    },
  })
  if (!response.ok && response.status !== 404) {
    throw new ApiError(response.status)
  }
}
