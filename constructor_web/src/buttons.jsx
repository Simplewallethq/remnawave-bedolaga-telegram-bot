import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { BUTTON_COLOR, WebApp, haptic, nativeButtons, nativeSecondary } from './tg'

// Нижние кнопки: в Telegram — нативные MainButton / SecondaryButton (как в референсе),
// в браузере — такие же кнопки внизу страницы.
// Экран объявляет кнопку хуком useMainButton({ text, onClick, ... }) — при уходе с экрана она скрывается.

const Ctx = createContext(null)

const DANGER = '#e3695d'
const SECONDARY_BG = '#2c2c2e'

export function ButtonsProvider({ children }) {
  const [main, setMain] = useState(null)
  const [secondary, setSecondary] = useState(null)
  return (
    <Ctx.Provider value={{ main, setMain, secondary, setSecondary }}>
      {children}
      {nativeButtons ? <NativeSync /> : <FallbackBar />}
    </Ctx.Provider>
  )
}

let owners = 0

function useBottomButton(kind, cfg) {
  const ctx = useContext(Ctx)
  const owner = useRef(0)
  if (!owner.current) owner.current = ++owners
  const set = kind === 'main' ? ctx.setMain : ctx.setSecondary
  const clickRef = useRef(cfg?.onClick)
  clickRef.current = cfg?.onClick
  const visible = !!cfg && cfg.visible !== false
  const { text, disabled, progress, danger } = cfg || {}

  useLayoutEffect(() => {
    const id = owner.current
    set(visible ? { text, disabled: !!disabled, progress: !!progress, danger: !!danger, click: clickRef, owner: id } : null)
  }, [set, visible, text, disabled, progress, danger])

  // при уходе с экрана убираем только свою кнопку — новый экран мог уже поставить свою
  useLayoutEffect(() => {
    const id = owner.current
    return () => set((cur) => (cur?.owner === id ? null : cur))
  }, [set])
}

export const useMainButton = (cfg) => useBottomButton('main', cfg)
export const useSecondaryButton = (cfg) => useBottomButton('secondary', cfg)
export const useBottomBarVisible = () => !!useContext(Ctx).main

function fire(btn) {
  if (!btn || btn.disabled || btn.progress) return
  haptic.impact('medium')
  btn.click.current?.()
}

function NativeSync() {
  const { main, secondary } = useContext(Ctx)
  const ref = useRef({ main, secondary })
  ref.current = { main, secondary }

  useEffect(() => {
    const onMain = () => fire(ref.current.main)
    const onSecondary = () => fire(ref.current.secondary)
    WebApp.MainButton.onClick(onMain)
    if (nativeSecondary) WebApp.SecondaryButton.onClick(onSecondary)
    return () => {
      WebApp.MainButton.offClick(onMain)
      if (nativeSecondary) WebApp.SecondaryButton.offClick(onSecondary)
    }
  }, [])

  useEffect(() => {
    const B = WebApp.MainButton
    if (!main) {
      B.hideProgress()
      B.hide()
      return
    }
    const color = main.danger ? DANGER : BUTTON_COLOR
    B.setParams({
      text: main.text,
      color,
      text_color: '#ffffff',
      is_active: !main.disabled,
      is_visible: true,
      ...(WebApp.isVersionAtLeast('7.10') ? { has_shine_effect: false } : {}),
    })
    main.progress ? B.showProgress(false) : B.hideProgress()
  }, [main])

  useEffect(() => {
    if (!nativeSecondary) return
    const B = WebApp.SecondaryButton
    if (!secondary || !main) {
      B.hide()
      return
    }
    B.setParams({
      text: secondary.text,
      color: SECONDARY_BG,
      text_color: secondary.danger ? DANGER : '#ffffff',
      is_active: !secondary.disabled,
      is_visible: true,
      position: 'bottom',
    })
  }, [secondary, main])

  // SecondaryButton до 7.10 нет — показываем её в странице
  if (!nativeSecondary && secondary && main) {
    return (
      <div className="tm-bottom-bar tm-bottom-bar-inline">
        <FallbackButton btn={secondary} secondary />
      </div>
    )
  }
  return null
}

function FallbackBar() {
  const { main, secondary } = useContext(Ctx)
  useEffect(() => {
    document.documentElement.classList.toggle('has-bottom-bar', !!main)
    document.documentElement.classList.toggle('has-bottom-bar-2', !!(main && secondary))
  }, [main, secondary])
  if (!main) return null
  return (
    <div className="tm-bottom-bar">
      <FallbackButton btn={main} />
      {secondary && <FallbackButton btn={secondary} secondary />}
    </div>
  )
}

function FallbackButton({ btn, secondary }) {
  const cls = ['tm-bottom-button', secondary && 'secondary', btn.danger && 'danger', btn.progress && 'progress']
  return (
    <button className={cls.filter(Boolean).join(' ')} disabled={btn.disabled} onClick={() => fire(btn)}>
      {btn.progress ? <span className="tm-spinner" /> : btn.text}
    </button>
  )
}
