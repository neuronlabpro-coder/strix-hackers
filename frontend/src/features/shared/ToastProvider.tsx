import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { CheckCircle2, Info, X, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { ToastContext, type Toast, type ToastTone } from './toast-context'

const TOAST_DURATION_MS = 4500

const TONE_ICON: Record<ToastTone, typeof Info> = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation('common')
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const notify = useCallback((tone: ToastTone, message: string) => {
    const id = `${tone}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    setToasts((current) => [...current, { id, tone, message }])
  }, [])

  useEffect(() => {
    if (toasts.length === 0) {
      return
    }
    const timer = window.setTimeout(() => {
      setToasts((current) => current.slice(1))
    }, TOAST_DURATION_MS)
    return () => window.clearTimeout(timer)
  }, [toasts])

  const value = useMemo(() => ({ toasts, notify, dismiss }), [dismiss, notify, toasts])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="region" aria-label={t('notifications')}>
        {toasts.map((toast) => {
          const Icon = TONE_ICON[toast.tone]
          return (
            <div
              key={toast.id}
              className={`toast toast-${toast.tone}`}
              role={toast.tone === 'error' ? 'alert' : 'status'}
            >
              <Icon size={16} aria-hidden="true" />
              <span className="toast-message">{toast.message}</span>
              <button
                className="toast-close"
                type="button"
                onClick={() => dismiss(toast.id)}
                aria-label={t('dismissNotification')}
              >
                <X size={14} aria-hidden="true" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}
