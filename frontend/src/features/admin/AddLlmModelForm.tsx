import { useEffect, useRef } from 'react'
import { LoaderCircle, Plus, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { LLMModelCreatePayload, LLMUseCase } from '../../types/api'
import type { LlmConsoleState } from './useLlmConsole'

const USE_CASES: LLMUseCase[] = ['ALL', 'QUICK_SCAN', 'DEEP_PENTEST', 'AUTOFIX']

const FIELD_IDS = {
  model_id: 'llm-model-id',
  display_name: 'llm-display-name',
  base_cost_input_m: 'llm-cost-input',
  base_cost_output_m: 'llm-cost-output',
  markup_pct: 'llm-margin',
  priority_order: 'llm-priority',
} as const

export function AddLlmModelForm({
  console: state,
  isOpen,
  onClose,
}: {
  console: LlmConsoleState
  isOpen: boolean
  onClose: () => void
}) {
  const { t } = useTranslation('llm')
  const dialogRef = useRef<HTMLDivElement>(null)
  const { form, setForm, submit, isCreating, createFailed } = state

  useEffect(() => {
    if (!isOpen) {
      return
    }
    const previouslyFocused = document.activeElement as HTMLElement | null
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select, [tabindex]:not([tabindex="-1"])',
    )
    focusable?.[0]?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab' || !focusable || focusable.length === 0) {
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      previouslyFocused?.focus()
    }
  }, [isOpen, onClose])

  if (!isOpen) {
    return null
  }

  const setField = <K extends keyof LLMModelCreatePayload>(
    key: K,
    value: LLMModelCreatePayload[K],
  ) => setForm({ ...form, [key]: value })

  const isValid = form.model_id.includes('/') && form.display_name.trim().length > 0

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-llm-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow">{t('eyebrow')}</p>
            <h2 id="add-llm-title">{t('form.title')}</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label={t('form.close')}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <section className="modal-section">
          <div className="filter-field">
            <label htmlFor={FIELD_IDS.model_id}>{t('form.modelId')}</label>
            <input
              id={FIELD_IDS.model_id}
              type="text"
              className="mono"
              value={form.model_id}
              onChange={(event) => setField('model_id', event.target.value)}
            />
            <p className="chart-empty">{t('form.modelIdHint')}</p>
          </div>

          <div className="filter-field">
            <label htmlFor={FIELD_IDS.display_name}>{t('form.displayName')}</label>
            <input
              id={FIELD_IDS.display_name}
              type="text"
              value={form.display_name}
              onChange={(event) => setField('display_name', event.target.value)}
            />
          </div>

          <div className="filter-bar">
            <div className="filter-field">
              <label htmlFor={FIELD_IDS.base_cost_input_m}>{t('form.costInput')}</label>
              <input
                id={FIELD_IDS.base_cost_input_m}
                type="number"
                min="0"
                step="0.0001"
                className="mono"
                value={form.base_cost_input_m}
                onChange={(event) => setField('base_cost_input_m', event.target.value)}
              />
            </div>
            <div className="filter-field">
              <label htmlFor={FIELD_IDS.base_cost_output_m}>{t('form.costOutput')}</label>
              <input
                id={FIELD_IDS.base_cost_output_m}
                type="number"
                min="0"
                step="0.0001"
                className="mono"
                value={form.base_cost_output_m}
                onChange={(event) => setField('base_cost_output_m', event.target.value)}
              />
            </div>
          </div>

          <div className="filter-field">
            <label htmlFor={FIELD_IDS.markup_pct}>{t('form.margin')}</label>
            <input
              id={FIELD_IDS.markup_pct}
              type="number"
              min="0"
              step="0.01"
              className="mono"
              value={form.markup_pct}
              onChange={(event) => setField('markup_pct', event.target.value)}
            />
            <p className="chart-empty">{t('form.marginHint')}</p>
          </div>

          <div className="filter-bar">
            <div className="filter-field">
              <label htmlFor={FIELD_IDS.priority_order}>{t('form.priority')}</label>
              <input
                id={FIELD_IDS.priority_order}
                type="number"
                min="1"
                step="1"
                className="mono"
                value={form.priority_order}
                onChange={(event) => setField('priority_order', Number(event.target.value))}
              />
              <p className="chart-empty">{t('form.priorityHint')}</p>
            </div>
            <div className="filter-field">
              <label htmlFor="llm-use-case">{t('form.useCase')}</label>
              <select
                id="llm-use-case"
                value={form.use_case}
                onChange={(event) => setField('use_case', event.target.value as LLMUseCase)}
              >
                {USE_CASES.map((useCase) => (
                  <option key={useCase} value={useCase}>
                    {t(`useCase.${useCase}`)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {createFailed ? (
            <p className="inline-notice inline-notice-warning" role="alert">
              {t('form.error')}
            </p>
          ) : null}

          <button
            className="primary-button"
            type="button"
            disabled={!isValid || isCreating}
            onClick={() => void submit().then((created) => (created ? onClose() : null))}
          >
            {isCreating ? (
              <LoaderCircle size={16} className="spin" aria-hidden="true" />
            ) : (
              <Plus size={16} aria-hidden="true" />
            )}
            <span>{isCreating ? t('form.submitting') : t('form.submit')}</span>
          </button>
        </section>
      </div>
    </div>
  )
}
