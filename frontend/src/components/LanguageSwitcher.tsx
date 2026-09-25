import { Languages } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import i18n from '../i18n'

export function LanguageSwitcher() {
  const { t } = useTranslation('common')
  const currentLanguage = i18n.resolvedLanguage ?? i18n.language
  const nextLanguage = currentLanguage === 'es' ? 'en' : 'es'
  const visibleText = nextLanguage.toUpperCase()
  const label = nextLanguage === 'en' ? t('switchToEnglish') : t('switchToSpanish')

  return (
    <button
      className="language-switcher"
      type="button"
      onClick={() => void i18n.changeLanguage(nextLanguage)}
      // El nombre accesible incluye el texto visible para cumplir WCAG 2.5.3.
      aria-label={`${visibleText} · ${label}`}
      title={label}
    >
      <Languages size={16} aria-hidden="true" />
      <span>{visibleText}</span>
    </button>
  )
}
