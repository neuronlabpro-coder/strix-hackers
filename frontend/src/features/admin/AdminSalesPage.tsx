import { useCallback } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { getAdminSales } from '../../lib/adminApi'
import { activeLocale, formatCents, formatCredits } from '../../lib/format'
import type { AdminSale } from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { PaginationBar } from './PaginationBar'
import { useAdminPage } from './useAdminPage'

/**
 * Historial de pagos procesados.
 *
 * ## Por qué no hay columna de importe en dólares
 *
 * La tabla de eventos de Stripe registra **qué** se procesó, a qué organización y cuántos
 * créditos acreditó. **No guarda cuánto se cobró**, y sacarlo obligaría a preguntar al
 * proveedor de pago una fila cada vez que se abre la vista.
 *
 * La primera versión de esta pantalla tenía la columna y pintaba un cero cuando el dato
 * no existía. Un cero es un número con aspecto de dato: en una tabla de ventas, una
 * columna que dice «$0,00» para todas las filas informa de que la plataforma no ha facturado
 * nada, que es una conclusión equivocada y cara. Aquí no hay columna de importe: hay
 * créditos, que sí están, y la ausencia de cifra en dólares se explica en el pie en vez
 * de rellenarse con un número inventado.
 *
 * ## Por qué el total es de la página y no del histórico
 *
 * `total_credits` suma lo que se ve. El pie lo dice con la frase «en esta página»: sin
 * ese matiz, el operador lee una suma parcial como la facturación total y toma una
 * decisión comercial sobre ella.
 */
export function AdminSalesPage() {
  const { t } = useTranslation('admin')
  const { token, user } = useAuth()

  const load = useCallback(
    (limit: number, offset: number) =>
      // El hook recibe `disabled` y no pide nada sin sesión; esta guarda solo evita pasar
      // un token inexistente y devuelve una página vacía en vez de lanzar la petición.
      token === null
        ? Promise.resolve({ items: [], total: 0, total_credits: '0', total_amount_cents: 0 })
        : getAdminSales(token, { limit, offset }),
    [token],
  )

  const page = useAdminPage<AdminSale, { total_credits: string; total_amount_cents: number }>(
    load,
    {
      pageSize: 25,
      filterKey: token ?? '',
      disabled: !token || user?.is_superuser !== true,
    },
  )

  /**
   * Las cuatro tarjetas de arriba de la tabla.
   *
   * ## Por qué la cuarta no dice "MRR"
   *
   * Porque el backend **no da un MRR**, y ponerlo sería poner un número que no es ese número.
   *
   * `AdminSalePage.total_amount_cents` y `total_credits` son la suma de **la página que se está
   * viendo**, y el propio esquema lo dice en su docstring: son "de la página, no del histórico",
   * y `total_amount_cents` además solo suma los importes conocidos. La fila que lo deja claro
   * está en el pie de la tabla desde el principio, con la frase "en esta página".
   *
   * Un MRR de verdad necesita agrupar por mes sobre **todo** el histórico, y eso es una consulta
   * distinta que este endpoint no hace. Si se rotula una suma de página como "MRR estimado", el
   * operador toma una decisión comercial —cuota de créditos, precio del siguiente trimestre—
   * sobre una cifra que baja en cuanto cambia de página. Un error de veinte píxeles en el diseño se
   * ve; un error de un veinte por ciento en una cifra que aparece en un sitio y no en otro, no.
   *
   * Así que la cuarta tarjeta apunta al Resumen global, que es donde el MRR sí vive y sí está
   * calculado sobre todo el histórico. Es menos vistosa que un número y mucho más útil.
   */
  const metricasVenta = [
    {
      clave: 'eventos',
      etiqueta: t('sales.kpi.events'),
      valor: String(page.total),
      pista: t('sales.kpi.eventsHint'),
      monospace: true,
    },
    {
      clave: 'importe',
      etiqueta: t('sales.kpi.amount'),
      valor: formatCents(page.extra?.total_amount_cents ?? 0),
      pista: t('sales.kpi.pageScopedHint'),
      monospace: false,
    },
    {
      clave: 'creditos',
      etiqueta: t('sales.kpi.credits'),
      valor: formatCredits(page.extra?.total_credits ?? '0'),
      pista: t('sales.kpi.pageScopedHint'),
      monospace: false,
    },
    {
      clave: 'mrr',
      etiqueta: t('sales.kpi.mrr'),
      valor: t('sales.kpi.mrrElsewhere'),
      pista: t('sales.kpi.mrrHint'),
      monospace: false,
      enlace: '/admin',
    },
  ]

  return (
    <section className="page-section" aria-labelledby="admin-sales-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="admin-sales-title">{t('sales.title')}</h1>
          <p className="page-description">{t('sales.description')}</p>
        </div>
        <div className="page-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={page.refresh}
            disabled={page.isLoading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{t('overview.refresh')}</span>
          </button>
        </div>
      </div>

      {/* Las tarjetas van **antes** de la tabla y no en el pie, que es donde ya estaban los
          totales como texto suelto. La razon no es estetica: un total al final de una tabla de
          veinte filas hay que_ir a buscarlo, y el operador que entra en "Ventas" para saber como
          va el mes no baja hasta el final para leerlo.

          Y la razon de que sean cuatro y no una es que cada cifra dice una cosa distinta, y
          confundirlas es el error que hacen estas pantallas. Ver el comentario de `metricasVenta`
          sobre por que la cuarta no lleva MRR. */}
      <div className="metric-grid metric-grid-four">
        {metricasVenta.map((metrica) => (
          <article className="metric-card" key={metrica.clave}>
            <p className="metric-label">{metrica.etiqueta}</p>
            {/*
              El enlace es opcional y no se decide aqui con un ternario sobre el texto: se
              decide con el elemento. Un `<span>` que actua de enlace no se puede pulsar con el
              teclado ni Announces, y "ver resumen" sin ser un enlace es un texto que promete
              algo que no hace.
            */}
            {metrica.enlace ? (
              <p className="metric-value metric-value-sm">
                <Link className="metric-link" to={metrica.enlace}>
                  {metrica.valor}
                </Link>
              </p>
            ) : (
              <p className={metrica.monospace ? 'metric-value metric-value-sm' : 'metric-value'}>
                {metrica.valor}
              </p>
            )}
            <p className="metric-hint">{metrica.pista}</p>
          </article>
        ))}
      </div>

      {page.loadFailed && page.items.length === 0 ? (
        <div className="empty-card">
          <h2>{t('states.error')}</h2>
          <button className="secondary-button" type="button" onClick={page.refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : page.isLoading && page.items.length === 0 ? (
        <div className="console-skeleton" aria-hidden="true">
          {Array.from({ length: 6 }, (_, indice) => (
            <div className="console-skeleton-row" key={indice}>
              <div className="console-skeleton-bar" style={{ flex: '2 1 0' }} />
              <div className="console-skeleton-bar" style={{ flex: '1 1 0' }} />
              <div className="console-skeleton-bar" style={{ flex: '3 1 0' }} />
              <div className="console-skeleton-bar" style={{ flex: '1 1 0' }} />
            </div>
          ))}
        </div>
      ) : page.items.length === 0 ? (
        <div className="empty-card">
          <p>{t('sales.empty')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table console-table">
            <caption className="visually-hidden">{t('sales.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('sales.columns.event')}</th>
                <th scope="col">{t('sales.columns.type')}</th>
                <th scope="col">{t('sales.columns.tenant')}</th>
                <th scope="col">{t('sales.columns.amount')}</th>
                <th scope="col">{t('sales.columns.credits')}</th>
                <th scope="col">{t('sales.columns.created')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((sale) => (
                <tr key={sale.id}>
                  <th scope="row" className="cell-primary">
                    <span className="mono">{sale.event_id}</span>
                  </th>
                  <td>
                    {/*
                      `checkout.session.completed` es el nombre del evento en el proveedor de
                      cobro, no un tipo de evento de esta plataforma. En una columna titulada
                      "Tipo" al lado de "Importe" y "Créditos", lo que se quiere leer es que ese
                      importe sí acreditó créditos, y eso es lo que dice la etiqueta.

                      El `title` deja el identificador del proveedor a un hover, porque hace falta
                      para casar la fila con el evento en el panel del proveedor. Y el
                      `defaultValue` es el identificador en crudo: el proveedor puede anadir tipos
                      que este backend no registra, y en ese caso ensenar `charge.dispute.created`
                      es correcto, mientras que inventarle una etiqueta no lo seria.
                    */}
                    <span className="mono cell-muted" title={sale.event_type}>
                      {t(`sales.types.${sale.event_type}`, { defaultValue: sale.event_type })}
                    </span>
                  </td>
                  <td>
                    {sale.organization_name ?? (
                      <span className="cell-muted">{t('sales.noTenant')}</span>
                    )}
                  </td>
                  {/*
                    El importe viene en centavos y se pinta como dólares con el separador
                    del idioma activo. `null` —un evento que no fue un cobro— se muestra
                    como «no aplica» y **no** como $0,00: un cero en una columna de
                    importes se lee como una venta de nada, que es una conclusión falsa.
                  */}
                  <td>
                    {sale.amount_cents === null ? (
                      <span className="cell-muted">{t('sales.notApplicable')}</span>
                    ) : (
                      <span className="mono">{formatCents(sale.amount_cents)}</span>
                    )}
                  </td>
                  <td>
                    {sale.credits_granted === null ? (
                      <span className="badge">{t('sales.notPurchase')}</span>
                    ) : (
                      <span className="mono delta-positive">
                        +{formatCredits(sale.credits_granted)}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="mono timestamp">
                      {new Intl.DateTimeFormat(activeLocale(), {
                        dateStyle: 'medium',
                        timeStyle: 'short',
                      }).format(new Date(sale.created_at))}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PaginationBar page={page} />

      <p className="chart-empty">
        {t('sales.total', {
          count: page.total,
          credits: formatCredits(page.extra?.total_credits ?? '0'),
        })}
      </p>
      <p className="chart-empty">
        {t('sales.totalAmount', {
          amount: formatCents(page.extra?.total_amount_cents ?? 0),
        })}
      </p>
      <p className="chart-empty">{t('sales.totalCaption')}</p>
    </section>
  )
}
