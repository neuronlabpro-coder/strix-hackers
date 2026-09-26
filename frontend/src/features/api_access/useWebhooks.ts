import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  createWebhook,
  deleteWebhook,
  getWebhookDeliveries,
  getWebhookEvents,
  listWebhooks,
  pingWebhook,
  updateWebhook,
} from '../../lib/api'
import type {
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEventCatalog,
  WebhookPingResult,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'

/**
 * Estado de la pestaña de webhooks.
 *
 * El catálogo de eventos y los endpoints se piden a la vez porque son independientes: el
 * selector de eventos necesita los nombres para pintar y la tabla necesita las filas, y
 * esperarlos en serie retrasaría el primer pintado sin motivo.
 */
export interface WebhooksState {
  catalog: WebhookEventCatalog | null
  endpoints: WebhookEndpoint[]
  isLoading: boolean
  loadFailed: boolean
  refresh: () => void
  create: (input: { url: string; description: string | null; eventTypes: string[] }) => Promise<string>
  remove: (id: string) => Promise<void>
  setActive: (id: string, isActive: boolean) => Promise<void>
  ping: (id: string) => Promise<WebhookPingResult>
  isMutating: boolean
}

const DELIVERY_PAGE_SIZE = 20

export function useWebhooks(): WebhooksState {
  const { token } = useAuth()
  const [result, setResult] = useState<{
    key: string
    catalog: WebhookEventCatalog | null
    endpoints: WebhookEndpoint[]
    failed: boolean
  }>({ key: '', catalog: null, endpoints: [], failed: false })
  const [isMutating, setIsMutating] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)

  // `requestKey` identifica la petición en curso y el estado sale de compararla durante
  // el render, en vez de escribirse en el efecto. Es la misma forma que usan
  // `useApiAccess` y `useCveCatalog`: escribir `setIsLoading(true)` al principio del
  // efecto provoca un render en cascada, y con un refresco manual se ve el parpadeo.
  const requestKey = `${token ?? ''}|${reloadToken}`
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token) {
      return
    }
    let isActive = true

    void Promise.all([getWebhookEvents(token), listWebhooks(token)])
      .then(([events, page]) => {
        if (!isActive) {
          return
        }
        setResult({ key: requestKey, catalog: events, endpoints: page.items, failed: false })
      })
      .catch(() => {
        if (!isActive) {
          return
        }
        setResult({ key: requestKey, catalog: null, endpoints: [], failed: true })
      })

    return () => {
      isActive = false
    }
  }, [reloadToken, requestKey, token])

  const refresh = useCallback(() => {
    setReloadToken((current) => current + 1)
  }, [])

  const create = useCallback(
    async (input: { url: string; description: string | null; eventTypes: string[] }) => {
      if (!token) {
        throw new ApiError(401)
      }
      const created = await createWebhook(token, {
        url: input.url,
        description: input.description,
        event_types: input.eventTypes,
      })
      setResult((current) => ({ ...current, endpoints: [created, ...current.endpoints] }))
      // El secreto se devuelve al modal y se queda ahí. No se guarda en el estado de la
      // pantalla a propósito: un `useState` con el secreto sobrevive a que el modal se
      // cierre, y eso lo dejaría en un sitio desde el que se podría leer por error.
      return created.signing_secret
    },
    [token],
  )

  const remove = useCallback(
    async (id: string) => {
      if (!token) {
        throw new ApiError(401)
      }
      setIsMutating(true)
      try {
        await deleteWebhook(token, id)
        setResult((current) => ({
          ...current,
          endpoints: current.endpoints.filter((item) => item.id !== id),
        }))
      } finally {
        setIsMutating(false)
      }
    },
    [token],
  )

  const setActive = useCallback(
    async (id: string, isActive: boolean) => {
      if (!token) {
        throw new ApiError(401)
      }
      setIsMutating(true)
      try {
        const updated = await updateWebhook(token, id, { is_active: isActive })
        setResult((current) => ({
          ...current,
          endpoints: current.endpoints.map((item) => (item.id === id ? updated : item)),
        }))
      } finally {
        setIsMutating(false)
      }
    },
    [token],
  )

  const ping = useCallback(
    async (id: string) => {
      if (!token) {
        throw new ApiError(401)
      }
      return pingWebhook(token, id)
    },
    [token],
  )

  return {
    catalog: isCurrent ? result.catalog : null,
    endpoints: isCurrent ? result.endpoints : [],
    isLoading: Boolean(token) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    refresh,
    create,
    remove,
    setActive,
    ping,
    isMutating,
  }
}

/**
 * Historial de entregas de un endpoint.
 *
 * Se pide bajo demanda, al abrir el drawer, y no con el resto de la pantalla. Son filas
 * de diagnóstico que casi nadie mira y que además son las últimas veinte: bajarlas
 * todas al abrir la pantalla multiplicaría la carga sin que nadie lo notara.
 */
export function useDeliveryHistory(endpointId: string | null): {
  deliveries: WebhookDelivery[]
  total: number
  isLoading: boolean
  failed: boolean
  reload: () => void
} {
  const { token } = useAuth()
  const [result, setResult] = useState<{
    key: string
    deliveries: WebhookDelivery[]
    total: number
    failed: boolean
  }>({ key: '', deliveries: [], total: 0, failed: false })
  const [reloadToken, setReloadToken] = useState(0)

  // Mismo criterio que en `useWebhooks`: el estado se deriva comparando la clave de la
  // petición. El `endpointId` va en la clave, así que abrir el historial de otro endpoint
  // invalida el anterior sin necesidad de vaciarlo a mano en el efecto.
  const requestKey = `${token ?? ''}|${endpointId ?? ''}|${reloadToken}`
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !endpointId) {
      return
    }
    let isActive = true

    void getWebhookDeliveries(token, endpointId, DELIVERY_PAGE_SIZE, 0)
      .then((page) => {
        if (!isActive) {
          return
        }
        setResult({
          key: requestKey,
          deliveries: page.items,
          total: page.total,
          failed: false,
        })
      })
      .catch(() => {
        if (!isActive) {
          return
        }
        setResult({ key: requestKey, deliveries: [], total: 0, failed: true })
      })

    return () => {
      isActive = false
    }
  }, [endpointId, reloadToken, requestKey, token])

  return {
    deliveries: isCurrent ? result.deliveries : [],
    total: isCurrent ? result.total : 0,
    isLoading: Boolean(token && endpointId) && !isCurrent && !result.failed,
    failed: isCurrent && result.failed,
    reload: () => setReloadToken((current) => current + 1),
  }
}
