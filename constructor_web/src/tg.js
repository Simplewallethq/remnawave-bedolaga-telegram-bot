// Обёртка над Telegram.WebApp: безопасные вызовы (с проверкой версии) и фолбэки для обычного браузера.
// Механики повторяют референс (Reference/webapp-common.js → TWebApp.initOnce).

export const WebApp = window.Telegram?.WebApp ?? null

/** Открыто ли внутри Telegram (в браузере platform === 'unknown' и initData пустая). */
export const inTelegram = !!(WebApp && WebApp.initData && WebApp.platform !== 'unknown')
export const platform = WebApp?.platform ?? 'unknown'
export const isMobile = ['ios', 'android'].includes(platform)

const COLORS = { header: '#000000', bg: '#000000', bottomBar: '#1c1c1e', button: '#248bda' }

export const supports = (v) => inTelegram && WebApp.isVersionAtLeast(v)

function safe(fn) {
  try {
    return fn()
  } catch (e) {
    if (import.meta.env.DEV) console.warn('[tg]', e)
  }
}

export function init() {
  const root = document.documentElement
  root.classList.add(inTelegram ? 'tg' : 'browser', `platform-${platform}`)
  if (isMobile) root.classList.add('mobile')

  if (inTelegram) {
    safe(() => WebApp.ready())
    safe(() => WebApp.expand())
    if (supports('6.1')) {
      safe(() => WebApp.setHeaderColor(COLORS.header))
      safe(() => WebApp.setBackgroundColor(COLORS.bg))
    }
    if (supports('7.10')) safe(() => WebApp.setBottomBarColor(COLORS.bottomBar))
    // 7.7: свайп вниз не сворачивает приложение (мешает скроллу и жестам)
    if (supports('7.7')) safe(() => WebApp.disableVerticalSwipes())
    if (isMobile && supports('8.0')) {
      safe(() => WebApp.requestFullscreen())
      safe(() => WebApp.lockOrientation())
    }
    const syncFullscreen = () => root.classList.toggle('fullscreen', !!WebApp.isFullscreen)
    syncFullscreen()
    safe(() => WebApp.onEvent('fullscreenChanged', syncFullscreen))
    // высота вьюпорта без клавиатуры — для нижних отступов
    const syncViewport = () => {
      root.style.setProperty('--app-height', `${WebApp.viewportStableHeight || window.innerHeight}px`)
    }
    syncViewport()
    safe(() => WebApp.onEvent('viewportChanged', syncViewport))
  }

  preventZoom()
  preventDefaults()
}

/** Полный запрет масштабирования: pinch, двойной тап, ctrl+колесо, ctrl +/−. */
function preventZoom() {
  const block = (e) => e.preventDefault()
  // iOS Safari/WKWebView игнорирует user-scalable=no — гасим жесты вручную
  document.addEventListener('gesturestart', block, { passive: false })
  document.addEventListener('gesturechange', block, { passive: false })
  document.addEventListener('gestureend', block, { passive: false })
  document.addEventListener(
    'touchmove',
    (e) => {
      if (e.touches.length > 1 || (e.scale !== undefined && e.scale !== 1)) e.preventDefault()
    },
    { passive: false },
  )
  // двойной тап (touch-action: manipulation в CSS + страховка для старых WebView)
  let lastTouchEnd = 0
  document.addEventListener(
    'touchend',
    (e) => {
      const now = Date.now()
      if (now - lastTouchEnd < 300 && !e.target.closest('input, textarea')) e.preventDefault()
      lastTouchEnd = now
    },
    { passive: false },
  )
  // десктоп-клиенты Telegram: ctrl/cmd + колесо и ctrl/cmd + '+', '-', '0'
  window.addEventListener('wheel', (e) => e.ctrlKey && e.preventDefault(), { passive: false })
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && ['+', '-', '=', '0'].includes(e.key)) e.preventDefault()
  })
}

/** Поведение нативного приложения: без контекстного меню, перетаскивания картинок, выделения. */
function preventDefaults() {
  document.addEventListener('contextmenu', (e) => {
    if (!e.target.closest('input, textarea, .selectable')) e.preventDefault()
  })
  document.addEventListener('dragstart', (e) => {
    if (e.target.tagName === 'IMG' || e.target.tagName === 'A') e.preventDefault()
  })
  // Enter в однострочном поле — скрыть клавиатуру (9.1: hideKeyboard) и отправить форму
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.matches('input')) hideKeyboard()
  })
}

export function hideKeyboard() {
  if (supports('9.1')) safe(() => WebApp.hideKeyboard())
  else document.activeElement?.blur?.()
}

// --- хаптика ----------------------------------------------------------------
export const haptic = {
  impact(style = 'light') {
    if (supports('6.1')) safe(() => WebApp.HapticFeedback.impactOccurred(style))
  },
  select() {
    if (supports('6.1')) safe(() => WebApp.HapticFeedback.selectionChanged())
  },
  notify(type) {
    if (supports('6.1')) safe(() => WebApp.HapticFeedback.notificationOccurred(type))
  },
}

// --- ссылки -----------------------------------------------------------------
export function openLink(url) {
  haptic.impact('soft')
  if (inTelegram && /^https?:\/\/t\.me\//.test(url)) return safe(() => WebApp.openTelegramLink(url))
  if (inTelegram) return safe(() => WebApp.openLink(url))
  window.open(url, '_blank', 'noopener')
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const ta = Object.assign(document.createElement('textarea'), { value: text })
    ta.style.cssText = 'position:fixed;opacity:0;top:0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  }
}

export async function pasteText() {
  try {
    return (await navigator.clipboard.readText()) || ''
  } catch {
    return ''
  }
}

// --- диалоги ----------------------------------------------------------------
/** Подтверждение: нативный popup Telegram (6.2+), иначе window.confirm. */
export function confirmPopup({ title, message, ok = 'OK', destructive = false }) {
  haptic.impact('soft')
  if (supports('6.2')) {
    return new Promise((resolve) => {
      safe(() =>
        WebApp.showPopup(
          {
            title,
            message,
            buttons: [{ id: 'ok', type: destructive ? 'destructive' : 'default', text: ok }, { type: 'cancel' }],
          },
          (id) => resolve(id === 'ok'),
        ),
      )
    })
  }
  return Promise.resolve(window.confirm([title, message].filter(Boolean).join('\n\n')))
}

export function setClosingConfirmation(on) {
  if (!supports('6.2')) return
  safe(() => (on ? WebApp.enableClosingConfirmation() : WebApp.disableClosingConfirmation()))
}

// --- нативные кнопки ----------------------------------------------------------
export const nativeButtons = inTelegram && WebApp.isVersionAtLeast('6.0')
export const nativeSecondary = inTelegram && WebApp.isVersionAtLeast('7.10')
export const nativeBack = inTelegram && WebApp.isVersionAtLeast('6.1')

export const BUTTON_COLOR = COLORS.button

export function startParam() {
  return WebApp?.initDataUnsafe?.start_param || new URLSearchParams(location.search).get('tgWebAppStartParam') || ''
}

export function tgUser() {
  return WebApp?.initDataUnsafe?.user || null
}
