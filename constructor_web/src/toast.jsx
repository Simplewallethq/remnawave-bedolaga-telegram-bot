import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { haptic } from './tg'

// Тосты как в референсе (TWebApp.showToast / .tm-toast): пилюля снизу, success / warning / error + хаптик.

const Ctx = createContext(null)
export const useToast = () => useContext(Ctx)

export function ToastProvider({ children }) {
  const [toast, setToast] = useState(null)
  const [shown, setShown] = useState(false)
  const timers = useRef([])

  const show = useCallback((text, kind = 'success', duration = 2000) => {
    timers.current.forEach(clearTimeout)
    haptic.notify(kind === 'error' ? 'error' : kind === 'warning' ? 'warning' : 'success')
    setToast({ text, kind, id: Date.now() })
    setShown(false)
    timers.current = [
      setTimeout(() => setShown(true), 10),
      setTimeout(() => setShown(false), duration),
      setTimeout(() => setToast(null), duration + 300),
    ]
  }, [])

  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  const api = useRef(null)
  if (!api.current) {
    api.current = {
      success: (t, d) => show(t, 'success', d),
      warning: (t, d) => show(t, 'warning', d),
      error: (t, d) => show(t, 'error', d ?? 2600),
    }
  }

  return (
    <Ctx.Provider value={api.current}>
      {children}
      <div className="tm-toast-container">
        {toast && (
          <div key={toast.id} className={`tm-toast tm-toast-${toast.kind}${shown ? ' tm-toast-show' : ''}`}>
            <div>{toast.text}</div>
          </div>
        )}
      </div>
    </Ctx.Provider>
  )
}
