import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { createLLMModel, getLLMModels, updateLLMModel } from '../../lib/api'
import type {
  LLMModelConfig,
  LLMModelCreatePayload,
  LLMModelUpdatePayload,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'
import { useToast } from '../shared/toast-context'

const EMPTY_FORM: LLMModelCreatePayload = {
  model_id: '',
  display_name: '',
  base_cost_input_m: '0.00',
  base_cost_output_m: '0.00',
  markup_pct: '0.00',
  priority_order: 1,
  is_active: true,
  use_case: 'ALL',
}

export interface LlmConsoleState {
  models: LLMModelConfig[]
  isLoading: boolean
  loadFailed: boolean
  pendingIds: ReadonlySet<string>
  isCreating: boolean
  createFailed: boolean
  form: LLMModelCreatePayload
  setForm: (form: LLMModelCreatePayload) => void
  refresh: () => void
  submit: () => Promise<boolean>
  update: (modelId: string, payload: LLMModelUpdatePayload) => Promise<boolean>
}

/**
 * Consola de modelos de lenguaje.
 *
 * El margen se edita como texto libre y no como `input[type=range]`: un margen
 * del 275 % es una decisión comercial legítima y un control deslizante lo haría
 * innecesariamente difícil de alcanzar con precisión. El deslizante tiene
 * sentido cuando el rango de valores es pequeño y acotado, que no es el caso.
 */
export function useLlmConsole(): LlmConsoleState {
  const { t } = useTranslation('llm')
  const { token, selectedOrganizationId } = useAuth()
  const { notify } = useToast()
  const [models, setModels] = useState<LLMModelConfig[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set())
  const [isCreating, setIsCreating] = useState(false)
  const [createFailed, setCreateFailed] = useState(false)
  const [form, setForm] = useState<LLMModelCreatePayload>(EMPTY_FORM)
  const [reloadToken, setReloadToken] = useState(0)

  // `load` es estable porque solo depende del token y del tenant, de modo que el
  // efecto no se reejecuta en cada render. El estado de carga se escribe en el
  // evento que lo provoca (el botón de recarga), nunca al inicio del efecto.
  const load = useCallback(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    void getLLMModels(token, selectedOrganizationId)
      .then((page) => {
        setModels(page.items)
        setLoadFailed(false)
      })
      .catch(() => {
        setLoadFailed(true)
      })
      .finally(() => {
        setIsLoading(false)
      })
  }, [selectedOrganizationId, token])

  useEffect(() => {
    load()
  }, [load, reloadToken])

  // Sin sesión no hay nada que cargar: `isLoading` se deriva para que la vista no
  // se quede en el estado de carga indefinidamente.
  const isReady = Boolean(token && selectedOrganizationId)
  const effectiveIsLoading = isReady && isLoading

  const refresh = useCallback(() => {
    if (isReady) {
      setIsLoading(true)
    }
    setReloadToken((current) => current + 1)
  }, [isReady])

  const submit = useCallback(async (): Promise<boolean> => {
    if (!token || !selectedOrganizationId) {
      return false
    }
    setIsCreating(true)
    setCreateFailed(true)
    try {
      await createLLMModel(token, selectedOrganizationId, form)
      setForm(EMPTY_FORM)
      setCreateFailed(false)
      notify('success', t('form.success'))
      load()
      return true
    } catch {
      setCreateFailed(true)
      return false
    } finally {
      setIsCreating(false)
    }
  }, [form, load, notify, selectedOrganizationId, t, token])

  const update = useCallback(
    async (modelId: string, payload: LLMModelUpdatePayload): Promise<boolean> => {
      if (!token || !selectedOrganizationId) {
        return false
      }
      setPendingIds((current) => new Set(current).add(modelId))
      try {
        const updated = await updateLLMModel(token, selectedOrganizationId, modelId, payload)
        setModels((current) =>
          current.map((model) => (model.id === modelId ? updated : model)),
        )
        notify('success', t('actions.save'))
        return true
      } catch {
        notify('error', t('form.error'))
        return false
      } finally {
        setPendingIds((current) => {
          const next = new Set(current)
          next.delete(modelId)
          return next
        })
      }
    },
    [notify, selectedOrganizationId, t, token],
  )

  return {
    models,
    isLoading: effectiveIsLoading,
    loadFailed,
    pendingIds,
    isCreating,
    createFailed,
    form,
    setForm,
    refresh,
    submit,
    update,
  }
}
