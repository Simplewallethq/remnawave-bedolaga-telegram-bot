import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { WebApp, haptic, hideKeyboard, isMobile, nativeBack } from './tg'

// Стековая навигация экранов. Назад — нативная BackButton Telegram, свайп от левого края
// (как TBackButton.initBackSwipe в референсе) или кнопка в шапке в браузере.

const NavCtx = createContext(null)
export const useNav = () => useContext(NavCtx)

const STORE_KEY = 'nav-stack'
let seq = 0
const entry = (name, params = {}) => ({ name, params, key: ++seq })

function loadStack(initial) {
  // Открыли ссылкой на конкретный экран (кнопка «Управлять» из бота) — она важнее сохранённого стека.
  if (initial.length > 1) return initial
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORE_KEY))
    if (Array.isArray(saved) && saved.length) return saved.map((e) => entry(e.name, e.params))
  } catch {
    /* sessionStorage недоступен */
  }
  return initial
}

/** Анимация смены экрана через View Transitions API (как в референсе), если поддерживается. */
function transition(update, dir) {
  hideKeyboard()
  const root = document.documentElement
  if (!document.startViewTransition || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    update()
    window.scrollTo(0, 0)
    return
  }
  root.dataset.navDir = dir
  const vt = document.startViewTransition(() => {
    flushSync(update)
    window.scrollTo(0, 0)
  })
  vt.finished.finally(() => delete root.dataset.navDir)
}

export function NavProvider({ initial, children }) {
  const [stack, setStack] = useState(() => loadStack(initial))
  const stackRef = useRef(stack)
  stackRef.current = stack

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(stack.map(({ name, params }) => ({ name, params }))))
    } catch {
      /* ignore */
    }
  }, [stack])

  const push = useCallback((name, params) => {
    haptic.impact('light')
    transition(() => setStack((s) => [...s, entry(name, params)]), 'forward')
  }, [])
  const replace = useCallback((name, params) => {
    transition(() => setStack((s) => [...s.slice(0, -1), entry(name, params)]), 'forward')
  }, [])
  const back = useCallback((n = 1) => {
    if (stackRef.current.length <= 1) return
    transition(() => setStack((s) => s.slice(0, Math.max(1, s.length - n))), 'back')
  }, [])
  /** Вернуться к ближайшему экрану name (или сбросить стек до него). */
  const backTo = useCallback((name, params) => {
    const s = stackRef.current
    const i = s.findLastIndex((e) => e.name === name)
    if (i >= 0) transition(() => setStack(s.slice(0, i + 1)), 'back')
    else transition(() => setStack([s[0], entry(name, params)]), 'back')
  }, [])
  /** Новый стек. apply — изменения данных, которые надо применить в том же кадре, что и смену экрана
   *  (иначе старый экран успеет перерисоваться с новыми данными — «мелькание»). */
  const reset = useCallback((list, apply) => {
    transition(() => {
      apply?.()
      setStack(list.map(([n, p]) => entry(n, p)))
    }, 'back')
  }, [])

  const value = useMemo(
    () => ({ stack, current: stack[stack.length - 1], canBack: stack.length > 1, push, replace, back, backTo, reset }),
    [stack, push, replace, back, backTo, reset],
  )

  return (
    <NavCtx.Provider value={value}>
      <BackButtonSync />
      {isMobile && <SwipeBack />}
      {children}
    </NavCtx.Provider>
  )
}

function BackButtonSync() {
  const { canBack, back } = useNav()
  const backRef = useRef(back)
  backRef.current = back

  useEffect(() => {
    if (!nativeBack) return
    const onClick = () => backRef.current()
    WebApp.BackButton.onClick(onClick)
    return () => WebApp.BackButton.offClick(onClick)
  }, [])

  useEffect(() => {
    if (!nativeBack) return
    canBack ? WebApp.BackButton.show() : WebApp.BackButton.hide()
  }, [canBack])
  return null
}

/** Свайп вправо от края — назад, с индикатором и хаптиком (порт TBackButton.initBackSwipe). */
function SwipeBack() {
  const { canBack, back } = useNav()
  const ref = useRef(null)
  const state = useRef({ canBack, back })
  state.current = { canBack, back }

  useEffect(() => {
    const threshold = 120
    let sx = 0, sy = 0, ex = 0, type = null, notified = false, skip = false
    const el = ref.current
    const interp = (t, start, end) => (t <= 0 ? start : start + (end - start) * (t / (t + 0.5)))

    const onStart = (e) => {
      sx = ex = e.touches[0].screenX
      sy = e.touches[0].screenY
      // горизонтальные жесты внутри скролл-лент и полей не перехватываем
      skip = !state.current.canBack || e.touches.length > 1 || !!e.target.closest('input, textarea, .no-swipe')
    }
    const onMove = (e) => {
      if (skip) return
      ex = e.changedTouches[0].screenX
      const dx = ex - sx
      const dy = e.changedTouches[0].screenY - sy
      if (type === 'h') e.cancelable && e.preventDefault()
      else if (!e.cancelable) return
      if (!type && Math.abs(dx) > 30 && Math.abs(dy) < 30 && dx > 0) {
        type = 'h'
        e.preventDefault()
      }
      if (type !== 'h') return
      if (dx > threshold && !notified) {
        notified = true
        haptic.impact('soft')
      }
      if (dx < threshold) notified = false
      el.style.insetInlineStart = `${interp(dx / 1.2 / 120, 0, 130) - 40 - 52}px`
    }
    const onEnd = () => {
      if (type === 'h' && ex - sx > threshold) state.current.back()
      type = null
      notified = false
      el.style.insetInlineStart = '-92px'
    }
    document.addEventListener('touchstart', onStart, { passive: true })
    document.addEventListener('touchmove', onMove, { passive: false })
    document.addEventListener('touchend', onEnd)
    document.addEventListener('touchcancel', onEnd)
    return () => {
      document.removeEventListener('touchstart', onStart)
      document.removeEventListener('touchmove', onMove)
      document.removeEventListener('touchend', onEnd)
      document.removeEventListener('touchcancel', onEnd)
    }
  }, [])

  return <div ref={ref} className="tm-swipe-back" />
}
