import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import authEn from './locales/en/auth.json'
import adminEn from './locales/en/admin.json'
import assetDiscoveryEn from './locales/en/assetDiscovery.json'
import billingEn from './locales/en/billing.json'
import chatEn from './locales/en/chat.json'
import supplyChainEn from './locales/en/supplyChain.json'
import commonEn from './locales/en/common.json'
import apiAccessEn from './locales/en/apiAccess.json'
import cveEn from './locales/en/cve.json'
import dashboardEn from './locales/en/dashboard.json'
import domainsEn from './locales/en/domains.json'
import enterpriseEn from './locales/en/enterprise.json'
import errorsEn from './locales/en/errors.json'
import integrationsEn from './locales/en/integrations.json'
import issuesEn from './locales/en/issues.json'
import llmEn from './locales/en/llm.json'
import knowledgeEn from './locales/en/knowledge.json'
import navigationEn from './locales/en/navigation.json'
import onboardingEn from './locales/en/onboarding.json'
import pentestsEn from './locales/en/pentests.json'
import prReviewsEn from './locales/en/prReviews.json'
import repositoriesEn from './locales/en/repositories.json'
import triageEn from './locales/en/triage.json'
import settingsEn from './locales/en/settings.json'
import supportEn from './locales/en/support.json'
import authEs from './locales/es/auth.json'
import adminEs from './locales/es/admin.json'
import assetDiscoveryEs from './locales/es/assetDiscovery.json'
import billingEs from './locales/es/billing.json'
import chatEs from './locales/es/chat.json'
import supplyChainEs from './locales/es/supplyChain.json'
import commonEs from './locales/es/common.json'
import apiAccessEs from './locales/es/apiAccess.json'
import cveEs from './locales/es/cve.json'
import dashboardEs from './locales/es/dashboard.json'
import domainsEs from './locales/es/domains.json'
import enterpriseEs from './locales/es/enterprise.json'
import errorsEs from './locales/es/errors.json'
import integrationsEs from './locales/es/integrations.json'
import issuesEs from './locales/es/issues.json'
import llmEs from './locales/es/llm.json'
import knowledgeEs from './locales/es/knowledge.json'
import navigationEs from './locales/es/navigation.json'
import onboardingEs from './locales/es/onboarding.json'
import pentestsEs from './locales/es/pentests.json'
import prReviewsEs from './locales/es/prReviews.json'
import repositoriesEs from './locales/es/repositories.json'
import triageEs from './locales/es/triage.json'
import settingsEs from './locales/es/settings.json'
import supportEs from './locales/es/support.json'

export type SupportedLanguage = 'es' | 'en'

const languageStorageKey = 'fenix_language'

function getInitialLanguage(): SupportedLanguage {
  if (typeof window === 'undefined') {
    return 'es'
  }

  const storedLanguage = window.localStorage.getItem(languageStorageKey)
  return storedLanguage === 'en' || storedLanguage === 'es' ? storedLanguage : 'es'
}

void i18n.use(initReactI18next).init({
  resources: {
    es: {
      chat: chatEs,
      supplyChain: supplyChainEs,
      common: commonEs,
      auth: authEs,
      navigation: navigationEs,
      errors: errorsEs,
      dashboard: dashboardEs,
      domains: domainsEs,
      assetDiscovery: assetDiscoveryEs,
      repositories: repositoriesEs,
      issues: issuesEs,
      integrations: integrationsEs,
      pentests: pentestsEs,
      apiAccess: apiAccessEs,
      cve: cveEs,
      prReviews: prReviewsEs,
      admin: adminEs,
      billing: billingEs,
      enterprise: enterpriseEs,
      triage: triageEs,
      llm: llmEs,
      knowledge: knowledgeEs,
      onboarding: onboardingEs,
      settings: settingsEs,
      support: supportEs,
    },
    en: {
      chat: chatEn,
      supplyChain: supplyChainEn,
      common: commonEn,
      auth: authEn,
      navigation: navigationEn,
      errors: errorsEn,
      dashboard: dashboardEn,
      domains: domainsEn,
      assetDiscovery: assetDiscoveryEn,
      repositories: repositoriesEn,
      issues: issuesEn,
      integrations: integrationsEn,
      pentests: pentestsEn,
      apiAccess: apiAccessEn,
      cve: cveEn,
      prReviews: prReviewsEn,
      admin: adminEn,
      billing: billingEn,
      enterprise: enterpriseEn,
      triage: triageEn,
      llm: llmEn,
      knowledge: knowledgeEn,
      onboarding: onboardingEn,
      settings: settingsEn,
      support: supportEn,
    },
  },
  lng: getInitialLanguage(),
  fallbackLng: 'es',
  ns: [
    'common',
    'auth',
    'navigation',
    'errors',
    'dashboard',
    'domains',
    'assetDiscovery',
    'repositories',
    'issues',
    'integrations',
    'pentests',
    'apiAccess',
    'cve',
    'prReviews',
    'admin',
    'billing',
    'enterprise',
    'triage',
    'llm',
    'knowledge',
    'onboarding',
    'settings',
    'support',
  ],
  defaultNS: 'common',
  interpolation: {
    escapeValue: false,
  },
})

i18n.on('languageChanged', (language: string) => {
  if (typeof window === 'undefined' || (language !== 'es' && language !== 'en')) {
    return
  }
  window.localStorage.setItem(languageStorageKey, language)
  // El atributo lang debe seguir al idioma activo o los lectores de pantalla
  // anuncian el texto con la fonética equivocada.
  document.documentElement.lang = language
})

if (typeof document !== 'undefined') {
  document.documentElement.lang = i18n.resolvedLanguage ?? i18n.language
}

export default i18n
