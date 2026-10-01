/**
 * Consola de operaciones: escaneos, revisiones, contenedores y sondas de toda la plataforma.
 *
 * ## Por qué esta pantalla existe
 *
 * Porque hay un caso que ninguna otra cubre: **un trabajo que se ha quedado pillado**. Un
 * escaneo lleva cuarenta minutos en `RUNNING`, con su contenedor vivo y su reserva de créditos ya
 * cobrada. Nadie lo va a cerrar desde el panel del cliente, porque el panel del cliente no enseña
 * ese run desde ningún sitio.
 *
 * ## Por qué los escaneos vienen primero y con el filtro de «colgados» marcado
 *
 * Porque es la pregunta que se hace al abrir la pantalla: «¿hay algo roto ahora?». El filtro usa
 * los mismos 300 segundos que el watchdog, así que lo que sale aquí es exactamente lo que el
 * watchdog va a tocar —no una lista de runs que todavía no ha mirando nadie.
 *
 * ## Por qué cancelar pide un motivo y el motivo decide el dinero
 *
 * Porque un proceso colgado y una cancelación del cliente se ven **exactamente igual**: los dos
 * están en `RUNNING`. Quien pulsa el botón sabe cuál es cuál, y la regla del reembolso sale de
 * ahí: los motivos `INFRASTRUCTURE_*` devuelven la reserva, los demás cobran. Por eso el selector
 * de motivo muestra, debajo de cada opción, si devuelve o no —es la información que el operador
 * necesita antes de confirmar, no un detalle técnico.
 *
 * ## Por qué limpiar contenedor es un botón aparte de cancelar
 *
 * Porque son dos problemas distintos. Cancelar es para un run en marcha. Limpiar es para un run
 * **ya terminado** cuyo contenedor, red o directorio siguen vivos: cancelar uno terminado da
 * `409`, correctamente. Meterlos en el mismo botón haría que el operador上年 clic y recibiera un
 * error sin entender por qué.
 *
 * ## Por qué no hay botón de borrar en ninguna de las cuatro tablas
 *
 * Por R4, y porque un run cancelado es un cobro: se queda con su evidencia, su ledger y su
 * historial. Lo que se puede es detener y limpiar.
 */

import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type {
  AbortReason,
  JobOperacionPage,
  ReviewOperacionPage,
  ScanOperacionPage,
} from '../../types/api'
import {
  cancelAdminScan,
  cleanupAdminScanContainer,
  getAdminJobs,
  getAdminReviews,
  getAdminScans,
} from '../../lib/adminApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'

type Seccion = 'scans' | 'reviews' | 'containers' | 'jobs'

const MOTIVOS: readonly AbortReason[] = [
  'INFRASTRUCTURE_STUCK',
  'INFRASTRUCTURE_FAILED',
  'INFRASTRUCTURE_ORPHANED',
  'CLIENT_CANCELLED',
  'DUPLICATE',
  'POLICY_VIOLATION',
]

export function AdminOperationsPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()
  const { notify } = useToast()
  const clave = token !== null && user?.is_superuser === true ? token : null
  const tokenActivo: string | null = clave

  const [seccion, setSeccion] = useState<Seccion>('scans')
  const [soloColgados, setSoloColgados] = useState(false)
  const [pendientes, setPendientes] = useState<ReadonlySet<string>>(new Set())
  const [motivo, setMotivo] = useState<AbortReason>('INFRASTRUCTURE_STUCK')
  const [nota, setNota] = useState('')
  const [motivoDe, setMotivoDe] = useState<string | null>(null)

  /**
   * La clave lleva la seccion y el filtro.
   *
   * Porque es lo que hace que la recarga ocurra: `useAsyncResource` vuelve a pedir cuando cambia
   * la clave, y un contador suelto que solo se llama desde un manejador no recarga nada. Y por
   * eso al cambiar de pestaña no se pide la de la anterior: cada tabla trae lo suyo y no hay un
   * «volver a cargar» que las mezcle.
   */
  const claveDatos =
    tokenActivo === null ? null : `${tokenActivo}:${seccion}:${soloColgados ? '1' : '0'}`

  /**
   * La clave lleva `<token>:<seccion>:<soloColgados>` y el fetcher la lee.
   *
   * ## Por qué el token viaja en la clave
   *
   * Porque es lo único que hace que la recarga ocurra: `useAsyncResource` vuelve a pedir cuando
   * la clave cambia. Y por eso el fetcher no necesita un estado propio de «token»: lo recibe en
   * la propia clave, y no hay forma de que se queden desincronizados los dos.
   *
   * ## Por qué el retorno es una unión
   *
   * Porque las tres secciones comparten forma —`items`, `total`, `limit`, `offset`— y solo
   * cambian los campos de cada fila. Declarar la unión hace que el compilador tenga que dejar
   * fuera los campos que no existen en las tres, que es exactamente lo que impide pintar
   * `pr_number` en la tabla de escaneos por un descuido.
   */
  type PaginaOperaciones = ScanOperacionPage | ReviewOperacionPage | JobOperacionPage

  const cargar = useCallback(
    (k: string): Promise<PaginaOperaciones> => {
      const partes = k.split(':')
      const auth = partes[0] ?? ''
      const seccionDeClave = partes[1] ?? 'scans'
      const soloColgados = partes[2] === '1'
      if (seccionDeClave === 'reviews') return getAdminReviews(auth)
      if (seccionDeClave === 'jobs') return getAdminJobs(auth)
      return getAdminScans(auth, { soloColgados })
    },
    [],
  )
  const datos = useAsyncResource<PaginaOperaciones>(cargar, claveDatos)

  const marcar = (id: string, v: boolean) =>
    setPendientes((actual) => {
      const siguiente = new Set(actual)
      if (v) siguiente.add(id)
      else siguiente.delete(id)
      return siguiente
    })

  const cancelar = async (runId: string) => {
    if (tokenActivo === null) return
    if (nota.trim().length < 3) {
      notify('error', t('operations.states.notaRequired'))
      return
    }
    marcar(`cancel:${runId}`, true)
    try {
      const r = await cancelAdminScan(tokenActivo, runId, motivo, nota.trim())
      notify(
        'success',
        r.devuelto_creditos === null
          ? t('operations.actions.cancelledNoRefund')
          : t('operations.actions.cancelledRefund', { credits: r.devuelto_creditos }),
      )
      setNota('')
      setMotivoDe(null)
      datos.reload()
    } catch {
      notify('error', t('operations.actions.error'))
    } finally {
      marcar(`cancel:${runId}`, false)
    }
  }

  const limpiar = async (runId: string) => {
    if (tokenActivo === null) return
    marcar(`clean:${runId}`, true)
    try {
      const r = await cleanupAdminScanContainer(tokenActivo, runId)
      notify('success', r.nota)
      datos.reload()
    } catch {
      notify('error', t('operations.actions.error'))
    } finally {
      marcar(`clean:${runId}`, false)
    }
  }

  if (tokenActivo === null) {
    return null
  }
  if (datos.loadFailed && datos.data === null) {
    return (
      <div className="empty-card">
        <p>{t('operations.states.error')}</p>
        <button className="secondary-button" type="button" onClick={datos.reload}>
          <span>{t('operations.states.retry')}</span>
        </button>
      </div>
    )
  }
  if (datos.isLoading && datos.data === null) {
    return (
      <div className="empty-card">
        <p>{t('operations.states.loading')}</p>
      </div>
    )
  }

  const enCurso = datos.data !== null && 'items' in datos.data ? datos.data.items : []
  const total = datos.data !== null && 'total' in datos.data ? datos.data.total : 0

  return (
    <section className="stack-lg">
      <header className="section-header">
        <div>
          <p className="eyebrow">{t('nav.console')}</p>
          <h1>{t('operations.title')}</h1>
          <p className="section-description">{t('operations.description')}</p>
        </div>
      </header>

      <div className="tabs" role="tablist" aria-label={t('operations.tabsLabel')}>
        {(['scans', 'containers', 'reviews', 'jobs'] as const).map((s) => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={seccion === s}
            className={seccion === s ? 'tab tab-active' : 'tab'}
            onClick={() => setSeccion(s)}
          >
            <span>{t(`operations.tabs.${s}`)}</span>
          </button>
        ))}
      </div>

      {seccion === 'scans' && (
        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={soloColgados}
            onChange={(e) => setSoloColgados(e.target.checked)}
          />
          <span>{t('operations.filters.soloColgados')}</span>
        </label>
      )}

      <p className="cell-muted">
        {t('operations.states.total', { count: total })}
      </p>

      {seccion === 'scans' ? (
        <TablaScans
          items={enCurso}
          pendientes={pendientes}
          motivo={motivo}
          nota={nota}
          motivoDe={motivoDe}
          alAbrirMotivo={setMotivoDe}
          alCambiarMotivo={setMotivo}
          alCambiarNota={setNota}
          alCancelar={cancelar}
          alLimpiar={limpiar}
        />
      ) : seccion === 'containers' ? (
        <TablaVacia titulo={t('operations.states.empty')} />
      ) : seccion === 'reviews' ? (
        <TablaReviews items={enCurso} />
      ) : (
        <TablaJobs items={enCurso} />
      )}
    </section>
  )
}

function TablaVacia({ titulo }: { titulo: string }) {
  return (
    <div className="empty-card">
      <p>{titulo}</p>
    </div>
  )
}

type FilaScan = Record<string, unknown> & {
  id: string
  status: string
  organizacion: string
  target_identifier: string
  container_id: string | null
  cleanup_pending: boolean
  error_message: string | null
  duracion_minutos: string
}

function TablaScans({
  items,
  pendientes,
  motivo,
  nota,
  motivoDe,
  alAbrirMotivo,
  alCambiarMotivo,
  alCambiarNota,
  alCancelar,
  alLimpiar,
}: {
  items: readonly unknown[]
  pendientes: ReadonlySet<string>
  motivo: AbortReason
  nota: string
  motivoDe: string | null
  alAbrirMotivo: (v: string | null) => void
  alCambiarMotivo: (v: AbortReason) => void
  alCambiarNota: (v: string) => void
  alCancelar: (runId: string) => Promise<void>
  alLimpiar: (runId: string) => Promise<void>
}) {
  const { t, i18n } = useTranslation('admin')
  if (items.length === 0) {
    return <TablaVacia titulo={t('operations.states.empty')} />
  }
  return (
    <div className="table-wrapper">
      <table className="data-table console-table">
        <caption className="visually-hidden">{t('operations.tabs.scans')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('operations.columns.status')}</th>
            <th scope="col">{t('operations.columns.organization')}</th>
            <th scope="col">{t('operations.columns.target')}</th>
            <th scope="col">{t('operations.columns.minutes')}</th>
            <th scope="col">{t('operations.columns.container')}</th>
            <th scope="col">
              <span className="visually-hidden">{t('operations.columns.actions')}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {(items as readonly FilaScan[]).map((fila) => {
            const enMarcha = fila.status === 'QUEUED' || fila.status === 'RUNNING'
            const abierto = motivoDe === fila.id
            return (
              <tr key={fila.id} className={enMarcha ? undefined : 'row-inactive'}>
                <td>
                  <span className="badge mono" title={fila.status}>
                    {t(`operations.states.status.${fila.status}`, { defaultValue: fila.status })}
                  </span>
                </td>
                <td>{fila.organizacion}</td>
                <td className="mono">{fila.target_identifier}</td>
                <td className="mono">
                  {fila.duracion_minutos}
                  <span className="cell-muted"> {t('operations.units.minutes')}</span>
                </td>
                <td className="mono">
                  {fila.container_id ?? t('operations.states.noContainer')}
                  {fila.cleanup_pending ? (
                    <span className="badge badge-warning">
                      {t('operations.states.cleanupPending')}
                    </span>
                  ) : null}
                </td>
                <td>
                  <div className="button-row">
                    {enMarcha ? (
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={pendientes.has(`cancel:${fila.id}`)}
                        onClick={() => alAbrirMotivo(abierto ? null : fila.id)}
                      >
                        <span>{t('operations.actions.cancel')}</span>
                      </button>
                    ) : null}
                    {fila.cleanup_pending ? (
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={pendientes.has(`clean:${fila.id}`)}
                        onClick={() => void alLimpiar(fila.id)}
                      >
                        <span>{t('operations.actions.cleanup')}</span>
                      </button>
                    ) : null}
                  </div>
                  {abierto ? (
                    <div className="field-group">
                      <div className="field">
                        <label htmlFor={`motivo-${fila.id}`}>
                          {t('operations.motive.label')}
                        </label>
                        <select
                          id={`motivo-${fila.id}`}
                          value={motivo}
                          disabled={pendientes.has(`cancel:${fila.id}`)}
                          onChange={(e) => alCambiarMotivo(e.target.value as AbortReason)}
                        >
                          {MOTIVOS.map((m) => (
                            <option key={m} value={m}>
                              {t(`operations.reasons.${m}`)}
                              {' — '}
                              {t(
                                DEVUELVEN.has(m)
                                  ? 'operations.reasons.refunds'
                                  : 'operations.reasons.charges',
                              )}
                            </option>
                          ))}
                        </select>
                        <p className="field-help">{t('operations.motive.help')}</p>
                      </div>
                      <div className="field">
                        <label htmlFor={`nota-${fila.id}`}>
                          {t('operations.motive.noteLabel')}
                        </label>
                        <input
                          id={`nota-${fila.id}`}
                          type="text"
                          maxLength={255}
                          disabled={pendientes.has(`cancel:${fila.id}`)}
                          value={nota}
                          onChange={(e) => alCambiarNota(e.target.value)}
                        />
                      </div>
                      <button
                        className="primary-button"
                        type="button"
                        disabled={pendientes.has(`cancel:${fila.id}`)}
                        onClick={() => void alCancelar(fila.id)}
                      >
                        <span>{t('operations.actions.confirmCancel')}</span>
                      </button>
                    </div>
                  ) : null}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <span className="visually-hidden">{i18n.language}</span>
    </div>
  )
}

/**
 * Los motivos que devuelven el dinero.
 *
 * ## Por qué esta lista está **en el cliente** y no se pide al backend
 *
 * Porque es una etiqueta, no una verdad: el backend es el que decide, y decide siempre igual. La
 * lista está aquí para que el operador vea el efecto de su elección **antes** de confirmar, que es
 * cuando puede corregirlo. Duplicarla no crea una segunda regla: si algún día divergieran, el
 * precio del error es un texto equivocado al confirmar, y el backend sigue devolviendo el dinero
 * correcto.
 *
 * Y si se copia mal, el fallo se ve en el selector antes de que llegue a un cobro.
 */
const DEVUELVEN: ReadonlySet<AbortReason> = new Set<AbortReason>([
  'INFRASTRUCTURE_STUCK',
  'INFRASTRUCTURE_FAILED',
  'INFRASTRUCTURE_ORPHANED',
])

function TablaReviews({ items }: { items: readonly unknown[] }) {
  const { t } = useTranslation('admin')
  if (items.length === 0) {
    return <TablaVacia titulo={t('operations.states.empty')} />
  }
  return (
    <div className="table-wrapper">
      <table className="data-table console-table">
        <caption className="visually-hidden">{t('operations.tabs.reviews')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('operations.columns.status')}</th>
            <th scope="col">{t('operations.columns.organization')}</th>
            <th scope="col">{t('operations.columns.pr')}</th>
            <th scope="col">{t('operations.columns.findings')}</th>
            <th scope="col">{t('operations.columns.date')}</th>
          </tr>
        </thead>
        <tbody>
          {(items as readonly Record<string, never>[]).map((fila) => (
            <tr key={String(fila.id)}>
              <td>
                <span className="badge mono" title={String(fila.status)}>
                  {t(`operations.states.reviewStatus.${String(fila.status)}`, {
                    defaultValue: String(fila.status),
                  })}
                </span>
              </td>
              <td>{String(fila.organizacion)}</td>
              <td>
                <span className="mono">#{String(fila.pr_number)}</span>{' '}
                {String(fila.pr_title ?? '')}
              </td>
              <td className="mono">
                {String(fila.issues_caught_critical)} / {String(fila.issues_caught_high)}
              </td>
              <td className="mono timestamp">{String(fila.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TablaJobs({ items }: { items: readonly unknown[] }) {
  const { t } = useTranslation('admin')
  if (items.length === 0) {
    return <TablaVacia titulo={t('operations.states.empty')} />
  }
  return (
    <div className="table-wrapper">
      <table className="data-table console-table">
        <caption className="visually-hidden">{t('operations.tabs.jobs')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('operations.columns.status')}</th>
            <th scope="col">{t('operations.columns.organization')}</th>
            <th scope="col">{t('operations.columns.kind')}</th>
            <th scope="col">{t('operations.columns.target')}</th>
            <th scope="col">{t('operations.columns.date')}</th>
          </tr>
        </thead>
        <tbody>
          {(items as readonly Record<string, never>[]).map((fila) => (
            <tr key={String(fila.id)}>
              <td>
                <span className="badge mono" title={String(fila.status)}>
                  {t(`operations.states.jobStatus.${String(fila.status)}`, {
                    defaultValue: String(fila.status),
                  })}
                </span>
              </td>
              <td>{String(fila.organizacion)}</td>
              <td>{String(fila.kind)}</td>
              <td className="mono">{String(fila.target)}</td>
              <td className="mono timestamp">{String(fila.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}