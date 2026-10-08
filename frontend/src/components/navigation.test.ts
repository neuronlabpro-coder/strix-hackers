import { describe, expect, it } from 'vitest'

import { isNavigationItemLocked, primaryNavigation } from './navigation'

const supplyChain = primaryNavigation.find((item) => item.enterpriseFeature === 'supplyChain')

describe('gating de navegación por organización', () => {
  it('espera el entitlement del tenant y respeta el override explícito', () => {
    expect(supplyChain).toBeDefined()
    if (!supplyChain) throw new Error('Falta la ruta Supply Chain')

    expect(isNavigationItemLocked(supplyChain, false, null)).toBe(true)
    expect(isNavigationItemLocked(supplyChain, false, { supply_chain: false })).toBe(true)
    expect(isNavigationItemLocked(supplyChain, false, { supply_chain: true })).toBe(false)
    expect(isNavigationItemLocked(supplyChain, true, null)).toBe(false)
  })
})
