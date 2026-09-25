import { CircleDot } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface PlaceholderPageProps {
  titleKey: string
}

export function PlaceholderPage({ titleKey }: PlaceholderPageProps) {
  const { t } = useTranslation(['common', 'navigation'])

  return (
    <section className="page-section" aria-labelledby="page-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('common:appName')}</p>
          <h1 id="page-title">{t(titleKey)}</h1>
          <p className="page-description">{t('navigation:comingSoon')}</p>
        </div>
      </div>
      <section className="content-card empty-card">
        <span className="empty-card-mark" aria-hidden="true">
          <CircleDot size={18} />
        </span>
        <p>{t('navigation:comingSoon')}</p>
      </section>
    </section>
  )
}
