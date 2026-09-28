import { useCallback, useEffect, useState } from 'react'

import {
  createKnowledgeDocument,
  deleteKnowledgeDocument,
  getKnowledgeDocuments,
} from '../../lib/api'
import type { KnowledgeDocument, KnowledgeDocType } from '../../types/api'
import { useAuth } from '../auth/useAuth'

/**
 * Los documentos del workspace y las operaciones sobre ellos.
 *
 * ## Por qué el estado es `{ clave, ... }` y no tres `useState` sueltos
 *
 * Porque las peticiones son asíncronas y las respuestas llegan tarde. Con el resultado en un
 * objeto que lleva la clave de la petición que lo produjo, `isLoading` es "la clave que tengo no
 * es la que pedí" y no necesita ningún `setState` de carga que arranque un segundo render. Es el
 * mismo patrón que usa `useKnowledge` para el catálogo técnico, y por eso los dos se leen igual.
 *
 * ## Por qué la recarga es optimista al borrar
 *
 * Porque el borrado de un documento es una acción immediate en la interfaz y el usuario espera
 * que la tarjeta desaparezca. Si la tarjeta se queda hasta que responde el servidor, con una red
 * lenta parece que el botón no funciona y lo pulsa otra vez, y la segunda pulsación ya no tiene
 * nada que borrar. Si la petición falla, la tarjeta vuelve.
 */

const PAGE_SIZE = 50

export interface DocumentosState {
  documents: KnowledgeDocument[]
  total: number
  isLoading: boolean
  loadFailed: boolean
  isSaving: boolean
  saveFailed: boolean
  filterType: KnowledgeDocType | null
  setFilterType: (value: KnowledgeDocType | null) => void
  create: (payload: {
    title: string
    doc_type: KnowledgeDocType
    content: string
  }) => Promise<boolean>
  remove: (documentId: string) => Promise<void>
  retry: () => void
}

export function useKnowledgeDocuments(): DocumentosState {
  const { token, selectedOrganizationId } = useAuth()
  const [result, setResult] = useState<{
    key: string
    documents: KnowledgeDocument[]
    total: number
    failed: boolean
  }>({ key: '', documents: [], total: 0, failed: false })
  const [filterType, setFilterType] = useState<KnowledgeDocType | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [isSaving, setIsSaving] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)

  const isAuthenticated = Boolean(token && selectedOrganizationId)
  const requestKey = JSON.stringify([token, selectedOrganizationId, filterType, reloadToken])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void getKnowledgeDocuments(token, selectedOrganizationId, {
      limit: PAGE_SIZE,
      offset: 0,
      ...(filterType ? { doc_type: filterType } : {}),
    })
      .then((page) => {
        if (!isActive) return
        setResult({ key: requestKey, documents: page.documents, total: page.total, failed: false })
      })
      .catch(() => {
        if (isActive) setResult({ key: requestKey, documents: [], total: 0, failed: true })
      })
    return () => {
      isActive = false
    }
  }, [filterType, requestKey, selectedOrganizationId, token])

  const create = useCallback(
    async (payload: {
      title: string
      doc_type: KnowledgeDocType
      content: string
    }): Promise<boolean> => {
      if (!token || !selectedOrganizationId || isSaving) return false
      setIsSaving(true)
      setSaveFailed(false)
      try {
        const creado = await createKnowledgeDocument(token, selectedOrganizationId, payload)
        // Se inserta en el listado **sin** volver a pedirlo. El documento acaba de guardarse y el
        // backend acaba de responder con el contenido completo, así que traerlo otra vez sería
        // una ida a la base para recibir lo que ya se tiene en la mano.
        setResult((current) => ({
          ...current,
          documents: [creado, ...current.documents],
          total: current.total + 1,
        }))
        return true
      } catch {
        setSaveFailed(true)
        return false
      } finally {
        setIsSaving(false)
      }
    },
    [isSaving, selectedOrganizationId, token],
  )

  const remove = useCallback(
    async (documentId: string): Promise<void> => {
      if (!token || !selectedOrganizationId) return
      const copia = result.documents
      const total = result.total
      setResult((current) => ({
        ...current,
        documents: current.documents.filter((d) => d.id !== documentId),
        total: Math.max(0, current.total - 1),
      }))
      try {
        await deleteKnowledgeDocument(token, selectedOrganizationId, documentId)
      } catch {
        setResult((current) => ({ ...current, documents: copia, total }))
      }
    },
    [result.documents, result.total, selectedOrganizationId, token],
  )

  return {
    documents: isCurrent ? result.documents : [],
    total: isCurrent ? result.total : 0,
    isLoading: isAuthenticated && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    isSaving,
    saveFailed,
    filterType,
    setFilterType,
    create,
    remove,
    retry: () => setReloadToken((current) => current + 1),
  }
}
