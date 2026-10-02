import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { BadgeDollarSign, X } from 'lucide-react'
import type { AdminOrganization, OrganizationPriceOverride } from '../../types/api'
import { createOrganizationPriceOverride, getOrganizationPricing } from '../../lib/adminApi'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'
import { useAsyncResource } from '../shared/useAsyncResource'
import type { EstadoPactado } from './pricingChain'
import { estadoDePactado, ordenarCadena, resumenDeLaCadena } from './pricingChain'

/**
 * Ficha de precios pactados de una organización, y el formulario para pactar uno.
 *
 * ## Por qué vive en la ficha del cliente y no en la página de precios
 *
 * Porque un precio pactado es un **acto comercial sobre un cliente**, no un ajuste de plataforma.
 * En la página de precios hay una sola fila y una sola verdad; aquí hay una cadena de acuerdos con
 * fechas y motivos, y se lee junto al nombre de quien la firmó. Ponerlo allí obligaría a elegir
 * cliente antes de poder mirar, y a que el comercial alternase entre dos pantallas para comparar
 * el precio de plataforma con el pactado.
 *
 * ## Por qué el panel no decide cuál es el vigente
 *
 * Porque la respuesta viene marcada. El vigente depende del reloj —un pactado empieza a regir el
 * día que dice, no el día que se escribió—, y una pantalla que lo dedujera por su cuenta tendría
 * que reimplementar esa regla. Lo único que se pinta es lo que el servidor dice, y por eso hay
 * tres estados visibles y no dos: vigente, futuro y caducado.
 *
 * ## Por qué no hay botón de borrar
 *
 * Por R4 y por lo que el pactado significa. Un acuerdo no se borra: se sustituye por otro, y los
 * dos se quedan con su motivo y su autor. La baja de un pactado es su sustitución, y por eso el
 * único botón que hay es "pactar".
 *
 * ## Por qué el valor viaja como texto y nunca como número
 *
 * Igual que en la página de precios: el backend declara `Decimal` con ocho decimales, y la coma
 * flotante perdería el último dígito de la paridad del crédito. Un precio pactado con el último
 * dígito redondeado es un precio que no es el que se pactó.
 */
interface TenantPricingDialogProps {
  organization: AdminOrganization | null
  onClose: () => void
}

const OPERACIONES: ReadonlyArray<{ valor: string; etiqueta: string; unidad: string }> = [
  { valor: 'SCAN_CREDIT_COST', etiqueta: 'scanCost', unidad: 'creditos' },
  { valor: 'QUICK_SCAN_MULTIPLIER', etiqueta: 'quickMultiplier', unidad: 'fraccion' },
  { valor: 'CREDITS_PER_USD', etiqueta: 'creditsPerUsd', unidad: 'creditosPorDolar' },
]

export function TenantPricingDialog({ organization, onClose }: TenantPricingDialogProps) {
  const { t, i18n } = useTranslation('admin')
  const { token, user } = useAuth()
  const { notify } = useToast()

  const organizationId = organization?.id ?? null
  const clave = token !== null && user?.is_superuser === true && organizationId !== null ? token : null

  const cargar = useCallback((k: string) => getOrganizationPricing(k, organizationId ?? ''), [
    organizationId,
  ])
  const detalle = useAsyncResource(cargar, clave)

  const [operacion, setOperacion] = useState<string>('SCAN_CREDIT_COST')
  const [valor, setValor] = useState('')
  const [motivo, setMotivo] = useState('')
  const [desde, setDesde] = useState('')
  const [guardando, setGuardando] = useState(false)

  const formatearFecha = useCallback(
    (iso: string) =>
      new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' }).format(
        new Date(iso),
      ),
    [i18n.language],
  )

  const pactar = async () => {
    if (organizationId === null) {
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
      const pactado = await createOrganizationPriceOverride(clave ?? '', organizationId, {
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
      // Solo se recarga esta ficha. La lista de tenants **no** cambia: pactar un precio no altera
      // el nombre, el plan ni el saldo de nadie, y recargarla haría parpadear la tabla de fondo
      // por algo que no se ve.
      detalle.reload()
    } catch {
      notify('error', t('tenants.pricing.actions.error'))
    } finally {
      setGuardando(false)
    }
  }

  // Antes del `return null` de abajo, y no despu\u00e9s.
  //
  // ## Por qu\u00e9 importa el orden
  //
  // Porque un hook que se llama a veces s\u00ed y a veces no rompe el orden de los hooks de React. No
  // da error al abrir el di\u00e1logo; da error cuando se cierra y se vuelve a abrir, con un estado de
  // la petici\u00f3n que corresponde a la fila anterior. Es el fallo m\u00e1s dif\u00edcil de ver de esta
  // pantalla, y por eso los hooks van todos arriba del todo, antes de cualquier `return`.
  const cadena = useMemo(
    () => ordenarCadena(detalle.data?.overrides ?? []),
    [detalle.data],
  )
  const resumen = useMemo(() => resumenDeLaCadena(cadena), [cadena])

  if (organization === null) {
    return null
  }

  const precios = detalle.data?.precios ?? null
  const plataforma = detalle.data?.precios_de_plataforma ?? null

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          onClose()
        }
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tenant-pricing-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h2 id="tenant-pricing-title">
            <BadgeDollarSign size={18} aria-hidden="true" />
            <span>{t('tenants.pricing.title')}</span>
          </h2>
          <button className="icon-button" type="button" onClick={onClose}>
            <span className="visually-hidden">{t('tenants.pricing.actions.close')}</span>
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <p className="modal-caption">{organization.name}</p>

        {detalle.loadFailed && detalle.data === null ? (
          <div className="empty-card">
            <p>{t('tenants.pricing.states.error')}</p>
            <button className="secondary-button" type="button" onClick={detalle.reload}>
              <span>{t('tenants.pricing.states.retry')}</span>
            </button>
          </div>
        ) : detalle.isLoading && detalle.data === null ? (
          <p className="cell-muted">{t('tenants.pricing.states.loading')}</p>
        ) : precios !== null && plataforma !== null ? (
          <div className="field-group">
            <h3>{t('tenants.pricing.current.title')}</h3>
            <p className="section-description">{t('tenants.pricing.current.help')}</p>
            <div className="table-wrapper">
              <table className="data-table console-table">
                <caption className="visually-hidden">{t('tenants.pricing.current.title')}</caption>
                <thead>
                  <tr>
                    <th scope="col">{t('tenants.pricing.columns.operation')}</th>
                    <th scope="col">{t('tenants.pricing.columns.paid')}</th>
                    <th scope="col">{t('tenants.pricing.columns.platform')}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <th scope="row">{t('pricing.scalars.fields.scanCost')}</th>
                    <td className="mono">{precios.scan_credit_cost}</td>
                    <td className="mono cell-muted">{plataforma.scan_credit_cost}</td>
                  </tr>
                  <tr>
                    <th scope="row">{t('pricing.scalars.fields.quickMultiplier')}</th>
                    <td className="mono">{precios.quick_scan_credit_multiplier}</td>
                    <td className="mono cell-muted">
                      {plataforma.quick_scan_credit_multiplier}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row">{t('pricing.scalars.fields.creditsPerUsd')}</th>
                    <td className="mono">{precios.credits_per_usd}</td>
                    <td className="mono cell-muted">{plataforma.credits_per_usd}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <h3>{t('tenants.pricing.chain.title')}</h3>
            <p className="section-description">{t('tenants.pricing.chain.help')}</p>
            {cadena.length === 0 ? (
              <p className="cell-muted">{t('tenants.pricing.chain.empty')}</p>
            ) : (
              // El resumen va **encima** de la lista y no al lado: el operador lo lee una vez para
              // saber si hay una renovación pendiente, y al lado tendría que buscarlo en cada fila.
              <>
                <p className="cell-muted">
                  {t('tenants.pricing.chain.summary', {
                    vigente: resumen.vigente,
                    future: resumen.futuro,
                    total: resumen.total,
                  })}
                </p>
                <ul className="pricing-chain">
                  {cadena.map((o) => (
                    <FilaPactado
                      key={o.id}
                      pactado={o}
                      formatearFecha={formatearFecha}
                      etiquetas={t}
                    />
                  ))}
                </ul>
              </>
            )}

            <h3>{t('tenants.pricing.form.title')}</h3>
            <p className="section-description">{t('tenants.pricing.form.help')}</p>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="pactado-operacion">
                  {t('tenants.pricing.form.operation')}
                </label>
                <select
                  id="pactado-operacion"
                  className="select-input"
                  value={operacion}
                  disabled={guardando}
                  onChange={(e) => setOperacion(e.target.value)}
                >
                  {OPERACIONES.map((o) => (
                    <option key={o.valor} value={o.valor}>
                      {t(`pricing.scalars.fields.${o.etiqueta}`)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="pactado-valor">{t('tenants.pricing.form.value')}</label>
                <input
                  id="pactado-valor"
                  type="text"
                  inputMode="decimal"
                  className="mono"
                  value={valor}
                  disabled={guardando}
                  onChange={(e) => setValor(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="pactado-motivo">{t('tenants.pricing.form.motive')}</label>
                <input
                  id="pactado-motivo"
                  type="text"
                  maxLength={255}
                  value={motivo}
                  disabled={guardando}
                  onChange={(e) => setMotivo(e.target.value)}
                />
                <p className="field-help">{t('tenants.pricing.form.motiveHelp')}</p>
              </div>
              <div className="field">
                <label htmlFor="pactado-desde">{t('tenants.pricing.form.from')}</label>
                <input
                  id="pactado-desde"
                  type="datetime-local"
                  value={desde}
                  disabled={guardando}
                  onChange={(e) => setDesde(e.target.value)}
                />
                <p className="field-help">{t('tenants.pricing.form.fromHelp')}</p>
              </div>
            </div>
          </div>
        ) : null}

        <footer className="modal-footer">
          <button className="secondary-button" type="button" onClick={onClose}>
            <span>{t('tenants.pricing.actions.close')}</span>
          </button>
          <button
            className="primary-button"
            type="button"
            disabled={guardando || precios === null}
            onClick={() => void pactar()}
          >
            <span>
              {t(guardando ? 'tenants.pricing.actions.saving' : 'tenants.pricing.actions.pact')}
            </span>
          </button>
        </footer>
      </div>
    </div>
  )
}

interface FilaPactadoProps {
  pactado: OrganizationPriceOverride
  formatearFecha: (iso: string) => string
  etiquetas: (clave: string) => string
}

/**
 * Una fila de la cadena de pactados, con su estado.
 *
 * ## Por qué se pinta el estado y no solo el valor
 *
 * Porque hay tres, y confundirlos cuesta dinero. Un pactado que empieza dentro de un mes y uno que
 * está vigente se ven igual si solo se enseña el número: el comercial firmaría creyendo que el
 * nuevo ya está en vigor, o creyendo que el viejo sigue cuando ya no. La fecha de inicio junto al
 * estado es lo que quita la duda.
 */
function FilaPactado({ pactado, formatearFecha, etiquetas }: FilaPactadoProps) {
  const { t } = useTranslation('admin')
  const nombreOperacion = t(`tenants.pricing.operations.${pactado.operacion}`)
  const estado = estadoDePactado(pactado)
  const claveEstado: Record<EstadoPactado, string> = {
    vigente: 'tenants.pricing.states.live',
    futuro: 'tenants.pricing.states.future',
    caducado: 'tenants.pricing.states.expired',
  }

  return (
    <li className="pricing-chain-item" data-estado={estado}>
      <span className={estado === 'vigente' ? 'badge badge-on' : 'badge badge-muted'}>
        {etiquetas(claveEstado[estado])}
      </span>
      <span className="mono pricing-chain-value">{pactado.valor}</span>
      <span className="cell-muted">{nombreOperacion}</span>
      <span className="cell-muted">{formatearFecha(pactado.valido_desde)}</span>
      <span className="pricing-chain-motive">{pactado.motivo}</span>
    </li>
  )
}
