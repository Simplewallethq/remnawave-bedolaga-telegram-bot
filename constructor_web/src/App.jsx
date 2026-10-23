import { ButtonsProvider } from './buttons'
import { Empty, Loading } from './components/ui'
import { DataProvider, useData } from './data'
import { IArrowRight, ILock } from './icons'
import { NavProvider, useNav } from './nav'
import Bot, { Edit } from './screens/Bot'
import Earnings, { Wallet, Withdraw } from './screens/Earnings'
import Home from './screens/Home'
import How from './screens/How'
import Success from './screens/Success'
import Wizard from './screens/Wizard'
import { WebApp, inTelegram, startParam } from './tg'
import { ToastProvider } from './toast'

// неизвестный экран (например, сохранённый в sessionStorage из старой версии) → главная
const SCREENS = {
  home: Home, how: How, wizard: Wizard, success: Success, bot: Bot, edit: Edit,
  earnings: Earnings, withdraw: Withdraw, wallet: Wallet,
}

/** Стартовый стек: кнопка «Управлять» из бота открывает приложение с ?bot=<id>,
 *  ссылка t.me/<bot>/<app>?startapp=bot_<id> — тоже страницу бота. */
function initialStack() {
  const home = { name: 'home', params: {}, key: 0 }
  const id = new URLSearchParams(location.search).get('bot') || /^bot_(\d+)$/.exec(startParam())?.[1]
  return id && /^\d+$/.test(id) ? [home, { name: 'bot', params: { id: +id }, key: -1 }] : [home]
}

function Screen() {
  const { current } = useNav()
  const C = SCREENS[current.name] ?? Home
  return <C key={current.key} {...current.params} />
}

/** В браузере нет нативной BackButton — показываем свою шапку. */
function BrowserHeader() {
  const { canBack, back } = useNav()
  return (
    <header className="tm-browser-header">
      {canBack && (
        <button type="button" className="tm-browser-back" onClick={() => back()}>
          <IArrowRight width={20} height={20} style={{ transform: 'rotate(180deg)' }} />
          Назад
        </button>
      )}
      <span className="tm-browser-badge">браузер · dev</span>
    </header>
  )
}

function Root() {
  const { state, config, error } = useData()
  if (error) {
    const unauth = error.status === 401
    return (
      <Empty
        icon={ILock}
        title={unauth ? 'Откройте из Telegram' : 'Не удалось загрузить'}
        text={unauth ? 'Мини‑приложение работает внутри Telegram: откройте его кнопкой в чате с ботом.' : error.message}
        action={unauth && inTelegram ? 'Закрыть' : 'Повторить'}
        onAction={() => (unauth && inTelegram ? WebApp.close() : location.reload())}
      />
    )
  }
  if (!state || !config) return <Loading />
  return (
    <NavProvider initial={initialStack()}>
      {!inTelegram && <BrowserHeader />}
      <Screen />
    </NavProvider>
  )
}

export default function App() {
  return (
    <ToastProvider>
      <ButtonsProvider>
        <DataProvider>
          <Root />
        </DataProvider>
      </ButtonsProvider>
    </ToastProvider>
  )
}
