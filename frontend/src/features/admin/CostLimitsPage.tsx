import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import {
  createCostLimitPolicy,
  deleteCostLimitPolicy,
  getAdminOrganizations,
  getCostLimitPolicies,
  previewCostLimits,
  updateCostLimitPolicy,
} from '../../lib/api'
import type {
  AdminOrganization,
  CostLimitOperation,
  CostLimitPolicy,
  CostLimitPreview,
  CostLimitScope,
} from '../../types/api'
import { useAuth } from '../auth/useAuth'

const OPERATIONS: readonly CostLimitOperation[] = ['PENTEST_QUICK', 'PENTEST_DEEP', 'PR_REVIEW', 'CHAT']
const SCOPES: readonly CostLimitScope[] = ['ORGANIZACION', 'OPERACION', 'PLAN', 'GLOBAL']

export function CostLimitsPage() {
  const { t } = useTranslation('admin')
  const { token, selectedOrganizationId } = useAuth()
  const [organizations, setOrganizations] = useState<AdminOrganization[]>([])
  const [policies, setPolicies] = useState<CostLimitPolicy[]>([])
  const [scope, setScope] = useState<CostLimitScope>('ORGANIZACION')
  const [targetId, setTargetId] = useState('')
  const [operation, setOperation] = useState<CostLimitOperation>('PENTEST_QUICK')
  const [planTier, setPlanTier] = useState<'FREE' | 'PRO' | 'ENTERPRISE'>('PRO')
  const [budget, setBudget] = useState('')
  const [turns, setTurns] = useState('')
  const [validFrom, setValidFrom] = useState('')
  const [validUntil, setValidUntil] = useState('')
  const [preview, setPreview] = useState<CostLimitPreview | null>(null)
  const [error, setError] = useState(false)
  const [saving, setSaving] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!token || !selectedOrganizationId) return
    try {
      const [orgs, rules] = await Promise.all([
        getAdminOrganizations(token, selectedOrganizationId, 100),
        getCostLimitPolicies(token, selectedOrganizationId),
      ])
      setOrganizations(orgs.items)
      setPolicies(rules)
      setTargetId((current) => current || orgs.items[0]?.id || '')
      setError(false)
    } catch {
      setError(true)
    }
  }, [token, selectedOrganizationId])

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let active = true
    void Promise.all([
      getAdminOrganizations(token, selectedOrganizationId, 100),
      getCostLimitPolicies(token, selectedOrganizationId),
    ]).then(([orgs, rules]) => {
      if (!active) return
      setOrganizations(orgs.items)
      setPolicies(rules)
      setTargetId((current) => current || orgs.items[0]?.id || '')
      setError(false)
    }).catch(() => { if (active) setError(true) })
    return () => { active = false }
  }, [token, selectedOrganizationId])

  useEffect(() => {
    if (!token || !selectedOrganizationId || !targetId) return
    void previewCostLimits(token, selectedOrganizationId, targetId, operation)
      .then(setPreview)
      .catch(() => setPreview(null))
  }, [token, selectedOrganizationId, targetId, operation, policies])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || !selectedOrganizationId || (!budget && !turns)) return
    setSaving(true)
    try {
      const payload = {
        scope,
        organization_id: scope === 'ORGANIZACION' ? targetId : null,
        operation: scope === 'OPERACION' ? operation : null,
        plan_tier: scope === 'PLAN' ? planTier : null,
        max_budget_usd: budget || null,
        max_turns: turns ? Number(turns) : null,
        valid_from: editingId ? null : validFrom ? new Date(validFrom).toISOString() : null,
        valid_until: validUntil ? new Date(validUntil).toISOString() : null,
      }
      if (editingId) {
        await updateCostLimitPolicy(token, selectedOrganizationId, editingId, payload)
      } else {
        await createCostLimitPolicy(token, selectedOrganizationId, payload)
      }
      setEditingId(null)
      setBudget('')
      setTurns('')
      setValidFrom('')
      setValidUntil('')
      await load()
    } catch {
      setError(true)
    } finally {
      setSaving(false)
    }
  }

  function edit(rule: CostLimitPolicy) {
    setEditingId(rule.id)
    setScope(rule.scope)
    if (rule.organization_id) setTargetId(rule.organization_id)
    if (rule.operation) setOperation(rule.operation)
    if (rule.plan_tier) setPlanTier(rule.plan_tier)
    setBudget(rule.max_budget_usd ?? '')
    setTurns(rule.max_turns?.toString() ?? '')
    setValidFrom('')
    setValidUntil(rule.valid_until ? rule.valid_until.slice(0, 16) : '')
  }

  async function remove(policyId: string) {
    if (!token || !selectedOrganizationId) return
    setSaving(true)
    try {
      await deleteCostLimitPolicy(token, selectedOrganizationId, policyId)
      if (editingId === policyId) setEditingId(null)
      await load()
    } catch {
      setError(true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="page-section" aria-labelledby="cost-limits-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('costLimits.eyebrow')}</p>
          <h1 id="cost-limits-title">{t('costLimits.title')}</h1>
          <p className="page-description">{t('costLimits.description')}</p>
        </div>
      </div>
      {error && <p role="alert">{t('costLimits.error')}</p>}
      <form className="empty-card" onSubmit={(event) => { void save(event) }}>
        <div className="filter-bar">
          <label className="filter-field">
            <span>{t('costLimits.scope')}</span>
            <select value={scope} onChange={(event) => setScope(event.target.value as CostLimitScope)}>
              {SCOPES.map((value) => <option key={value} value={value}>{t(`costLimits.scopes.${value}`)}</option>)}
            </select>
          </label>
          {(scope === 'ORGANIZACION' || scope === 'GLOBAL') && (
            <label className="filter-field">
              <span>{t('costLimits.organization')}</span>
              <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
                {organizations.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
              </select>
            </label>
          )}
          <label className="filter-field">
            <span>{t('costLimits.operation')}</span>
            <select value={operation} onChange={(event) => setOperation(event.target.value as CostLimitOperation)}>
              {OPERATIONS.map((value) => <option key={value} value={value}>{t(`costLimits.operations.${value}`)}</option>)}
            </select>
          </label>
          {scope === 'PLAN' && (
            <label className="filter-field">
              <span>{t('costLimits.plan')}</span>
              <select value={planTier} onChange={(event) => setPlanTier(event.target.value as typeof planTier)}>
                {(['FREE', 'PRO', 'ENTERPRISE'] as const).map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
          )}
        </div>
        <div className="filter-bar">
          <label className="filter-field">
            <span>{t('costLimits.budget')}</span>
            <input type="number" min="0.00000001" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} />
          </label>
          <label className="filter-field">
            <span>{t('costLimits.turns')}</span>
            <input type="number" min="1" step="1" value={turns} onChange={(event) => setTurns(event.target.value)} />
          </label>
          <label className="filter-field">
            <span>{t('costLimits.from')}</span>
            <input type="datetime-local" value={validFrom} onChange={(event) => setValidFrom(event.target.value)} />
          </label>
          <label className="filter-field">
            <span>{t('costLimits.until')}</span>
            <input type="datetime-local" value={validUntil} onChange={(event) => setValidUntil(event.target.value)} />
          </label>
        </div>
        <button className="primary-button" type="submit" disabled={saving || (!budget && !turns)}>
          {saving ? t('costLimits.saving') : editingId ? t('costLimits.update') : t('costLimits.save')}
        </button>
        {editingId && <button type="button" onClick={() => setEditingId(null)}>{t('costLimits.cancel')}</button>}
      </form>
      {preview && (
        <div className="empty-card" aria-live="polite">
          <h2>{t('costLimits.preview')}</h2>
          <p>{t('costLimits.budget')}: {preview.max_budget_usd} USD ({t(`costLimits.scopes.${preview.presupuesto_origen.nivel}`)})</p>
          <p>{t('costLimits.turns')}: {preview.max_turns} ({t(`costLimits.scopes.${preview.turnos_origen.nivel}`)})</p>
        </div>
      )}
      <div className="empty-card">
        <h2>{t('costLimits.history')}</h2>
        <ul>
          {policies.map((rule) => (
            <li key={rule.id}>
              {t(`costLimits.scopes.${rule.scope}`)} · {rule.max_budget_usd ?? '—'} USD · {rule.max_turns ?? '—'} {t('costLimits.turns')}
              {' · '}{new Date(rule.valid_from).toLocaleDateString()}
              {new Date(rule.valid_from) <= new Date() && (!rule.valid_until || new Date(rule.valid_until) > new Date()) && (
                <>
                  {' '}<button type="button" onClick={() => edit(rule)} disabled={saving}>{t('costLimits.edit')}</button>
                  {' '}<button type="button" onClick={() => { void remove(rule.id) }} disabled={saving}>{t('costLimits.delete')}</button>
                </>
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
