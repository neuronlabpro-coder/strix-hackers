import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'

export interface DiffViewerProps {
  diff: string
}

/**
 * Renderizador de unified diff sin dependencias: clasifica cada linea por su
 * prefijo y la paints con el tinte correspondiente. El contenido llega del
 * backend ya validado por `patches.parse_patch`.
 */
export function DiffViewer({ diff }: DiffViewerProps) {
  const { t } = useTranslation('issues')
  const lines = useMemo(() => diff.split('\n'), [diff])

  return (
    <div className="code-panel">
      <div className="code-panel-header">
        <p className="code-panel-title">{t('detail.patch.title')}</p>
        <p className="chart-empty">{t('detail.patch.caption')}</p>
      </div>
      <div className="diff-view" role="group" aria-label={t('detail.patch.title')} tabIndex={0}>
        {lines.map((line, index) => {
          const key = `${index}-${line.slice(0, 12)}`
          if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) {
            return (
              <span key={key} className="diff-line diff-hunk">
                {line}
              </span>
            )
          }
          if (line.startsWith('@@')) {
            return (
              <span key={key} className="diff-line diff-hunk">
                {line}
              </span>
            )
          }
          if (line.startsWith('+')) {
            return (
              <span key={key} className="diff-line diff-added">
                {line}
              </span>
            )
          }
          if (line.startsWith('-')) {
            return (
              <span key={key} className="diff-line diff-removed">
                {line}
              </span>
            )
          }
          return (
            <span key={key} className="diff-line diff-context">
              {line}
            </span>
          )
        })}
      </div>
    </div>
  )
}
