/**
 * Consola de precios de plataforma: la única pantalla desde la que se cambia lo que cobra el
 * producto.
 *
 * ## Por qué esta pantalla no es una más de la consola
 *
 * Porque las demás muestran datos de una organización, y esta escribe un número del que
 * **depende toda la plataforma**. Es la diferencia entre leer la caja y decidir qué se
 * vende, y por eso está en su propia sección del menú en vez de dentro de «Ventas».
 *
 * ## Por qué los importes viajan como texto y no se convierten a número
 *
 * Porque el backend los declara `Decimal` con ocho decimales, y un número JavaScript es un
 * binario en coma flotante donde `0.1` no es exactamente `0.1`. La paridad del crédito es el
 * caso peor: es un cociente, y a `0.01234567` la coma flotante le ha perdido el último dígito
 * antes de llegar aquí. El `<input>` guarda **el texto crudo** y lo que se envía es ese mismo
 * texto; convertirlo con `Number()` para «limpiarlo» es exactamente el error que este
 * comentario evita.
 *
 * ## Por qué el botón de guardar aparece y desaparece
 *
 * Porque un botón `disabled` no explica por qué no se puede pulsar. Renderizarlo solo cuando
 * algo cambió convierte la pregunta «¿puedo guardar?» en algo que se responde mirando la
 * pantalla, y hace imposible activar el botón sin querer.
 *
 * ## Por qué el motivo es un campo y no un `prompt`
 *
 * Porque un `prompt` se puede cancelar en blanco y porque bloquea: mientras está abierto no se
 * ve el precio que se está cambiando. Con un campo visible, el motivo se escribe antes de
 * guardar y se lee después en el histórico, que es donde de verdad importa.
 *
 * ## Por qué no hay ningún botón de borrar
 *
 * Por R4 y por operativa. Un precio que se puede borrar no se puede auditar, y un borrado
 * accidental desde el panel no se distingue de una decisión. Los packs y los tramos se
 * desactivan: la fila se queda, deja de ofrecerse, y su historial sigue siendo legible.
 */

import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type {
  PlatformCreditPack,
  PlatformPriceChange,
  PlatformPricingDetail,
  PlatformVolumeTier,
} from '../../types/api'
import {
  createPlatformCreditPack,
  createPlatformVolumeTier,
  getPlatformPriceChanges,
  getPlatformPricing,
  updatePlatformCreditPack,
  updatePlatformPricing,
  updatePlatformVolumeTier,
} from '../../lib/adminApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'

/**
 * Los siete precios de la fila única, y **solo** esos siete.
 *
 * ## Por qué la unión es explícita y no `keyof PlatformPricing`
 *
 * Porque `PlatformPricing` incluye `updated_at`, que es `string | null`. Con
 * `keyof PlatformPricing` el índice devuelve `string | null`, y `String | null` no se puede
 * meter en un `<input type="number">`: el error sale en la compilación, tres pantallas más
 * abajo, como un argumento que no es `string`. Declarando la unión a mano, el compilador
 * comprueba **que los siete campos existen** y que ninguno es nullable, y si mañana se añade
 * un octavo precio a la fila y no está en esta lista, el olvido se ve en la misma línea.
 */
type CampoPrecio =
  | 'credits_per_usd'
  | 'scan_credit_cost'
  | 'quick_scan_credit_multiplier'
  | 'low_credit_balance_threshold'
  | 'custom_spend_minimum_usd'
  | 'custom_spend_maximum_usd'
  | 'pro_subscription_monthly_usd'

/** Cada precio, con su etiqueta traducida, su unidad y el paso del `<input>`. */
const PRECIOS: ReadonlyArray<{
  campo: CampoPrecio
  etiqueta: string
  unidad: string
  paso: string
}> = [
  { campo: 'credits_per_usd', etiqueta: 'creditsPerUsd', unidad: 'creditsPerUsd', paso: '0.00000001' },
  { campo: 'scan_credit_cost', etiqueta: 'scanCost', unidad: 'scanCost', paso: '0.0001' },
  { campo: 'quick_scan_credit_multiplier', etiqueta: 'quickMultiplier', unidad: 'quickMultiplier', paso: '0.01' },
  { campo: 'low_credit_balance_threshold', etiqueta: 'lowThreshold', unidad: 'lowThreshold', paso: '0.0001' },
  { campo: 'custom_spend_minimum_usd', etiqueta: 'minSpend', unidad: 'minSpend', paso: '0.01' },
  { campo: 'custom_spend_maximum_usd', etiqueta: 'maxSpend', unidad: 'maxSpend', paso: '0.01' },
  { campo: 'pro_subscription_monthly_usd', etiqueta: 'proPrice', unidad: 'proPrice', paso: '0.01' },
]

/** Compara el texto del borrador con el del servidor, sin pasar por `Number`. */
function cambia(texto: string, guardado: string): boolean {
  return texto.trim() !== guardado.trim()
}

export function AdminPricingPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()
  const { notify } = useToast()

  const clave = token !== null && user?.is_superuser === true ? token : null
  /** El mismo token, ya estrechado: lo que se pasa a los formularios es `string`. */
  const tokenActivo: string | null = clave

  const cargarDetalle = useCallback((k: string) => getPlatformPricing(k), [])
  const detalle = useAsyncResource<PlatformPricingDetail>(cargarDetalle, clave)

  /**
   * El contador vive **en la clave** del histórico, y no en un estado aparte.
   *
   * Es lo que hace `useAsyncResource`: vuelve a pedir cuando cambia la clave. Un contador
   * suelto al que solo se llama desde un manejador no recarga nada, porque el hook no se entera
   * de que el contador cambió: la clave es el único contrato con él. Y por eso no se recarga el
   * histórico en cada tecla de un `<input>` —la tecla no cambia la clave—, sino solo después de
   * guardar, que es cuando hay algo nuevo que enseñar.
   */
  const [tickHistorial, setTickHistorial] = useState(0)
  const recargarHistorial = useCallback(() => setTickHistorial((n) => n + 1), [])
  const claveHistorial = clave === null ? null : `${clave}:${tickHistorial}`

  const cargarHistorial = useCallback((k: string) => getPlatformPriceChanges(k), [])
  const historial = useAsyncResource(cargarHistorial, claveHistorial)

  /** Los borradores de los siete precios: el texto crudo de cada `<input>`. */
  const [borrador, setBorrador] = useState<Record<string, string>>({})
  const [motivo, setMotivo] = useState('')
  const [pendientes, setPendientes] = useState<ReadonlySet<string>>(new Set())

  const guardando = pendientes.has('precios')
  const precios = detalle.data?.precios ?? null

  const cambios = useMemo(() => {
    if (precios === null) {
      return []
    }
    return PRECIOS.filter((p) => {
      const texto = borrador[p.campo]
      return texto !== undefined && cambia(texto, precios[p.campo])
    }).map((p) => [p.campo, borrador[p.campo].trim()] as const)
  }, [borrador, precios])

  if (tokenActivo === null) {
    return null
  }
  // **El fallo se comprueba antes que la carga**, y el orden es lo único que importa aquí.
  //
  // `useAsyncResource` deriva las dos cosas: `isLoading` es «la clave pedida no es la de los
  // datos» y `loadFailed` es «esta petición falló». Cuando la petición falla, `keyDeLosDatos`
  // sigue siendo `null`, así que **las dos son ciertas a la vez**. Mirando primero
  // `isLoading`, el error no se llega a pintar nunca y la pantalla queda en «Cargando
  // precios…» para siempre.
  //
  // Y no es un caso raro: es exactamente lo que pasa cuando el backend no tiene la ruta —un
  // despliegue a medias, un servidor sin reiniciar— y ese es el fallo que un operador no puede
  // diagnosticar desde la pantalla, porque la pantalla le está diciendo que sigue cargando.
  if (detalle.loadFailed && detalle.data === null) {
    return (
      <div className="empty-card">
        <p>{t('pricing.states.error')}</p>
        <button className="secondary-button" type="button" onClick={detalle.reload}>
          <span>{t('pricing.states.retry')}</span>
        </button>
      </div>
    )
  }
  if (detalle.isLoading && detalle.data === null) {
    return <div className="empty-card"><p>{t('pricing.states.loading')}</p></div>
  }
  if (precios === null) {
    return null
  }

  const guardarPrecios = async () => {
    if (motivo.trim().length < 3) {
      notify('error', t('pricing.states.motiveRequired'))
      return
    }
    setPendientes((actual) => new Set(actual).add('precios'))
    try {
      await updatePlatformPricing(tokenActivo, {
        ...Object.fromEntries(cambios),
        motivo: motivo.trim(),
      })
      notify('success', t('pricing.actions.savedPrices'))
      setBorrador({})
      setMotivo('')
      detalle.reload()
      recargarHistorial()
    } catch {
      notify('error', t('pricing.actions.error'))
    } finally {
      setPendientes((actual) => {
        const siguiente = new Set(actual)
        siguiente.delete('precios')
        return siguiente
      })
    }
  }

  const alternarPack = async (pack: PlatformCreditPack) => {
    const id = `pack:${pack.id}`
    setPendientes((actual) => new Set(actual).add(id))
    try {
      await updatePlatformCreditPack(tokenActivo, pack.id, {
        is_active: !pack.is_active,
        motivo: pack.is_active
          ? t('pricing.packs.inactive')
          : t('pricing.packs.active'),
      })
      notify('success', t('pricing.actions.savedPack'))
      detalle.reload()
      recargarHistorial()
    } catch {
      notify('error', t('pricing.actions.error'))
    } finally {
      setPendientes((actual) => {
        const siguiente = new Set(actual)
        siguiente.delete(id)
        return siguiente
      })
    }
  }

  const alternarTramo = async (tramo: PlatformVolumeTier) => {
    const id = `tier:${tramo.id}`
    setPendientes((actual) => new Set(actual).add(id))
    try {
      await updatePlatformVolumeTier(tokenActivo, tramo.id, {
        is_active: !tramo.is_active,
        motivo: tramo.is_active
          ? t('pricing.tiers.inactive')
          : t('pricing.tiers.active'),
      })
      notify('success', t('pricing.actions.savedTier'))
      detalle.reload()
      recargarHistorial()
    } catch {
      notify('error', t('pricing.actions.error'))
    } finally {
      setPendientes((actual) => {
        const siguiente = new Set(actual)
        siguiente.delete(id)
        return siguiente
      })
    }
  }

  const valor = (campo: string, guardado: string) => borrador[campo] ?? guardado

  return (
    <section className="stack-lg">
      <header className="section-header">
        <div>
          <p className="eyebrow">{t('nav.console')}</p>
          <h1>{t('pricing.title')}</h1>
          <p className="section-description">{t('pricing.description')}</p>
        </div>
        <span className={detalle.data?.desde_la_base ? 'badge badge-on' : 'badge badge-warning'}>
          {t(detalle.data?.desde_la_base ? 'pricing.badges.fromDatabase' : 'pricing.badges.fromCode')}
        </span>
      </header>

      {detalle.loadFailed && (
        <p className="inline-notice inline-notice-warning" role="alert">
          {t('pricing.states.error')}
        </p>
      )}

      {/* ------------------------------------------------------------ precios base */}
      <article className="panel">
        <h2>{t('pricing.scalars.title')}</h2>
        <p className="section-description">{t('pricing.scalars.help')}</p>
        <div className="table-wrapper">
          <table className="data-table console-table">
            <caption className="visually-hidden">{t('pricing.scalars.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('pricing.scalars.columns.field')}</th>
                <th scope="col">{t('pricing.scalars.columns.value')}</th>
                <th scope="col">{t('pricing.scalars.columns.unit')}</th>
              </tr>
            </thead>
            <tbody>
              {PRECIOS.map((precio) => {
                const idCampo = `precio-${precio.campo}`
                return (
                  <tr key={precio.campo}>
                    <th scope="row">
                      <label htmlFor={idCampo}>{t(`pricing.scalars.fields.${precio.etiqueta}`)}</label>
                    </th>
                    <td>
                      <input
                        id={idCampo}
                        type="number"
                        step={precio.paso}
                        inputMode="decimal"
                        className="mono"
                        disabled={guardando}
                        value={valor(precio.campo, precios[precio.campo])}
                        onChange={(e) =>
                          setBorrador((actual) => ({ ...actual, [precio.campo]: e.target.value }))
                        }
                      />
                    </td>
                    <td className="cell-muted">{t(`pricing.scalars.units.${precio.unidad}`)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        <div className="field">
          <label htmlFor="motivo-precios">{t('pricing.motive.label')}</label>
          <input
            id="motivo-precios"
            type="text"
            maxLength={255}
            placeholder={t('pricing.motive.placeholder')}
            disabled={guardando}
            value={motivo}
            onChange={(e) => setMotivo(e.target.value)}
          />
          <p className="field-help">{t('pricing.motive.help')}</p>
        </div>

        {cambios.length > 0 ? (
          <div className="button-row">
            <button
              className="primary-button"
              type="button"
              disabled={guardando}
              onClick={() => void guardarPrecios()}
            >
              <span>{t(guardando ? 'pricing.actions.saving' : 'pricing.actions.save')}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={guardando}
              onClick={() => {
                setBorrador({})
                setMotivo('')
              }}
            >
              <span>{t('pricing.actions.revert')}</span>
            </button>
          </div>
        ) : null}
      </article>

      {/* ------------------------------------------------------------ packs */}
      <article className="panel">
        <h2>{t('pricing.packs.title')}</h2>
        <p className="section-description">{t('pricing.packs.help')}</p>
        {detalle.data !== null && detalle.data.packs.length === 0 ? (
          <p className="cell-muted">{t('pricing.packs.empty')}</p>
        ) : (
          <div className="table-wrapper">
            <table className="data-table console-table">
              <caption className="visually-hidden">{t('pricing.packs.title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('pricing.packs.columns.credits')}</th>
                  <th scope="col">{t('pricing.packs.columns.amount')}</th>
                  <th scope="col">{t('pricing.packs.columns.order')}</th>
                  <th scope="col">{t('pricing.packs.columns.state')}</th>
                  <th scope="col">
                    <span className="visually-hidden">
                      {t('pricing.packs.columns.actions')}
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(detalle.data?.packs ?? []).map((pack) => (
                  <tr key={pack.id} className={pack.is_active ? undefined : 'row-inactive'}>
                    <td className="mono">{pack.credits}</td>
                    <td className="mono">{pack.amount_usd}</td>
                    <td className="mono">{pack.display_order}</td>
                    <td>{t(pack.is_active ? 'pricing.packs.active' : 'pricing.packs.inactive')}</td>
                    <td>
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={pendientes.has(`pack:${pack.id}`)}
                        onClick={() => void alternarPack(pack)}
                      >
                        <span>
                          {t(
                            pack.is_active
                              ? 'pricing.actions.deactivate'
                              : 'pricing.actions.activate',
                          )}
                        </span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <NuevoPack
          clave={tokenActivo}
          alGuardar={() => {
            detalle.reload()
            recargarHistorial()
          }}
          avisar={notify}
          pendiente={pendientes.has('nuevo-pack')}
          marcarPendiente={(v) =>
            setPendientes((actual) => {
              const siguiente = new Set(actual)
              if (v) siguiente.add('nuevo-pack')
              else siguiente.delete('nuevo-pack')
              return siguiente
            })
          }
        />
      </article>

      {/* ------------------------------------------------------------ escalera */}
      <article className="panel">
        <h2>{t('pricing.tiers.title')}</h2>
        <p className="section-description">{t('pricing.tiers.help')}</p>
        {detalle.data !== null && detalle.data.tiers.length === 0 ? (
          <p className="cell-muted">{t('pricing.tiers.empty')}</p>
        ) : (
          <div className="table-wrapper">
            <table className="data-table console-table">
              <caption className="visually-hidden">{t('pricing.tiers.title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('pricing.tiers.columns.spend')}</th>
                  <th scope="col">{t('pricing.tiers.columns.discount')}</th>
                  <th scope="col">{t('pricing.tiers.columns.order')}</th>
                  <th scope="col">{t('pricing.tiers.columns.state')}</th>
                  <th scope="col">
                    <span className="visually-hidden">{t('pricing.tiers.columns.actions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(detalle.data?.tiers ?? []).map((tramo) => (
                  <tr key={tramo.id} className={tramo.is_active ? undefined : 'row-inactive'}>
                    <td className="mono">{tramo.spend_min_usd}</td>
                    <td className="mono">{tramo.discount}</td>
                    <td className="mono">{tramo.display_order}</td>
                    <td>{t(tramo.is_active ? 'pricing.tiers.active' : 'pricing.tiers.inactive')}</td>
                    <td>
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={pendientes.has(`tier:${tramo.id}`)}
                        onClick={() => void alternarTramo(tramo)}
                      >
                        <span>
                          {t(
                            tramo.is_active
                              ? 'pricing.actions.deactivate'
                              : 'pricing.actions.activate',
                          )}
                        </span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <NuevoTramo
          clave={tokenActivo}
          alGuardar={() => {
            detalle.reload()
            recargarHistorial()
          }}
          avisar={notify}
          pendiente={pendientes.has('nuevo-tramo')}
          marcarPendiente={(v) =>
            setPendientes((actual) => {
              const siguiente = new Set(actual)
              if (v) siguiente.add('nuevo-tramo')
              else siguiente.delete('nuevo-tramo')
              return siguiente
            })
          }
        />
      </article>

      {/* ------------------------------------------------------------ histórico */}
      <article className="panel">
        <h2>{t('pricing.history.title')}</h2>
        <p className="section-description">{t('pricing.history.help')}</p>
        {historial.data === null ? (
          <p className="cell-muted">{t('pricing.states.loading')}</p>
        ) : historial.data.items.length === 0 ? (
          <p className="cell-muted">{t('pricing.history.empty')}</p>
        ) : (
          <>
            <p className="cell-muted">
              {t('pricing.history.total', { count: historial.data.total })}
            </p>
            <div className="table-wrapper">
              <table className="data-table console-table">
                <caption className="visually-hidden">{t('pricing.history.title')}</caption>
                <thead>
                  <tr>
                    <th scope="col">{t('pricing.history.columns.price')}</th>
                    <th scope="col">{t('pricing.history.columns.before')}</th>
                    <th scope="col">{t('pricing.history.columns.after')}</th>
                    <th scope="col">{t('pricing.history.columns.actor')}</th>
                    <th scope="col">{t('pricing.history.columns.motive')}</th>
                    <th scope="col">{t('pricing.history.columns.date')}</th>
                  </tr>
                </thead>
                <tbody>
                  {historial.data.items.map((cambio, indice) => (
                    <FilaCambio key={`${cambio.clave}-${cambio.changed_at}-${indice}`} cambio={cambio} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </article>
    </section>
  )
}

/**
 * Una fila del histórico.
 *
 * ## Por qué el identificador va en el `title` y la etiqueta traducida a la vista
 *
 * Porque el identificador crudo es lo que hay que citar en un ticket o en una conversación con
 * el cliente, y la frase traducida es lo que hay que entender. Poner el identificador a la
 * vista obliga a leer nombres de columna; poner solo la frase translated deja sin poder citar
 * el dato exacto. Con las dos, cada opción sirve.
 *
 * ## Por qué `valor_anterior` en `null` se pinta como «sembrado» y no como vacío
 *
 * Porque `null` ahí significa algo concreto: **no había precio anterior en la base**, el precio
 * estaba en el código. Un hueco en blanco se lee como «no lo sé», y aquí sí se sabe: es el
 * primer asiento de una fila que la migración sembró.
 */
function FilaCambio({ cambio }: { cambio: PlatformPriceChange }) {
  const { t, i18n } = useTranslation('admin')
  const fmt = (v: string | null) =>
    v === null ? t('pricing.history.noValue') : v
  return (
    <tr>
      <td>
        <span className="mono" title={cambio.clave}>
          {cambio.clave}
        </span>
      </td>
      <td className="mono">
        {cambio.valor_anterior === null ? t('pricing.history.seeded') : fmt(cambio.valor_anterior)}
      </td>
      <td className="mono">{fmt(cambio.valor_nuevo)}</td>
      <td>
        {cambio.actor_user_id ?? <span className="cell-muted">{t('pricing.history.system')}</span>}
      </td>
      <td className="cell-muted">{cambio.motivo ?? '—'}</td>
      <td>
        <span className="mono timestamp">
          {new Intl.DateTimeFormat(i18n.language, {
            dateStyle: 'medium',
            timeStyle: 'medium',
          }).format(new Date(cambio.changed_at))}
        </span>
      </td>
    </tr>
  )
}

/** Alta de un pack. El motivo lo escribe el operador, y es obligatorio. */
function NuevoPack({
  clave,
  alGuardar,
  avisar,
  pendiente,
  marcarPendiente,
}: {
  clave: string
  alGuardar: () => void
  avisar: ReturnType<typeof useToast>['notify']
  pendiente: boolean
  marcarPendiente: (v: boolean) => void
}) {
  const { t } = useTranslation('admin')
  const [credits, setCredits] = useState('')
  const [amount, setAmount] = useState('')
  const [motive, setMotive] = useState('')

  const enviar = async () => {
    if (motive.trim().length < 3) {
      avisar('error', t('pricing.states.motiveRequired'))
      return
    }
    marcarPendiente(true)
    try {
      await createPlatformCreditPack(clave, {
        credits: Number(credits),
        amount_usd: amount.trim(),
        motivo: motive.trim(),
      })
      avisar('success', t('pricing.actions.createdPack'))
      setCredits('')
      setAmount('')
      setMotive('')
      alGuardar()
    } catch {
      avisar('error', t('pricing.actions.error'))
    } finally {
      marcarPendiente(false)
    }
  }

  return (
    <div className="field-group">
      <h3>{t('pricing.actions.add')}</h3>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="pack-nuevo-credits">{t('pricing.packs.new.credits')}</label>
          <input
            id="pack-nuevo-credits"
            type="number"
            min="1"
            step="1"
            disabled={pendiente}
            value={credits}
            onChange={(e) => setCredits(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="pack-nuevo-importe">{t('pricing.packs.new.amount')}</label>
          <input
            id="pack-nuevo-importe"
            type="number"
            min="0"
            step="0.01"
            inputMode="decimal"
            className="mono"
            disabled={pendiente}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="pack-nuevo-motivo">{t('pricing.motive.label')}</label>
          <input
            id="pack-nuevo-motivo"
            type="text"
            maxLength={255}
            disabled={pendiente}
            value={motive}
            onChange={(e) => setMotive(e.target.value)}
          />
        </div>
      </div>
      <button
        className="secondary-button"
        type="button"
        disabled={pendiente || credits.trim() === '' || amount.trim() === ''}
        onClick={() => void enviar()}
      >
        <span>{t('pricing.actions.add')}</span>
      </button>
    </div>
  )
}

/** Alta de un tramo de la escalera. */
function NuevoTramo({
  clave,
  alGuardar,
  avisar,
  pendiente,
  marcarPendiente,
}: {
  clave: string
  alGuardar: () => void
  avisar: ReturnType<typeof useToast>['notify']
  pendiente: boolean
  marcarPendiente: (v: boolean) => void
}) {
  const { t } = useTranslation('admin')
  const [spend, setSpend] = useState('')
  const [discount, setDiscount] = useState('')
  const [motive, setMotive] = useState('')

  const enviar = async () => {
    if (motive.trim().length < 3) {
      avisar('error', t('pricing.states.motiveRequired'))
      return
    }
    marcarPendiente(true)
    try {
      await createPlatformVolumeTier(clave, {
        spend_min_usd: spend.trim(),
        discount: discount.trim(),
        motivo: motive.trim(),
      })
      avisar('success', t('pricing.actions.createdTier'))
      setSpend('')
      setDiscount('')
      setMotive('')
      alGuardar()
    } catch {
      avisar('error', t('pricing.actions.error'))
    } finally {
      marcarPendiente(false)
    }
  }

  return (
    <div className="field-group">
      <h3>{t('pricing.actions.add')}</h3>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="tramo-nuevo-gasto">{t('pricing.tiers.new.spend')}</label>
          <input
            id="tramo-nuevo-gasto"
            type="number"
            min="0"
            step="0.01"
            inputMode="decimal"
            className="mono"
            disabled={pendiente}
            value={spend}
            onChange={(e) => setSpend(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="tramo-nuevo-descuento">{t('pricing.tiers.new.discount')}</label>
          <input
            id="tramo-nuevo-descuento"
            type="number"
            min="0"
            max="1"
            step="0.01"
            inputMode="decimal"
            className="mono"
            disabled={pendiente}
            value={discount}
            onChange={(e) => setDiscount(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="tramo-nuevo-motivo">{t('pricing.motive.label')}</label>
          <input
            id="tramo-nuevo-motivo"
            type="text"
            maxLength={255}
            disabled={pendiente}
            value={motive}
            onChange={(e) => setMotive(e.target.value)}
          />
        </div>
      </div>
      <button
        className="secondary-button"
        type="button"
        disabled={pendiente || spend.trim() === '' || discount.trim() === ''}
        onClick={() => void enviar()}
      >
        <span>{t('pricing.actions.add')}</span>
      </button>
    </div>
  )
}
