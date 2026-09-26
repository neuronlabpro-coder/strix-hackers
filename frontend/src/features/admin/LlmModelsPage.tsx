import { useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { LLMModelConfig } from '../../types/api'
import { AddLlmModelForm } from './AddLlmModelForm'
import { useLlmConsole } from './useLlmConsole'

export function LlmModelsPage() {
  const { t } = useTranslation('llm')
  const { t: tCommon } = useTranslation('common')
  const { i18n } = useTranslation()
  const state = useLlmConsole()
  const [isFormOpen, setIsFormOpen] = useState(false)
  const locale = i18n.language

  const totalCost = state.models.reduce(
    (total, model) => total + Number(model.usage.base_cost_usd),
    0,
  )
  const totalProfit = state.models.reduce(
    (total, model) => total + Number(model.usage.net_profit_usd),
    0,
  )
  const totalRuns = state.models.reduce((total, model) => total + model.usage.runs, 0)

  return (
    <section className="page-section" aria-labelledby="llm-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="llm-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
        <div className="page-actions">
          <button className="secondary-button" type="button" onClick={state.refresh}>
            <RefreshCw size={16} aria-hidden="true" />
            <span>{tCommon('actions.refresh')}</span>
          </button>
          <button className="primary-button" type="button" onClick={() => setIsFormOpen(true)}>
            <Plus size={17} aria-hidden="true" />
            <span>{t('form.title')}</span>
          </button>
        </div>
      </div>

      <div className="metric-grid">
        <article className="metric-card">
          <p className="eyebrow">{t('metrics.runs')}</p>
          <strong className="metric-value mono">{totalRuns}</strong>
          <span className="metric-caption">{t('metrics.title')}</span>
        </article>
        <article className="metric-card">
          <p className="eyebrow">{t('metrics.cost')}</p>
          <strong className="metric-value mono">
            {formatUsd(totalCost, locale)}
          </strong>
          <span className="metric-caption">{t('metrics.title')}</span>
        </article>
        <article className="metric-card">
          <p className="eyebrow">{t('metrics.profit')}</p>
          <strong className="metric-value mono">
            {formatUsd(totalProfit, locale)}
          </strong>
          <span className="metric-caption">{t('metrics.marginEffective')}</span>
        </article>
      </div>

      {state.isLoading ? (
        <div className="empty-card">
          <p>{t('states.loading')}</p>
        </div>
      ) : state.loadFailed ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button className="secondary-button" type="button" onClick={state.refresh}>
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="visually-hidden">{t('title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('table.priority')}</th>
                <th scope="col">{t('table.model')}</th>
                <th scope="col">{t('table.cost')}</th>
                <th scope="col">{t('table.margin')}</th>
                <th scope="col">{t('table.price')}</th>
                <th scope="col">{t('table.useCase')}</th>
                <th scope="col">{t('table.usage')}</th>
                <th scope="col">{t('table.status')}</th>
              </tr>
            </thead>
            <tbody>
              {state.models.map((model) => (
                <LlmModelRow
                  key={model.id}
                  model={model}
                  isPending={state.pendingIds.has(model.id)}
                  onUpdate={(payload) => void state.update(model.id, payload)}
                  labels={{
                    activate: t('actions.activate'),
                    deactivate: t('actions.deactivate'),
                    save: t('actions.save'),
                    saving: t('form.submitting'),
                    input: t('columns.input'),
                    output: t('columns.output'),
                    noUsage: t('metrics.noUsage'),
                    priorityLabel: (order: number) => t(`priorityLabel.${order}`),
                    useCase: (useCase: string) => t(`useCase.${useCase}`),
                    costLabel: t('table.cost'),
                    priceLabel: t('table.price'),
                    locale,
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="chart-empty">{t('hints.fallback')}</p>

      <AddLlmModelForm
        console={state}
        isOpen={isFormOpen}
        onClose={() => setIsFormOpen(false)}
      />
    </section>
  )
}

interface RowLabels {
  activate: string
  deactivate: string
  save: string
  saving: string
  input: string
  output: string
  noUsage: string
  priorityLabel: (order: number) => string
  useCase: (useCase: string) => string
  costLabel: string
  priceLabel: string
  locale: string
}

function LlmModelRow({
  model,
  isPending,
  onUpdate,
  labels,
}: {
  model: LLMModelConfig
  isPending: boolean
  onUpdate: (payload: { is_active?: boolean; markup_pct?: string }) => void
  labels: RowLabels
}) {
  const [marginDraft, setMarginDraft] = useState(model.markup_pct)
  const marginChanged = marginDraft !== model.markup_pct
  const marginMultiplier = 1 + Number(model.markup_pct) / 100
  const hasUsage = model.usage.runs > 0

  return (
    <tr className={model.is_active ? undefined : 'row-inactive'}>
      <td>
        <span className="provider-cell">
          <span className="mono priority-badge">{model.priority_order}</span>
          <span className="chart-empty">{labels.priorityLabel(model.priority_order)}</span>
        </span>
      </td>
      <td>
        <span className="mono">{model.model_id}</span>
        <p className="chart-empty">{model.display_name}</p>
      </td>
      <td>
        <span className="mono">
          {formatUsd(model.base_cost_input_m, labels.locale)} {labels.input}
        </span>
        <p className="chart-empty mono">
          {formatUsd(model.base_cost_output_m, labels.locale)} {labels.output}
        </p>
      </td>
      <td>
        <label className="margin-field">
          <span className="visually-hidden">
            {`${labels.save}: ${model.model_id}`}
          </span>
          <input
            type="number"
            min="0"
            step="0.01"
            className="mono"
            value={marginDraft}
            disabled={isPending}
            onChange={(event) => setMarginDraft(event.target.value)}
          />
          <span className="chart-empty">%</span>
        </label>
        {marginChanged ? (
          <button
            className="secondary-button"
            type="button"
            disabled={isPending}
            onClick={() => onUpdate({ markup_pct: marginDraft })}
          >
            <span>{isPending ? labels.saving : labels.save}</span>
          </button>
        ) : null}
      </td>
      <td>
        <span className="mono">
          {formatUsd(String(Number(model.base_cost_input_m) * marginMultiplier), labels.locale)}
        </span>
        <p className="chart-empty mono">×{marginMultiplier.toFixed(2)}</p>
      </td>
      <td>
        <span className="badge">{labels.useCase(model.use_case)}</span>
      </td>
      <td>
        {hasUsage ? (
          <>
            <span className="mono">
              {new Intl.NumberFormat(labels.locale).format(
                model.usage.prompt_tokens + model.usage.completion_tokens,
              )}
            </span>
            <p className="chart-empty mono">
              {formatUsd(model.usage.net_profit_usd, labels.locale)} {labels.priceLabel.toLowerCase()}
            </p>
          </>
        ) : (
          <span className="chart-empty">{labels.noUsage}</span>
        )}
      </td>
      <td>
        <label className="switch">
          <input
            type="checkbox"
            checked={model.is_active}
            disabled={isPending}
            aria-label={`${model.is_active ? labels.deactivate : labels.activate}: ${model.model_id}`}
            onChange={(event) => onUpdate({ is_active: event.target.checked })}
          />
          <span className="switch-track" aria-hidden="true">
            <span className="switch-thumb" />
          </span>
        </label>
        <p className="chart-empty">
          {model.is_active ? labels.activate : labels.deactivate}
        </p>
      </td>
    </tr>
  )
}

function formatUsd(value: string | number, locale: string): string {
  const amount = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(amount)) {
    return '—'
  }
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 4,
    minimumFractionDigits: 2,
  }).format(amount)
}
