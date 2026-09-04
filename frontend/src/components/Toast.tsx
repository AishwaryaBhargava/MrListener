import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

/**
 * A tiny toast stack for save/error feedback.
 *
 * Deliberately unstyled beyond the two tones the app needs: this is for
 * confirming a write the user just triggered ("Settings saved") or telling
 * them it failed. Anything that needs a decision belongs in the page, not
 * here.
 */

export type ToastTone = 'ok' | 'error'

interface Toast {
  id: number
  tone: ToastTone
  message: string
}

interface ToastApi {
  show: (message: string, tone?: ToastTone) => void
  error: (message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

const LIFETIME_MS = 3600

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const show = useCallback(
    (message: string, tone: ToastTone = 'ok') => {
      const id = nextId.current++
      setToasts((current) => [...current.slice(-2), { id, tone, message }])
      window.setTimeout(() => dismiss(id), LIFETIME_MS)
    },
    [dismiss],
  )

  const api = useMemo<ToastApi>(
    () => ({ show, error: (message: string) => show(message, 'error') }),
    [show],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <button
            key={toast.id}
            type="button"
            className={toast.tone === 'error' ? 'toast error' : 'toast'}
            onClick={() => dismiss(toast.id)}
          >
            {toast.message}
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

/** Never throws: a component outside the provider simply gets a no-op. */
export function useToast(): ToastApi {
  const value = useContext(ToastContext)
  return value ?? { show: () => undefined, error: () => undefined }
}
