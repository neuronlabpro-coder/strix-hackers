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
 * Porque el borrado de un documento es una acción inmediata en la interfaz y el usuario espera
 * que la tarjeta desaparezca. Si la tarjeta se queda hasta que responde el servidor, con una red
 * lenta parece que el botón no funciona y lo pulsa otra vez, y la segunda pulsación ya no tiene
 * nada que borrar. Si la petición falla, la tarjeta vuelve.
 *
 * ## Por qué los filtros viven en la clave y no en el `fetcher`
 *
 * Porque la clave es lo que hace que `useEffect` vuelva a pedir. Si los filtros estuvieran solo
 * en el estado, la clave no cambiaría al escribirlos y la lista no se recargaría. Es el mismo
 * motivo por el que la clave incluye el `offset`: cambiar de página también es cambiar la
 * petición.
 */

const PAGE_SIZE = 50

/**
 * Filtros de la vista de documentos, en el estado en que los pinta la pantalla.
 *
 * ## Por qué las fechas son cadenas `AAAA-MM-DD` y no `Date`
 *
 * Porque es lo que da el `<input type="date">` y lo que viaja en la query. Convertirlas a `Date`
 * en el cliente obligaría a decidir una zona horaria para un filtro por día natural, y esa
 * decisión no está en ningún sitio del proyecto. El backend corta el rango en UTC y ya se ha
 * explicado por qué allí.
 *
 * ## Por qué el tipo va **aparte** del texto y de las fechas
 *
 * Porque el tipo no es un texto: es un valor de un enumerado de la base, y mandarlo como
 * cadena dejaría que el servidor recibiera un tipo que no existe. Además el selector de tipo
 * ya existe como grupo de chips y no se duplica como desplegable.
 */
export interface DocumentosQuery {
  type: KnowledgeDocType | null
  search: string
  createdFrom: string
  createdTo: string
}

export const EMPTY_DOCUMENTOS_QUERY: DocumentosQuery = {
  type: null,
  search: '',
  createdFrom: '',
  createdTo: '',
}

/**
 * Si hay algún filtro puesto, y por tanto si el botón de limpiar tiene algo que borrar.
 *
 * ## Por qué el texto va **recortado**
 *
 * Porque el servidor hace `.strip()` antes de aplicar el término, así que un campo con tres
 * espacios filtra exactamente lo mismo que uno vacío. Sin el `trim` aquí, el botón aparecería
 * sobre una lista que no está filtrada y al pulsarlo no cambiaría nada: el peor resultado posible
 * para un botón, porque enseña al operador que ese control a veces no hace nada.
 *
 * Se exporta porque la vista la pinta y la regla merece una prueba propia: es aritmética pura
 * sobre cuatro campos y comprobarla entera sin montar React es exactamente lo que este proyecto
 * puede hacer.
 */
export function hayFiltrosPuestos(query: DocumentosQuery): boolean {
  return Boolean(
    query.type !== null ||
      query.search.trim() !== '' ||
      query.createdFrom !== '' ||
      query.createdTo !== '',
  )
}

export interface DocumentosState {
  documents: KnowledgeDocument[]
  total: number
  limit: number
  offset: number
  isLoading: boolean
  loadFailed: boolean
  isSaving: boolean
  saveFailed: boolean
  query: DocumentosQuery
  setQuery: (query: DocumentosQuery) => void
  setOffset: (offset: number) => void
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
    limit: number
    offset: number
    failed: boolean
  }>({ key: '', documents: [], total: 0, limit: PAGE_SIZE, offset: 0, failed: false })
  const [query, setQueryState] = useState<DocumentosQuery>(EMPTY_DOCUMENTOS_QUERY)
  const [offset, setOffsetState] = useState(0)
  const [reloadToken, setReloadToken] = useState(0)
  const [isSaving, setIsSaving] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)

  const isAuthenticated = Boolean(token && selectedOrganizationId)
  const requestKey = JSON.stringify([
    token,
    selectedOrganizationId,
    query,
    offset,
    reloadToken,
  ])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let isActive = true
    void getKnowledgeDocuments(token, selectedOrganizationId, {
      limit: PAGE_SIZE,
      offset,
      ...(query.type ? { doc_type: query.type } : {}),
      ...(query.search.trim() ? { query: query.search.trim() } : {}),
      ...(query.createdFrom ? { createdFrom: query.createdFrom } : {}),
      ...(query.createdTo ? { createdTo: query.createdTo } : {}),
    })
      .then((page) => {
        if (!isActive) return
        setResult({
          key: requestKey,
          documents: page.documents,
          total: page.total,
          limit: page.limit,
          offset: page.offset,
          failed: false,
        })
      })
      .catch(() => {
        // Se conservan los documentos anteriores: un corte de red no es «esta lista no tiene
        // nada», y vaciarla haría que el usuario borrara su lectura por un problema de conexión.
        if (isActive) {
          setResult((current) => ({ ...current, key: requestKey, failed: true }))
        }
      })
    return () => {
      isActive = false
    }
  }, [offset, query, reloadToken, requestKey, selectedOrganizationId, token])

  /**
   * Si el documento recién creado entra en lo que se está viendo.
   *
   * ## Por qué no siempre se inserta en el listado
   *
   * Porque antes de los filtros no había forma de que un documento recién guardado **no**
   * apareciera: la lista era entera. Con filtros y paginación sí la hay, y en los tres casos en
   * que no aparece hay que recargar en vez de insertar:
   *
   * 1. No estamos en la primera página: insertarlo en la posición 0 lo colocaría en medio de la
   *    página que el usuario está leyendo, y la suma de `total` sería correcta pero las filas
   *    no.
   * 2. Hay un filtro de tipo y el documento es de otro tipo: aparecería una tarjeta que el
   *    filtro acaba de decir que no existe.
   * 3. Hay un texto o un rango de fechas: el documento puede no casar con el criterio, y la
   *    lista respondería con un documento que su propio filtro excluye.
   *
   * El texto se comprueba aquí porque es una condición que el cliente puede resolver sin
   * inventarse nada: si el título no contiene el término, el documento tampoco puede estar en el
   * resultado. El rango de fechas **no** se comprueba, y esa es la decisión: saber si un
   * `created_at` cae dentro de un rango en UTC es el mismo cálculo que hace el servidor, y
   * duplicarlo en el cliente sería tener dos verdades para la misma pregunta. Con un rango
   * activo se recarga siempre.
   *
   * En los tres casos se vuelve a la primera página y se recarga, que es lo que quiera el
   * filtro. En el resto se conserva la inserción optimista de siempre, porque el documento
   * acaba de guardarse y el backend acaba de devolverlo con el contenido completo: traerlo otra
   * vez sería una ida a la base para recibir lo que ya se tiene en la mano.
   */
  const cabeEnLoQueSeVe = useCallback(
    (creado: KnowledgeDocument): boolean => {
      if (offset !== 0) return false
      if (query.type !== null && creado.doc_type !== query.type) return false
      if (query.createdFrom !== '' || query.createdTo !== '') return false
      const termino = query.search.trim().toLowerCase()
      return termino === '' || creado.title.toLowerCase().includes(termino)
    },
    [offset, query],
  )

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
        if (cabeEnLoQueSeVe(creado)) {
          setResult((current) => ({
            ...current,
            documents: [creado, ...current.documents],
            total: current.total + 1,
          }))
        } else {
          setOffsetState(0)
          setReloadToken((current) => current + 1)
        }
        return true
      } catch {
        setSaveFailed(true)
        return false
      } finally {
        setIsSaving(false)
      }
    },
    [cabeEnLoQueSeVe, isSaving, selectedOrganizationId, token],
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
    limit: isCurrent ? result.limit : PAGE_SIZE,
    offset: isCurrent ? result.offset : 0,
    isLoading: isAuthenticated && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    isSaving,
    saveFailed,
    query,
    // ## Por qué cambiar un filtro vuelve a la primera página
    //
    // Porque la página 4 del filtro anterior no significa nada en el nuevo. Sin este
    // `setOffsetState(0)`, escribir tres letras en el buscador deja la cuadrícula vacía —el
    // `offset` 150 está más allá del total del resultado nuevo— y el usuario ve «ningún
    // resultado» con un filtro que sí tiene resultados.
    setQuery: (next: DocumentosQuery) => {
      setQueryState(next)
      setOffsetState(0)
    },
    setOffset: (next: number) => setOffsetState(Math.max(0, next)),
    create,
    remove,
    retry: () => setReloadToken((current) => current + 1),
  }
}
