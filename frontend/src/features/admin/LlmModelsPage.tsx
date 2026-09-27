import { useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { LLMModelConfig, LLMModelUpdatePayload, LLMUseCase } from '../../types/api'
import { AddLlmModelForm } from './AddLlmModelForm'
import { LLM_USE_CASES } from './llmUseCases'
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
                    priority: t('table.priority'),
                    useCaseLabel: t('table.useCase'),
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
  /** Etiqueta accesible del campo de prioridad, para el lector de pantalla. */
  priority: string
  /** Etiqueta accesible del selector de caso de uso. */
  useCaseLabel: string
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
  onUpdate: (payload: LLMModelUpdatePayload) => void
  labels: RowLabels
}) {
  const [marginDraft, setMarginDraft] = useState(model.markup_pct)
  const marginChanged = marginDraft !== model.markup_pct
  const marginMultiplier = 1 + Number(model.markup_pct) / 100
  const hasUsage = model.usage.runs > 0

  return (
    <tr className={model.is_active ? undefined : 'row-inactive'}>
      <td>
        {/*
          La prioridad es un `<select>` y no un badge porque **ordena**. Cambiarla desde la
          lista es la operacion de reordenar el enrutado, y tener que abrir un dialogo para
          mover un modelo un puesto hacia arriba obliga a recorrer la tabla entera para no
          equivocar el destino.

          El numero que se ve es el valor del campo, no una posicion en la tabla: con dos
          modelos en la prioridad 1, quien lo elige no quiere el "primero de la pantalla",
          quiere el 1. Por eso el `value` es `model.priority_order` y no el indice de la fila.
        */}
        <label className="margin-field">
          <span className="visually-hidden">
            {`${labels.priority}: ${model.model_id}`}
          </span>
          <input
            type="number"
            min="1"
            step="1"
            className="mono priority-badge"
            value={model.priority_order}
            disabled={isPending}
            onChange={(event) => onUpdate({ priority_order: Number(event.target.value) })}
          />
        </label>
        <p className="chart-empty">{labels.priorityLabel(model.priority_order)}</p>
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
        {/*
          El caso de uso decide a que bucle ofensivo se enruta el modelo: `DEEP_PENTEST` y
          `QUICK_SCAN` para el bucle en terminal, `AUTOFIX` para la generacion del parche, y
          `ALL` para los transversales.

          Editarlo por fila y no solo en el alta es lo que hace falta: el catalogo se reenruta
          cada vez que aparece un modelo mejor, y pedir un alta nueva para cambiar una
          etiqueta que ya existe seria dejar el catalogo lleno de duplicados del mismo
          proveedor.
        */}
        <label className="margin-field">
          <span className="visually-hidden">
            {`${labels.useCaseLabel}: ${model.model_id}`}
          </span>
          <select
            value={model.use_case}
            disabled={isPending}
            onChange={(event) =>
              onUpdate({ use_case: event.target.value as LLMUseCase })
            }
          >
            {LLM_USE_CASES.map((value) => (
              <option key={value} value={value}>
                {labels.useCase(value)}
              </option>
            ))}
          </select>
        </label>
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
