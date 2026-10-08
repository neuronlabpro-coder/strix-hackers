import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import {
  createEnterpriseAgreement,
  getAdminOrganizations,
  getEnterpriseAgreements,
} from '../../lib/api'
import type { AdminOrganization, EnterpriseAgreement } from '../../types/api'
import { useAuth } from '../auth/useAuth'

const FEATURES = ['supply_chain', 'container_scanning', 'internal_network_scanning'] as const
type FeatureChoice = 'inherit' | 'enabled' | 'disabled'

export function EnterpriseAgreementsPage() {
  const { t } = useTranslation('admin')
  const { token, selectedOrganizationId } = useAuth()
  const [organizations, setOrganizations] = useState<AdminOrganization[]>([])
  const [targetId, setTargetId] = useState('')
  const [agreements, setAgreements] = useState<EnterpriseAgreement[]>([])
  const [price, setPrice] = useState('')
  const [seats, setSeats] = useState('')
  const [credits, setCredits] = useState('0')
  const [discount, setDiscount] = useState('0')
  const [budget, setBudget] = useState('')
  const [turns, setTurns] = useState('')
  const [from, setFrom] = useState('')
  const [until, setUntil] = useState('')
  const [operations, setOperations] = useState('')
  const [limits, setLimits] = useState('{}')
  const [features, setFeatures] = useState<Record<string, FeatureChoice>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(false)

  useEffect(() => {
    if (!token || !selectedOrganizationId) return
    let active = true
    void getAdminOrganizations(token, selectedOrganizationId, 100)
      .then((page) => {
        if (!active) return
        setOrganizations(page.items)
        setTargetId((current) => current || page.items[0]?.id || '')
      })
      .catch(() => { if (active) setError(true) })
    return () => { active = false }
  }, [token, selectedOrganizationId])

  useEffect(() => {
    if (!token || !selectedOrganizationId || !targetId) return
    let active = true
    void getEnterpriseAgreements(token, selectedOrganizationId, targetId)
      .then((rows) => {
        if (!active) return
        setAgreements(rows)
        const current = rows.find((row) =>
          new Date(row.valid_from).getTime() <= Date.now()
          && (!row.valid_until || new Date(row.valid_until).getTime() > Date.now()),
        )
        setPrice(current?.price_monthly_usd ?? '')
        setSeats(current?.seats?.toString() ?? '')
        setCredits(current?.included_credits ?? '0')
        setDiscount(current?.discount_pct ?? '0')
        setBudget(current?.max_budget_usd ?? '')
        setTurns(current?.max_turns?.toString() ?? '')
        setOperations(current?.special_operations.join(', ') ?? '')
        setLimits(JSON.stringify(current?.limits ?? {}))
        setFeatures(Object.fromEntries(FEATURES.map((key) => [
          key,
          current && key in current.features
            ? (current.features[key] ? 'enabled' : 'disabled')
            : 'inherit',
        ])))
        setError(false)
      })
      .catch(() => { if (active) setError(true) })
    return () => { active = false }
  }, [token, selectedOrganizationId, targetId])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!token || !selectedOrganizationId || !targetId) return
    setSaving(true)
    try {
      const parsedLimits: unknown = JSON.parse(limits)
      if (typeof parsedLimits !== 'object' || parsedLimits === null || Array.isArray(parsedLimits)) {
        throw new Error('limits')
      }
      const overrides = Object.fromEntries(
        FEATURES.filter((key) => features[key] !== 'inherit')
          .map((key) => [key, features[key] === 'enabled']),
      )
      const saved = await createEnterpriseAgreement(token, selectedOrganizationId, targetId, {
        price_monthly_usd: price || null,
        seats: seats ? Number(seats) : null,
        included_credits: credits,
        discount_pct: discount,
        max_budget_usd: budget || null,
        max_turns: turns ? Number(turns) : null,
        features: overrides,
        limits: parsedLimits as Record<string, number>,
        special_operations: operations.split(',').map((value) => value.trim()).filter(Boolean),
        valid_from: from ? new Date(from).toISOString() : null,
        valid_until: until ? new Date(until).toISOString() : null,
      })
      setAgreements((current) => [saved, ...current])
      setError(false)
    } catch {
      setError(true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="page-section" aria-labelledby="enterprise-agreements-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('agreements.eyebrow')}</p>
          <h1 id="enterprise-agreements-title">{t('agreements.title')}</h1>
          <p className="page-description">{t('agreements.description')}</p>
        </div>
      </div>
      {error && <p role="alert">{t('agreements.error')}</p>}
      <form className="empty-card" onSubmit={(event) => { void save(event) }}>
        <div className="filter-bar">
          <label className="filter-field">
            <span>{t('agreements.organization')}</span>
            <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
              {organizations.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
            </select>
          </label>
          {([
            ['price', price, setPrice], ['seats', seats, setSeats], ['credits', credits, setCredits],
            ['discount', discount, setDiscount], ['budget', budget, setBudget], ['turns', turns, setTurns],
          ] as const).map(([key, value, setter]) => (
            <label className="filter-field" key={key}>
              <span>{t(`agreements.fields.${key}`)}</span>
              <input type="number" min="0" step={key === 'seats' || key === 'turns' ? '1' : '0.01'} value={value}
                onChange={(event) => setter(event.target.value)} />
            </label>
          ))}
        </div>
        <div className="filter-bar">
          {FEATURES.map((key) => (
            <label className="filter-field" key={key}>
              <span>{t(`agreements.features.${key}`)}</span>
              <select value={features[key] ?? 'inherit'} onChange={(event) => setFeatures((current) => ({
                ...current, [key]: event.target.value as FeatureChoice,
              }))}>
                <option value="inherit">{t('agreements.inherit')}</option>
                <option value="enabled">{t('agreements.enabled')}</option>
                <option value="disabled">{t('agreements.disabled')}</option>
              </select>
            </label>
          ))}
        </div>
        <div className="filter-bar">
          <label className="filter-field"><span>{t('agreements.fields.operations')}</span>
            <input value={operations} onChange={(event) => setOperations(event.target.value)} /></label>
          <label className="filter-field"><span>{t('agreements.fields.limits')}</span>
            <input className="mono" value={limits} onChange={(event) => setLimits(event.target.value)} /></label>
          <label className="filter-field"><span>{t('agreements.fields.from')}</span>
            <input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} /></label>
          <label className="filter-field"><span>{t('agreements.fields.until')}</span>
            <input type="datetime-local" value={until} onChange={(event) => setUntil(event.target.value)} /></label>
        </div>
        <button className="primary-button" type="submit" disabled={saving || !targetId}>
          {saving ? t('agreements.saving') : t('agreements.save')}
        </button>
      </form>
      <div className="empty-card">
        <h2>{t('agreements.history')}</h2>
        <ul>{agreements.map((agreement) => (
          <li key={agreement.id}>
            {new Date(agreement.valid_from).toLocaleDateString()} · {agreement.price_monthly_usd === null ? t('agreements.custom') : `${agreement.price_monthly_usd} USD`} · {agreement.seats ?? '—'} {t('agreements.fields.seats')}
          </li>
        ))}</ul>
      </div>
    </section>
  )
}
