import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Info } from 'lucide-react'
import type { OrganizationPriceOverride } from '../../types/api'
import { createOrganizationPriceOverride, getOrganizationPricing } from '../../lib/adminApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { EstadoPactado } from './pricingChain'
import { ordenarCadena, estadosDeLaCadena } from './pricingChain'

/**
 * Ficha de precios pactados de una organización. Ruta propia, no un diálogo.
 *
 * ## Por qué una página y no un modal
 *
 * Porque el sistema de diseño reserva el modal para flujos de dos pasos con una decisión
 * irreversible, y aquí no hay ninguna de las dos cosas: se **lee** (qué cobra este cliente, qué
 * pactados tiene y cuáles están vigentes) y luego se **añade** uno. Meter una tabla de cinco
 * columnas y un formulario dentro de una caja flotante sobre un listado de veinticinco clientes
 * obligaba a la caja a crecer hasta no caber en la ventana, que es exactamente lo que se veía: un
 * scroll horizontal dentro del diálogo y el texto cortado.
 *
 * ## Por qué una página y no ampliar el diálogo
 *
 * Porque el contenido no cabe de verdad. Tres precios con su comparación, una cadena de acuerdos
 * con su estado y un formulario son tres bloques con su propio ritmo, y eso es una página. Un
 * modal con scroll triple es un-peor-página con pasos extra.
 *
 * ## Por qué esta página conserva la cadena entera
 *
 * Porque un acuerdo no se borra: se sustituye. La página muestra los pactados con su motivo y su
 * autor, y el vigente lo marca el servidor, nunca esta pantalla. Lo que el comercial tiene que
 * poder responder en un segundo es «cuánto se le está cobrando» y «cuándo cambió, y por qué», y las
 * dos respuestas están en la misma vista.
 */
export function AdminTenantPricingPage() {
  const { t } = useTranslation('admin')
  const { organizationId } = useParams<{ organizationId: string }>()
  const { token, user } = useAuth()
  const { notify } = useToast()

  const organizationIdValue = organizationId ?? null
  const clave =
    token !== null && user?.is_superuser === true && organizationIdValue !== null ? token : null

  const cargar = useCallback(
    (k: string) => getOrganizationPricing(k, organizationIdValue ?? ''),
    [organizationIdValue],
  )
  const detalle = useAsyncResource(cargar, clave)

  const [operacion, setOperacion] = useState('SCAN_CREDIT_COST')
  const [valor, setValor] = useState('')
  const [motivo, setMotivo] = useState('')
  const [desde, setDesde] = useState('')
  const [guardando, setGuardando] = useState(false)

  /**
   * Los dos `useMemo` van **antes** de cualquier `return`, y no por gusto.
   *
   * Un hook que se llama a veces sí y a veces no rompe el orden de los hooks: no falla al abrir la
   * ficha, falla al cerrarla y volver a abrirla, con el estado de la petición anterior pegado. Es
   * el fallo más difícil de ver de esta pantalla, porque el primer clic funciona.
   */
  const cadena = useMemo(
    () => ordenarCadena(detalle.data?.overrides ?? []),
    [detalle.data],
  )
  /**
   * El estado de cada pactado, resuelto contra el resto de la cadena.
   *
   * Antes se pintaba `estadoDePactado` fila a fila, y un pactado ya sustituido salía como
   * **"Programado"**: no es verdad, ya empezó y se dejó de aplicar porque se pactó otro. Se vio en
   * la primera captura de esta pantalla, y es de esos fallos que solo aparecen mirando.
   */
  const estados = useMemo(() => estadosDeLaCadena(cadena), [cadena])

  const pactar = async () => {
    if (organizationIdValue === null || clave === null) {
      return
    }
    if (motivo.trim().length < 3) {
      notify('error', t('tenants.pricing.motiveRequired'))
      return
    }
    if (valor.trim() === '') {
      notify('error', t('tenants.pricing.valueRequired'))
      return
    }
    setGuardando(true)
    try {
      const pactado = await createOrganizationPriceOverride(clave, organizationIdValue, {
        operacion,
        valor: valor.trim(),
        motivo: motivo.trim(),
        valido_desde: desde === '' ? null : new Date(desde).toISOString(),
        valido_hasta: null,
      })
      notify(
        'success',
        pactado.sustituye_id === null
          ? t('tenants.pricing.actions.created')
          : t('tenants.pricing.actions.replaced'),
      )
      setValor('')
      setMotivo('')
      setDesde('')
      detalle.reload()
    } catch {
      notify('error', t('tenants.pricing.actions.error'))
    } finally {
      setGuardando(false)
    }
  }

  const precios = detalle.data?.precios ?? null
  const plataforma = detalle.data?.precios_de_plataforma ?? null

  return (
    <section className="page-section stack-lg" aria-labelledby="tenant-pricing-title">
      <Link className="console-backlink" to="/admin/tenants">
        <ArrowLeft size={15} aria-hidden="true" />
        <span>{t('tenants.pricing.back')}</span>
      </Link>

      <div className="page-header">
        <div>
          <p className="eyebrow">{t('tenants.pricing.eyebrow')}</p>
          <h1 id="tenant-pricing-title">{t('tenants.pricing.title')}</h1>
          <p className="page-description">{t('tenants.pricing.description')}</p>
        </div>
      </div>

      {detalle.loadFailed && detalle.data === null ? (
        <div className="empty-card">
          <h2>{t('tenants.pricing.states.error')}</h2>
          <button className="secondary-button" type="button" onClick={detalle.reload}>
            <span>{t('tenants.pricing.states.retry')}</span>
          </button>
        </div>
      ) : detalle.isLoading && detalle.data === null ? (
        <div className="console-skeleton-lines" aria-hidden="true">
          {Array.from({ length: 7 }, (_, indice) => (
            <div
              key={indice}
              className="console-skeleton-line"
              style={{ width: `${100 - indice * 7}%` }}
            />
          ))}
        </div>
      ) : precios !== null && plataforma !== null ? (
        <>
          {/* ---------------------------------------------------- lo que se cobra */}
          <section className="console-block" aria-labelledby="tenant-pricing-current">
            <header>
              <div>
                <h2 id="tenant-pricing-current">{t('tenants.pricing.current.title')}</h2>
                <p className="section-description">{t('tenants.pricing.current.help')}</p>
              </div>
            </header>

            {/*
              Filas de ajuste, no una tabla.
            */}
            <div className="console-settings">
              <FilaPrecio
                nombre={t('pricing.scalars.fields.scanCost')}
                ayuda={t('tenants.pricing.rows.scanCost')}
                unidad={t('pricing.scalars.units.scanCost')}
                pagado={precios.scan_credit_cost}
                referencia={plataforma.scan_credit_cost}
              />
              <FilaPrecio
                nombre={t('pricing.scalars.fields.quickMultiplier')}
                ayuda={t('tenants.pricing.rows.quickMultiplier')}
                unidad={t('pricing.scalars.units.quickMultiplier')}
                pagado={precios.quick_scan_credit_multiplier}
                referencia={plataforma.quick_scan_credit_multiplier}
              />
              <FilaPrecio
                nombre={t('pricing.scalars.fields.creditsPerUsd')}
                ayuda={t('tenants.pricing.rows.creditsPerUsd')}
                unidad={t('pricing.scalars.units.creditsPerUsd')}
                pagado={precios.credits_per_usd}
                referencia={plataforma.credits_per_usd}
              />
            </div>
          </section>

          {/* ---------------------------------------------------- la cadena */}
          <section className="console-block" aria-labelledby="tenant-pricing-chain">
            <header>
              <div>
                <h2 id="tenant-pricing-chain">{t('tenants.pricing.chain.title')}</h2>
                <p className="section-description">{t('tenants.pricing.chain.help')}</p>
              </div>
            </header>

            {cadena.length === 0 ? (
              <p className="cell-muted">{t('tenants.pricing.chain.empty')}</p>
            ) : (
              <>
                <p className="cell-muted">
                  {t('tenants.pricing.chain.summary', {
                    vigente: cadena.filter((p) => estados.get(p.id) === 'vigente').length,
                    programado: cadena.filter((p) => estados.get(p.id) === 'programado').length,
                    sustituido: cadena.filter((p) => estados.get(p.id) === 'sustituido').length,
                    total: cadena.length,
                  })}
                </p>
                <ul className="pricing-chain">
                  {cadena.map((pactado) => (
                    <FilaPactado key={pactado.id} pactado={pactado} estado={estados.get(pactado.id)} />
                  ))}
                </ul>
              </>
            )}
          </section>

          {/* ---------------------------------------------------- pactar */}
          <section className="console-block" aria-labelledby="tenant-pricing-form">
            <header>
              <div>
                <h2 id="tenant-pricing-form">{t('tenants.pricing.form.title')}</h2>
                <p className="section-description">{t('tenants.pricing.form.help')}</p>
              </div>
            </header>

            <div className="console-settings">
              <div className="console-setting-row">
                <div className="console-setting-copy">
                  <strong>{t('tenants.pricing.form.operation')}</strong>
                </div>
                <div className="console-setting-control">
                  <select
                    className="select-input"
                    aria-label={t('tenants.pricing.form.operation')}
                    value={operacion}
                    disabled={guardando}
                    onChange={(e) => setOperacion(e.target.value)}
                  >
                    <option value="SCAN_CREDIT_COST">
                      {t('pricing.scalars.fields.scanCost')}
                    </option>
                    <option value="QUICK_SCAN_MULTIPLIER">
                      {t('pricing.scalars.fields.quickMultiplier')}
                    </option>
                    <option value="CREDITS_PER_USD">
                      {t('pricing.scalars.fields.creditsPerUsd')}
                    </option>
                  </select>
                </div>
              </div>

              <div className="console-setting-row">
                <div className="console-setting-copy">
                  <strong>{t('tenants.pricing.form.value')}</strong>
                  <span>{t('tenants.pricing.form.valueHelp')}</span>
                </div>
                <div className="console-setting-control">
                  <input
                    type="text"
                    inputMode="decimal"
                    className="text-input mono"
                    aria-label={t('tenants.pricing.form.value')}
                    value={valor}
                    disabled={guardando}
                    onChange={(e) => setValor(e.target.value)}
                  />
                </div>
              </div>

              <div className="console-setting-row">
                <div className="console-setting-copy">
                  <strong>{t('tenants.pricing.form.motive')}</strong>
                  <span>{t('tenants.pricing.form.motiveHelp')}</span>
                </div>
                <div className="console-setting-control">
                  <input
                    type="text"
                    className="text-input"
                    maxLength={255}
                    aria-label={t('tenants.pricing.form.motive')}
                    value={motivo}
                    disabled={guardando}
                    onChange={(e) => setMotivo(e.target.value)}
                  />
                </div>
              </div>

              <div className="console-setting-row">
                <div className="console-setting-copy">
                  <strong>{t('tenants.pricing.form.from')}</strong>
                  <span>{t('tenants.pricing.form.fromHelp')}</span>
                </div>
                <div className="console-setting-control">
                  <input
                    type="datetime-local"
                    className="text-input"
                    aria-label={t('tenants.pricing.form.from')}
                    value={desde}
                    disabled={guardando}
                    onChange={(e) => setDesde(e.target.value)}
                  />
                </div>
              </div>
            </div>

            <div className="button-row">
              <button
                className="primary-button"
                type="button"
                disabled={guardando}
                onClick={() => void pactar()}
              >
                <span>
                  {t(guardando ? 'tenants.pricing.actions.saving' : 'tenants.pricing.actions.pact')}
                </span>
              </button>
            </div>
          </section>
        </>
      ) : null}

      <p className="chart-empty">
        <Info size={13} aria-hidden="true" />
        <span>{t('tenants.pricing.footnote')}</span>
      </p>
    </section>
  )
}

interface FilaPrecioProps {
  nombre: string
  ayuda: string
  unidad: string
  pagado: string
  referencia: string
}

/**
 * Una fila de precio pactado.
 *
 * ## Por qué el precio de plataforma va dentro de la fila y no en una columna aparte
 *
 * Porque lo que el comercial necesita responder es una pregunta de sí o no por fila: ¿esto está
 * negociado? Con el valor de plataforma al lado del pactado, la respuesta está en la misma línea y
 * no hay que comparar mentalmente dos columnas. Con una columna de plataforma aparte, hay que
 * recorrer las dos y decidir cuál manda, y el ancho de columna extra no aporta nada.
 *
 * ## Por qué el distintivo solo aparece cuando los precios difieren
 *
 * Porque si coinciden, el cliente no tiene precio pactado en esa operación, y decirlo en todas las
 * filas sería ruido: se lee «todo está negociado» cuando en realidad no hay nada negociado. El
 * distintivo aparece solo cuando hay algo que distinguir.
 */
function FilaPrecio({ nombre, ayuda, unidad, pagado, referencia }: FilaPrecioProps) {
  const { t } = useTranslation('admin')
  const negociado = pagado !== referencia

  return (
    <div className="console-setting-row">
      <div className="console-setting-copy">
        <strong>{nombre}</strong>
        <span>{ayuda}</span>
      </div>
      <div className="console-setting-control">
        <span className="mono">{pagado}</span>
        <span className="cell-muted">{unidad}</span>
        {negociado ? (
          <span className="badge badge-on">{t('tenants.pricing.rows.negotiated')}</span>
        ) : null}
      </div>
    </div>
  )
}

interface FilaPactadoProps {
  pactado: OrganizationPriceOverride
  /** El estado ya resuelto contra la cadena. `undefined` solo si el id no estaba en el mapa. */
  estado: EstadoPactado | undefined
}

/**
 * Una fila de la cadena, con su estado.
 *
 * ## Por qué el estado va en la insignia y no en un filete
 *
 *
 * Porque el sistema de diseño prohíbe las franjas de color laterales: son un acento decorativo y
 * compiten con la insignia por la atención sin añadir información. El estado ya se dice con texto
 * ("Vigente", "Programado", "Caducado"), que es lo que se lee; la insignia es lo que se escanea.
 */
function FilaPactado({ pactado, estado }: FilaPactadoProps) {
  const { t, i18n } = useTranslation('admin')
  const fecha = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(pactado.valido_desde))

  const CLAVE: Record<EstadoPactado, string> = {
    vigente: 'tenants.pricing.states.live',
    programado: 'tenants.pricing.states.future',
    caducado: 'tenants.pricing.states.expired',
    sustituido: 'tenants.pricing.states.superseded',
  }

  // Un estado que no esté en el mapa no debería pasar: se cae al del propio pactado, que es
  // peor pero no es un `undefined` pintado en pantalla.
  const resuelto = estado ?? 'programado'
  return (
    <li className="pricing-chain-item" data-estado={resuelto}>
      <span className={resuelto === 'vigente' ? 'badge badge-on' : 'badge badge-muted'}>
        {t(CLAVE[resuelto])}
      </span>
      <span className="pricing-chain-value">{pactado.valor}</span>
      <span className="cell-muted">
        {t(`tenants.pricing.operations.${pactado.operacion}`)}
      </span>
      <span className="cell-muted mono">{fecha}</span>
      <span className="pricing-chain-motive">{pactado.motivo}</span>
    </li>
  )
}
