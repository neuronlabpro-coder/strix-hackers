import { useEffect, useState } from 'react'

import { getKnowledgeEntries } from '../../lib/api'
import type { KnowledgeCategory, KnowledgePage, KnowledgeSeverity } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const PAGE_SIZE = 24

const CATEGORIES: KnowledgeCategory[] = [
  'INJECTION',
  'XSS',
  'AUTH',
  'CRYPTOGRAPHY',
  'SECRET_EXPOSURE',
  'DESERIALIZATION',
  'SSRF',
  'PATH_TRAVERSAL',
  'LOGIC',
  'DEPENDENCY',
]

const SEVERITIES: KnowledgeSeverity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

export interface KnowledgeQuery {
  category: KnowledgeCategory | null
  severity: KnowledgeSeverity | null
  search: string
}

export interface KnowledgeState {
  page: KnowledgePage | null
  isLoading: boolean
  loadFailed: boolean
  query: KnowledgeQuery
  setQuery: (query: KnowledgeQuery) => void
  refresh: () => void
}

const EMPTY_QUERY: KnowledgeQuery = { category: null, severity: null, search: '' }

export function useKnowledge(): KnowledgeState {
  const { token, selectedOrganizationId } = useAuth()
  const [query, setQueryState] = useState<KnowledgeQuery>(EMPTY_QUERY)
  const [reloadToken, setReloadToken] = useState(0)
  const [result, setResult] = useState<{ key: string; page: KnowledgePage; failed: boolean }>({
    key: '',
    page: { items: [], total: 0, limit: PAGE_SIZE, offset: 0 },
    failed: false,
  })

  const requestKey = JSON.stringify([token, selectedOrganizationId, query, reloadToken])
  const isCurrent = result.key === requestKey

  useEffect(() => {
    if (!token || !selectedOrganizationId) {
      return
    }
    let isActive = true
    void getKnowledgeEntries(token, selectedOrganizationId, {
      limit: PAGE_SIZE,
      offset: 0,
      ...(query.category ? { category: query.category } : {}),
      ...(query.severity ? { severity: query.severity } : {}),
      ...(query.search ? { search: query.search } : {}),
    })
      .then((page) => {
        if (isActive) {
          setResult({ key: requestKey, page, failed: false })
        }
      })
      .catch(() => {
        if (isActive) {
          setResult((current) => ({ ...current, key: requestKey, failed: true }))
        }
      })
    return () => {
      isActive = false
    }
  }, [query, reloadToken, requestKey, selectedOrganizationId, token])

  return {
    page: isCurrent ? result.page : null,
    isLoading: Boolean(token && selectedOrganizationId) && !isCurrent && !result.failed,
    loadFailed: isCurrent && result.failed,
    query,
    setQuery: setQueryState,
    refresh: () => setReloadToken((current) => current + 1),
  }
}

export { CATEGORIES, SEVERITIES, PAGE_SIZE }
