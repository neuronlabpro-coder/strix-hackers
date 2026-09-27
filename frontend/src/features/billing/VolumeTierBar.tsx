import { useTranslation } from 'react-i18next'

import { formatNumber, formatUsd } from '../../lib/format'
import type { VolumePricing } from '../../types/billing'
import { tierIndexFor, type SpendRange } from './volumePricing'

/**
 * Barra segmentada de la escalera de descuento.
 *
 * Es el **único** componente de este módulo, y las funciones de la escalera viven en
 * `volumePricing.ts`. Un archivo que exporta componentes y funciones rompe el refresco en
 * caliente: al guardar, el cargador solo se invalida si el módulo tiene un único tipo de
 * export, y con dos obliga a recargar la página.
 *
 * Un único segmento en acento marca dónde está el cliente. Con cinco del mismo color el ojo
 * no tiene por dónde empezar, y con el activo en verde no hace falta ni flecha ni etiqueta
 * flotante.
 */
interface VolumeTierBarProps {
  pricing: VolumePricing
  ranges: readonly SpendRange[]
  /** Gasto actual en dólares. */
  spend: number
}

export function VolumeTierBar({ pricing, ranges, spend }: VolumeTierBarProps) {
  const { t } = useTranslation('billing')
  const activo = tierIndexFor(spend, ranges)

  return (
    <div
      className="volume-bar"
      role="img"
      aria-label={t('volume.barLabel', {
        count: ranges.length,
        position: activo >= 0 ? String(activo + 1) : t('volume.noPosition'),
      })}
    >
      {pricing.tiers.map((tier, index) => {
        const range = ranges[index]
        const esActivo = index === activo
        const alcanzado = activo >= index && activo !== -1
        return (
          <div
            key={`${range.min}-${tier.discount}`}
            className={[
              'volume-bar-segment',
              alcanzado ? 'volume-bar-segment-reached' : '',
              esActivo ? 'volume-bar-segment-active' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            title={t('volume.tierTooltip', {
              from: formatUsd(range.min),
              to: formatUsd(range.max),
              discount: formatNumber(Number(tier.discount) * 100, { maximumFractionDigits: 0 }),
            })}
          >
            <span className="volume-bar-label">
              {t('volume.tierBadge', {
                pct: formatNumber(Number(tier.discount) * 100, { maximumFractionDigits: 0 }),
              })}
            </span>
            <span className="volume-bar-range">{formatUsd(range.min)}</span>
          </div>
        )
      })}
    </div>
  )
}
