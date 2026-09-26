import { useTranslation } from 'react-i18next'

import type { CVESeverity } from '../../types/api'

export interface SeverityBadgeProps {
  severity: CVESeverity
}

/**
 * Badge de severidad cromático.
 *
 * La rampa de severidad viene tokenizada en `design-dark.md` y aquí solo se le pasa
 * el valor. El texto lleva el nombre traducido además del color, porque el color
 * por sí solo no comunica nada a quien no distingue rojo de naranja, y el color es
 * información redundante: el rótulo ya dice "Crítica".
 */
export function SeverityBadge({ severity }: SeverityBadgeProps) {
  const { t } = useTranslation('cve')
  return (
    <span className={`badge badge-status-${severity.toLowerCase()}`}>
      {t(`severity.${severity}`)}
    </span>
  )
}
