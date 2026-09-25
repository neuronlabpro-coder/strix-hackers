import { createContext, useContext } from 'react'

export type ToastTone = 'success' | 'error' | 'info'

export interface Toast {
  id: string
  tone: ToastTone
  message: string
}

export interface ToastContextValue {
  toasts: Toast[]
  notify: (tone: ToastTone, message: string) => void
  dismiss: (id: string) => void
}

/**
 * Notificaciones transitorias. Existen porque el triaje Kanban es una
 * mutacion optimista: si la API rechaza el cambio, el usuario necesita saberlo
 * sin que la tarjeta regrese en silencio.
 */
export const ToastContext = createContext<ToastContextValue | undefined>(undefined)

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext)
  if (context === undefined) {
    throw new Error('useToast debe usarse dentro de ToastProvider')
  }
  return context
}
