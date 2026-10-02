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
 * `409`, correctamente. Meterlos en el mismo botón haría que el operador pulsara y recibiera un
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
  ContenedorOperacion,
  ContenedorPage,
  JobOperacion,
  JobOperacionPage,
  ReviewOperacion,
  ReviewOperacionPage,
  ScanOperacion,
  ScanOperacionPage,
} from '../../types/api'
import {
  cancelAdminScan,
  cleanupAdminScanContainer,
  getAdminContainers,
  getAdminJobs,
  getAdminReviews,
  getAdminScans,
} from '../../lib/adminApi'
import { formatDate } from '../../lib/format'
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
  type PaginaOperaciones =
    | ScanOperacionPage
    | ContenedorPage
    | ReviewOperacionPage
    | JobOperacionPage

  const cargar = useCallback(
    (k: string): Promise<PaginaOperaciones> => {
      const partes = k.split(':')
      const auth = partes[0] ?? ''
      const seccionDeClave = partes[1] ?? 'scans'
      const soloColgados = partes[2] === '1'
      if (seccionDeClave === 'containers') return getAdminContainers(auth)
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

  // El nombre `enCurso` viene de cuando esta pantalla solo tenía escaneos, y ya no es cierto:
  // cada pestaña trae su propia colección y aquí solo se lee la que devolvió el fetcher. Se
  // renombra a `filas` porque una variable que dice `enCurso` y contiene revisiones de pull
  // request es el tipo de nombre que hace que alguien lea el código dando por hecho algo falso.
  // ## Por qué el `as` es necesario y no es una puerta trasera
  //
  // Porque `PaginaOperaciones` es una unión y TypeScript no puede estrecha sola cuál de sus
  // cuatro miembros es, ya que los cuatro comparten `items`, `total`, `limit` y `offset`. El
  // narrowing real lo hace `seccion`, que es lo que elige la rama del fetcher: cada pestaña pide
  // su endpoint y solo puede devolver su página. El `as` solo le dice al compilador lo que ya es
  // cierto.
  //
  // Y no se puede dejar en `readonly unknown[]`, que es lo que tenía antes: con `unknown` el
  // compilador no puede comprobar que la tabla de escaneos no pinte `pr_number`, que es
  // exactamente el fallo que la unión de tipos existe para cazar. Se sustituye por un `as`
  // justificado para que los cuatro `items` vuelvan a estar comprobados de verdad.
  const filas = datos.data !== null && 'items' in datos.data ? datos.data.items : []
  const total = datos.data !== null && 'total' in datos.data ? datos.data.total : 0

  return (
    <section className="stack-lg">
      <header className="section-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
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
          items={filas as readonly ScanOperacion[]}
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
        <TablaContenedores
          items={filas as readonly ContenedorOperacion[]}
          pendientes={pendientes}
          alLimpiar={limpiar}
        />
      ) : seccion === 'reviews' ? (
        <TablaReviews items={filas as readonly ReviewOperacion[]} />
      ) : (
        <TablaJobs items={filas as readonly JobOperacion[]} />
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
  items: readonly ScanOperacion[]
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
          {items.map((fila) => {
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
                <td className="mono cell-inline">
                  {fila.container_id ?? t('operations.states.noContainer')}
                  {fila.cleanup_pending ? (
                    <span className="badge badge-warning">
                      {t('operations.states.cleanupPending')}
                    </span>
                  ) : null}
                </td>
                <td className="cell-acciones">
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

/**
 * Los contenedores vivos o con limpieza pendiente.
 *
 * ## Por qué esta tabla se escribió ahora y no estaba
 *
 * Porque la pestaña «Contenedores» era un hueco: el `fetcher` no tenía rama para ella, así que
 * caía en la de escaneos, y el JSX pintaba un estado vacío fijo. El efecto en pantalla era una
 * pestaña que decía «5 elementos» arriba y «No hay nada en esta sección» debajo, sobre la misma
 * lista, y un operador que buscaba un contenedor con limpieza pendiente no lo encontraba en
 * ninguna parte.
 *
 * El botón de limpiar se reutiliza tal cual, y con el mismo `run_id` que usa la tabla de
 * escaneos: la limpieza se pide por run, no por contenedor, así que el nombre del contenedor es
 * informative y la acción es la misma.
 *
 * Y `nombre_esperado` se pinta al lado del real porque son la pareja que delata un fallo: si
 * difieren, el `container_id` guardado ya no corresponde al run y hay que Limpiar.
 */
function TablaContenedores({
  items,
  pendientes,
  alLimpiar,
}: {
  items: readonly ContenedorOperacion[]
  pendientes: ReadonlySet<string>
  alLimpiar: (runId: string) => Promise<void>
}) {
  const { t } = useTranslation('admin')
  if (items.length === 0) {
    return <TablaVacia titulo={t('operations.states.empty')} />
  }
  return (
    <div className="table-wrapper">
      <table className="data-table console-table">
        <caption className="visually-hidden">{t('operations.tabs.containers')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('operations.columns.status')}</th>
            <th scope="col">{t('operations.columns.organization')}</th>
            <th scope="col">{t('operations.columns.container')}</th>
            <th scope="col">{t('operations.columns.expectedContainer')}</th>
            <th scope="col">
              <span className="visually-hidden">{t('operations.columns.actions')}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((fila) => {
            const desalineado = fila.container_id !== fila.nombre_esperado
            return (
              <tr key={fila.run_id}>
                <td>
                  <span className="badge mono" title={fila.status}>
                    {t(`operations.states.status.${fila.status}`, {
                      defaultValue: fila.status,
                    })}
                  </span>
                </td>
                <td>{fila.organizacion}</td>
                <td className="mono cell-inline">
                  {fila.container_id}
                  {fila.cleanup_pending ? (
                    <span className="badge badge-warning">
                      {t('operations.states.cleanupPending')}
                    </span>
                  ) : null}
                  {desalineado ? (
                    /* `.badge-error`, no `.badge-high`: esa clase no existe en ninguna hoja y el
                       verificador de clases la señala. El tono es el mismo `--color-high` que usa
                       el resto de estados de error del panel. */
                    <span className="badge badge-error">
                      {t('operations.states.nameMismatch')}
                    </span>
                  ) : null}
                </td>
                <td className="mono cell-muted">{fila.nombre_esperado}</td>
                <td className="cell-acciones">
                  <div className="button-row">
                    {fila.cleanup_pending ? (
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={pendientes.has(`clean:${fila.run_id}`)}
                        onClick={() => void alLimpiar(fila.run_id)}
                      >
                        <span>{t('operations.actions.cleanup')}</span>
                      </button>
                    ) : null}
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function TablaReviews({ items }: { items: readonly ReviewOperacion[] }) {
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
          {items.map((fila) => (
            <tr key={fila.id}>
              <td>
                <span className="badge mono" title={fila.status}>
                  {t(`operations.states.reviewStatus.${fila.status}`, {
                    defaultValue: fila.status,
                  })}
                </span>
              </td>
              <td>{fila.organizacion}</td>
              <td>
                <span className="mono">#{fila.pr_number}</span>{' '}
                {fila.pr_title ?? ''}
              </td>
              <td className="mono">
                {fila.issues_caught_critical} / {fila.issues_caught_high}
              </td>
              <td className="mono timestamp">{formatDate(fila.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TablaJobs({ items }: { items: readonly JobOperacion[] }) {
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
          {items.map((fila) => (
            <tr key={fila.id}>
              <td>
                <span className="badge mono" title={fila.status}>
                  {t(`operations.states.jobStatus.${fila.status}`, {
                    defaultValue: fila.status,
                  })}
                </span>
              </td>
              <td>{fila.organizacion}</td>
              <td>{fila.kind}</td>
              <td className="mono">{fila.target}</td>
              <td className="mono timestamp">{formatDate(fila.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}