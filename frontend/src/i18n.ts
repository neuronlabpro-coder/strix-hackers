import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import authEn from './locales/en/auth.json'
import commonEn from './locales/en/common.json'
import errorsEn from './locales/en/errors.json'
import navigationEn from './locales/en/navigation.json'
import authEs from './locales/es/auth.json'
import commonEs from './locales/es/common.json'
import errorsEs from './locales/es/errors.json'
import navigationEs from './locales/es/navigation.json'

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
      common: commonEs,
      auth: authEs,
      navigation: navigationEs,
      errors: errorsEs,
    },
    en: {
      common: commonEn,
      auth: authEn,
      navigation: navigationEn,
      errors: errorsEn,
    },
  },
  lng: getInitialLanguage(),
  fallbackLng: 'es',
  ns: ['common', 'auth', 'navigation', 'errors'],
  defaultNS: 'common',
  interpolation: {
    escapeValue: false,
  },
})

i18n.on('languageChanged', (language: string) => {
  if (typeof window !== 'undefined' && (language === 'es' || language === 'en')) {
    window.localStorage.setItem(languageStorageKey, language)
  }
})

export default i18n
