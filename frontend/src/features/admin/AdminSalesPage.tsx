import { useCallback } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

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

      {page.loadFailed && page.items.length === 0 ? (
        <div className="empty-card">
          <h2>{t('states.error')}</h2>
          <button className="secondary-button" type="button" onClick={page.refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : page.items.length === 0 ? (
        <div className="empty-card">
          <p>{t('sales.empty')}</p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
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
                    <span className="mono cell-muted">{sale.event_type}</span>
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
