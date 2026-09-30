/**
 * Tipos de los agentes de escaneo, tal y como los devuelve la API.
 *
 * ## Por qué `result` es `Record<string, unknown>` y no una interfaz
 *
 * Porque su forma depende del `kind` del trabajo, y un tipo que lasmezcla sería mentir: no hay
 * un `paquetes` en un escaneo de red ni un `hosts` en uno de contenedor. Declararlo como objeto
 * genérico obliga a comprobar en la vista de detalle, que es donde se mira, y a que el
 * compilador no dé por hecho un campo que no está.
 *
 * Y la alternativa —un tipo que reúna las dos formas con campos opcionales— es peor, porque
 * haría que `trabajo.result.paquetes` compilara en un escaneo de red y devolviera `undefined`
 * sin que nadie lo note.
 */

/** Lo que devuelve `GET /api/v1/agents/{id}/jobs` y `GET /api/v1/agents/jobs/{id}`. */
export interface AgentJob {
  id: string
  kind: AgentJobKind
  target: string
  status: AgentJobStatus
  priority_order: number
  agent_id: string | null
  agent_name: string | null
  requested_by: string | null
  attempt_count: number
  claimed_at: string | null
  completed_at: string | null
  error_message: string | null
  /**
   * Que el resultado siga coincidiendo con su huella.
   *
   * Viene del servidor y se recalcula **al leer**: la plataforma guarda el SHA-256 del JSON y lo
   * vuelve a calcular cuando lo sirve. Por eso el campo no significa "el agente lo mandó bien"
   * sino "nadie lo cambió después".
   */
  evidence_intact: boolean
  created_at: string
  /** Solo en el detalle. La forma depende de `kind`. */
  result?: Record<string, unknown> | null
}

export type AgentJobKind = 'CONTAINER_SCAN' | 'NETWORK_SCAN'

export type AgentJobStatus = 'QUEUED' | 'CLAIMED' | 'RUNNING' | 'COMPLETED' | 'FAILED'

export type AgentStatus = 'ACTIVE' | 'REVOKED'

/** Un agente dado de alta, sin su token. */
/**
 * El sistema para el que el operador dijo desplegar el agente, a diferencia de
 * `platform_hint`, que lo mide el agente cuando se conecta.
 *
 * ## Por qué `desconocido` es un valor y no `undefined`
 *
 * Porque en la base de datos es `NOT NULL` con valor por defecto, y un `undefined` en el
 * contrato dejaría tres estados donde la fila solo tiene dos: declarado y medido se
 * distinguen, declarado y no-declarado también, y `undefined` los mezclaría. Un tipo que no
 * puede representar lo que la base puede contener acaba mintiendo en la primera fila antigua.
 */
export type SistemaObjetivo = 'linux' | 'windows' | 'macos' | 'desconocido'

export interface ScannerAgent {
  id: string
  name: string
  token_prefix: string
  status: AgentStatus
  agent_version: string | null
  platform_hint: string | null
  // Deliberadamente **ancha**, no `SistemaObjetivo`.
  //
  // ## Por qué
  //
  // Porque un backend más antiguo que el campo manda una respuesta sin él, y un tipo estricto
  // convertiría esa ausencia en un `undefined` silencioso que arrive hasta la clave de
  // traducción. Declarando el campo como `SistemaObjetivo | string | null | undefined`, el
  // `undefined` es parte del contrato y `normalizaSistema` —que es donde vive la decisión— lo ve
  // siempre. Un tipo estricto aquí no protege de nada: solo oculta el dato que falta.
  sistema_objetivo: SistemaObjetivo | string | null | undefined
  enrolled_at: string
  last_seen_at: string | null
  revoked_at: string | null
  revoked_reason: string | null
}

/** La respuesta del alta, que además trae el token **una sola vez**. */
export interface AgentEnrolled extends ScannerAgent {
  token: string
}

export interface AgentPage {
  items: ScannerAgent[]
  total: number
  limit: number
  offset: number
}

export interface AgentJobPage {
  items: AgentJob[]
  total: number
  limit: number
  offset: number
}

export interface AgentJobRequest {
  kind: AgentJobKind
  target: string
  priority_order?: number
}

/** Un paquete del inventario de una imagen. */
export interface AgentPackage {
  name: string
  version: string
  ecosystem: string
  license?: string
  arch?: string
}

/**
 * Los números de la cabecera de una pantalla.
 *
 * ## Por qué trae los dos mundos y cada pantalla lee el suyo
 *
 * porque el servidor los calcula en una sola pasada y la respuesta es la misma forma para las
 * dos. Filtrar en el cliente significaría que `/networks` trae el inventario de cuatrocientos
 * paquetes de una imagen para no dibujarlo nunca. Por eso hay dos rutas en el servidor y el
 * filtro va **allí**, y aquí los dos mundos conviven porque el tipo es el mismo.
 *
 * Y el hecho de que un campo sea `0` en la pantalla equivocada no es un error: es que ese campo
 * no cuenta para esa pantalla. La lectura de cada uno esta en la pagina que lo usa.
 */
export interface AgentSummary {
  total_agentes: number
  /** Agentes que se han identificado en la ventana reciente. */
  vivos: number
  /** La ventana, en segundos, para poder decir «en los ultimos cinco minutos». */
  ventana_de_vida: number

  total_imagenes: number
  total_paquetes: number
  total_capas: number
  paquetes_por_ecosistema: Record<string, number>
  imagenes_inventariadas: number
  imagenes_sin_inventario: number

  total_redes: number
  total_hosts: number
  total_puertos: number
  puertos_por_numero: Record<string, number>
  direcciones_analizadas: number

  /** Los cinco estados siempre presentes, aunque valgan cero. */
  por_estado: Record<string, number>
  por_dia: ScanCountByDay[]
}

export interface ScanCountByDay {
  dia: string
  escaneos: number
  terminados: number
  fallidos: number
}

/** Un puerto abierto que el agente encontró en un host. */
export interface AgentOpenPort {
  puerto: number
  servicio: string | null
  banner: string | null
}

/** Un host que respondió al barrido. */
export interface AgentHost {
  ip: string
  puertos: AgentOpenPort[]
}

/**
 * El resultado de un escaneo de contenedor.
 *
 * Los dos campos de vulnerabilidades están a propósito, y ser `null` es el dato: el proyecto
 * tiene `cve_records`, pero esa tabla no guarda qué paquetes afecta cada CVE, así que no hay
 * nada local contra lo que cruzar. Un número aquí sería inventado, y la razón viene escrita
 * para que la vista pueda enseñarla en vez de dejar un `—` que parece un fallo.
 *
 * Cada motivo viene **con su código y con su texto**. El texto es la evidencia, y el código es
 * lo que permite traducirla. Los dos campos son opcionales porque un resultado guardado por un
 * agente anterior al código no lo tiene, y ese motivo se enseña tal cual en vez de desaparecer.
 */
export interface ContainerScanResult {
  referencia: string
  digest: string
  sistema_operativo: string
  version_sistema_operativo: string | null
  arquitectura: string | null
  total_capas: number
  capas_leidas: number
  bytes_capas: number
  paquetes: AgentPackage[]
  total_paquetes: number
  vulnerabilidades_por_paquete: null
  motivo_sin_vulnerabilidades_por_paquete: string
  codigo_sin_vulnerabilidades_por_paquete?: string
  formato_no_leido?: string
  codigo_formato_no_leido?: string
}

/** El resultado de un escaneo de red. */
export interface NetworkScanResult {
  cidr: string
  direcciones_analizadas: number
  hosts_con_puertos: number
  puertos_analizados: number[]
  hosts: AgentHost[]
  durata_segundos: number
  vulnerabilidades_por_puerto: null
  motivo_sin_vulnerabilidades_por_puerto: string
  codigo_sin_vulnerabilidades_por_puerto?: string
  direcciones_recortadas?: boolean
  motivo_recorte?: string
  codigo_recorte?: string
  /** Las direcciones que tenía el prefijo antes de recortarlo. */
  total_en_el_prefijo?: number
}
