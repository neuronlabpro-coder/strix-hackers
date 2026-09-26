import { CircleDot } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface PlaceholderPageProps {
  titleKey: string
  /**
   * Razón concreta por la que la vista aún no existe, en el namespace `common`.
   *
   * Se muestra siempre. Una pantalla placeholder que solo dice «disponible en una fase
   * posterior» es indistinguible de un fallo, y quien la ve no puede decidir si
   * esperar, reportar un bug o buscar la alternativa. Nombrar el bloqueo real
   * —«requiere el modelo de datos de dominios»— convierte una pantalla muerta en
   * información accionable.
   */
  reasonKey?: string
}

export function PlaceholderPage({ titleKey, reasonKey }: PlaceholderPageProps) {
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
        <p>{reasonKey ? t(`common:${reasonKey}`) : t('navigation:comingSoon')}</p>
      </section>
    </section>
  )
}
