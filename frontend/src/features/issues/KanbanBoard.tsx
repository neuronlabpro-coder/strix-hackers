import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import type { IssueStatus, VulnerabilityListItem, VulnerabilitySeverity } from '../../types/api'
import { STATUSES, type IssuesViewMode } from './useIssues'
import { useTriage } from './useTriage'

const SEVERITY_CLASS: Record<VulnerabilitySeverity, string> = {
  CRITICAL: 'severity-swatch-critical',
  HIGH: 'severity-swatch-high',
  MEDIUM: 'severity-swatch-medium',
  LOW: 'severity-swatch-low',
  INFO: 'severity-swatch-info',
}

export interface KanbanBoardProps {
  findings: VulnerabilityListItem[]
  viewMode: IssuesViewMode
}

/**
 * Tablero de triaje por estado de remediación.
 *
 * El arrastre con puntero es la vía rápida, pero no la única: cada tarjeta
 * expone un `<select>` de destino en su menú de contexto. Un tablero que solo
 * se pudiera usar arrastrando dejaría fuera a quien navega con teclado o usa
 * lector de pantalla, y el cambio de estado es la acción central de la vista.
 */
export function KanbanBoard({ findings }: KanbanBoardProps) {
  const { t } = useTranslation('issues')
  const { t: tTriage } = useTranslation('triage')
  const navigate = useNavigate()
  const { triage, isPending } = useTriage()
  const [draggedId, setDraggedId] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<IssueStatus | null>(null)

  const moveFinding = (finding: VulnerabilityListItem, nextStatus: IssueStatus) => {
    void triage(finding, nextStatus)
  }

  const handleDrop = (status: IssueStatus) => {
    if (draggedId !== null) {
      const finding = findings.find((item) => item.id === draggedId)
      if (finding && finding.status !== status) {
        moveFinding(finding, status)
      }
    }
    setDraggedId(null)
    setDropTarget(null)
  }

  return (
    <div className="board" role="list" aria-label={t('board.caption')}>
      {STATUSES.map((status) => {
        const column = findings.filter((finding) => finding.status === status)
        const isTarget = dropTarget === status
        return (
          <div
            key={status}
            role="listitem"
            className={isTarget ? 'board-column board-column-target' : 'board-column'}
            onDragOver={(event) => {
              event.preventDefault()
              setDropTarget(status)
            }}
            onDragLeave={() => setDropTarget(null)}
            onDrop={(event) => {
              event.preventDefault()
              handleDrop(status)
            }}
          >
            <div className="board-column-header">
              <span>{t(`status.${status}`)}</span>
              <span>{column.length}</span>
            </div>
            {column.length === 0 ? (
              <p className="chart-empty">{t('board.empty')}</p>
            ) : (
              column.map((finding) => (
                <KanbanCard
                  key={finding.id}
                  finding={finding}
                  isPending={isPending(finding.id)}
                  onOpen={() => navigate(`/issues/${finding.id}`)}
                  onMove={(nextStatus) => moveFinding(finding, nextStatus)}
                  labels={{
                    moveTo: tTriage('moveTo'),
                    statuses: STATUSES,
                    statusLabel: (value: IssueStatus) => t(`status.${value}`),
                  }}
                />
              ))
            )}
          </div>
        )
      })}
    </div>
  )
}

interface KanbanCardLabels {
  moveTo: string
  statuses: readonly IssueStatus[]
  statusLabel: (status: IssueStatus) => string
}

function KanbanCard({
  finding,
  isPending,
  onOpen,
  onMove,
  labels,
}: {
  finding: VulnerabilityListItem
  isPending: boolean
  onOpen: () => void
  onMove: (status: IssueStatus) => void
  labels: KanbanCardLabels
}) {
  const options = labels.statuses.filter((status) => status !== finding.status)

  return (
    <div
      className={isPending ? 'board-card board-card-pending' : 'board-card'}
      draggable={!isPending}
      onDragStart={(event) => {
        event.dataTransfer.setData('text/plain', finding.id)
        event.dataTransfer.effectAllowed = 'move'
      }}
      data-finding-id={finding.id}
    >
      <button className="board-card-open" type="button" onClick={onOpen}>
        <span>{finding.title}</span>
        <span className="board-card-meta">
          <span
            className={`severity-swatch ${SEVERITY_CLASS[finding.severity]}`}
            aria-hidden="true"
          />
          <span className="mono">{finding.cvss_score.toFixed(1)}</span>
          <span className="mono">{finding.affected_target}</span>
        </span>
      </button>
      {options.length > 0 ? (
        <label className="board-card-move">
          <span className="visually-hidden">{`${labels.moveTo}: ${finding.title}`}</span>
          <select
            value=""
            disabled={isPending}
            onChange={(event) => {
              const next = event.target.value
              if (next) {
                onMove(next as IssueStatus)
              }
            }}
          >
            <option value="">{labels.moveTo}</option>
            {options.map((status) => (
              <option key={status} value={status}>
                {labels.statusLabel(status)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </div>
  )
}
