import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export type EnterpriseFeature = 'networks' | 'containers' | 'supplyChain'

export interface EnterpriseGateModalProps {
  feature: EnterpriseFeature | null
  onClose: () => void
}

const SALES_CONTACT = 'mailto:sales@mindguard.tech?subject=Fenix%20Enterprise'

/**
 * Modal de alta impacto para las secciones Enterprise. No navega: explica la
 * capacidad y ofrece el contacto comercial, porque la funcionalidad todavía no
 * existe en el producto y fingir un destino seria mentir.
 */
export function EnterpriseGateModal({ feature, onClose }: EnterpriseGateModalProps) {
  const { t } = useTranslation('enterprise')
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!feature) {
      return
    }
    const previouslyFocused = document.activeElement as HTMLElement | null
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    )
    focusable?.[0]?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab' || !focusable || focusable.length === 0) {
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      previouslyFocused?.focus()
    }
  }, [feature, onClose])

  if (!feature) {
    return null
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="enterprise-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow">{t('modal.eyebrow')}</p>
            <h2 id="enterprise-title">{t(`features.${feature}.title`)}</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label={t('modal.close')}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <section className="modal-section">
          <p className="page-description">{t(`features.${feature}.description`)}</p>
          <p className="code-block" style={{ background: 'transparent', padding: 0 }}>
            {t(`features.${feature}.capabilities`)}
          </p>
          <p className="chart-empty">{t('modal.note')}</p>
        </section>

        <footer className="modal-section">
          <div className="page-actions">
            <a className="primary-button" href={SALES_CONTACT}>
              <span>{t('modal.contactSales')}</span>
            </a>
            <a className="secondary-button" href={SALES_CONTACT}>
              <span>{t('modal.requestDemo')}</span>
            </a>
          </div>
        </footer>
      </div>
    </div>
  )
}
