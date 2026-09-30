/**
 * Cliente de la API de agentes de escaneo.
 *
 * ## Por qué estas funciones **no** son las de `lib/api.ts`
 *
 * Porque `api.ts` habla con la API de **una persona**: manda `Authorization: Bearer` con el token
 * de sesión y `X-Organization-Id` con el tenant. Y estas son las mismas rutas que ve el panel,
 * pero con una diferencia que no es de forma sino de fondo: quien llama es una persona del
 * tenant, no un agente. Compartir el cliente haría que añadir un endpoint para el agente
 *ruitment fuera un detalle de un archivo de cuatro mil líneas, y ese es exactamente el sitio
 * donde una credencial acaba donde no debe.
 *
 * Lo que sí se comparte es la forma de la petición —mismas cabeceras, mismo `fetch`, mismo
 * manoeuvre de errores— para que el comportamiento sea idéntico y no haya dos reglas de error
 * conviviendo en la aplicación.
 *
 * ## Por qué el token del agente **nunca** pasa por aquí
 *
 * Porque esta pantalla es de administración de agentes, no el agente. El token se ve una vez,
 * en el alta, y el cliente que lo consume es `agent/fenix_agent`, que habla por su cuenta. Si un
 * token de agente llegara a este módulo, significaría que alguien ha metido la credencial del
 * proceso de la red del cliente en el navegador de un administrador, y eso no tiene ningún
 * escenario legítimo.
 */

import type {
  AgentEnrolled,
  AgentJob,
  AgentJobKind,
  AgentJobPage,
  AgentJobRequest,
  AgentJobStatus,
  AgentPage,
  AgentSummary,
  ScannerAgent,
} from '../types/agents'

/** Error de la API, con el detalle del servidor para poder mostrarlo. */
export class AgentsApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'AgentsApiError'
    this.status = status
  }
}

function cabeceras(token: string, organizationId: string, conCuerpo: boolean): HeadersInit {
  return {
    Authorization: `Bearer ${token}`,
    'X-Organization-Id': organizationId,
    Accept: 'application/json',
    ...(conCuerpo ? { 'Content-Type': 'application/json' } : {}),
  }
}

async function pedir<T>(ruta: string, init: RequestInit): Promise<T> {
  const respuesta = await fetch(ruta, init)
  if (!respuesta.ok) {
    let mensaje = `HTTP ${respuesta.status}`
    try {
      const cuerpo = (await respuesta.json()) as { detail?: unknown }
      if (typeof cuerpo.detail === 'string') {
        mensaje = cuerpo.detail
      }
    } catch {
      // Un `detail` que no es JSON no es motivo para perder el status, que es lo que el
      // operador necesita ver cuando algo falla.
    }
    throw new AgentsApiError(respuesta.status, mensaje)
  }
  if (respuesta.status === 204) {
    return undefined as T
  }
  return (await respuesta.json()) as T
}

export function listAgents(token: string, organizationId: string): Promise<AgentPage> {
  return pedir<AgentPage>('/api/v1/agents', {
    method: 'GET',
    headers: cabeceras(token, organizationId, false),
  })
}

export function getAgent(
  token: string,
  organizationId: string,
  agentId: string
): Promise<ScannerAgent> {
  return pedir<ScannerAgent>(`/api/v1/agents/${agentId}`, {
    method: 'GET',
    headers: cabeceras(token, organizationId, false),
  })
}

/**
 * El sistema para el que se despliega el agente.
 *
 * ## Por qué se reexporta desde aquí y no se duplica
 *
 * Porque lo consumen tres sitios —la API, el modal y el panel— y un tipo declarado dos veces es
 * un tipo que diverge. La guía es quien decide qué hacer con cada valor, así que el tipo vive
 * con ella y la API lo reexporta.
 */
import type { SistemaObjetivo } from '../types/agents'

export type { SistemaObjetivo }


export function createAgent(
  token: string,
  organizationId: string,
  payload: {
    name: string
    agent_version?: string
    platform_hint?: string
    sistema_objetivo?: SistemaObjetivo
  }
): Promise<AgentEnrolled> {
  return pedir<AgentEnrolled>(
    '/api/v1/agents',
    {
      method: 'POST',
      headers: cabeceras(token, organizationId, true),
      body: JSON.stringify(payload),
    }
  )
}

export function revokeAgent(
  token: string,
  organizationId: string,
  agentId: string,
  motivo?: string
): Promise<ScannerAgent> {
  const consulta = motivo ? `?motivo=${encodeURIComponent(motivo)}` : ''
  return pedir<ScannerAgent>(`/api/v1/agents/${agentId}/revoke${consulta}`, {
    method: 'POST',
    headers: cabeceras(token, organizationId, false),
  })
}

export interface AgentJobQuery {
  limit?: number
  offset?: number
  estado?: AgentJobStatus
  tipo?: AgentJobKind
}

export function listAgentJobs(
  token: string,
  organizationId: string,
  query: AgentJobQuery = {}
): Promise<AgentJobPage> {
  const parametros = new URLSearchParams()
  if (query.limit !== undefined) parametros.set('limit', String(query.limit))
  if (query.offset !== undefined) parametros.set('offset', String(query.offset))
  if (query.estado) parametros.set('estado', query.estado)
  if (query.tipo) parametros.set('tipo', query.tipo)
  const sufijo = parametros.toString()
  return pedir<AgentJobPage>(`/api/v1/agents/jobs${sufijo ? `?${sufijo}` : ''}`, {
    method: 'GET',
    headers: cabeceras(token, organizationId, false),
  })
}

export function getAgentJob(
  token: string,
  organizationId: string,
  jobId: string
): Promise<AgentJob> {
  return pedir<AgentJob>(`/api/v1/agents/jobs/${jobId}`, {
    method: 'GET',
    headers: cabeceras(token, organizationId, false),
  })
}

export function createAgentJob(
  token: string,
  organizationId: string,
  payload: AgentJobRequest
): Promise<AgentJob> {
  return pedir<AgentJob>(
    '/api/v1/agents/jobs',
    {
      method: 'POST',
      headers: cabeceras(token, organizationId, true),
      body: JSON.stringify(payload),
    }
  )
}

export function requeueExpiredJobs(
  token: string,
  organizationId: string
): Promise<{ requeued: number }> {
  return pedir<{ requeued: number }>('/api/v1/agents/jobs/requeue-expired', {
    method: 'POST',
    headers: cabeceras(token, organizationId, false),
  })
}

/**
 * Los numeros de la cabecera de una pantalla.
 *
 * Hay dos rutas y no una con un parametro porque `/containers` y `/networks` son pantallas
 * distintas con tablas distintas. Traer el inventario de cuatrocientos paquetes para dibujar un
 * grafico de puertos seria enviar cientos de kilobytes que nadie pidio, asi que el filtro va
 * en la ruta y el recorte en el servidor.
 */
export function getContainersSummary(
  token: string,
  organizationId: string
): Promise<AgentSummary> {
  return pedir<AgentSummary>('/api/v1/agents/summary', {
    method: 'GET',
    headers: cabeceras(token, organizationId, false),
  })
}

export function getNetworksSummary(
  token: string,
  organizationId: string
): Promise<AgentSummary> {
  return pedir<AgentSummary>('/api/v1/agents/summary/networks', {
    method: 'GET',
    headers: cabeceras(token, organizationId, false),
  })
}
