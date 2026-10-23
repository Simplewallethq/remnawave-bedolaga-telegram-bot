import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api } from './api'

// Состояние приложения: конфиг (тарифы, правила вывода), сводка владельца и список ботов,
// кэш подробных данных ботов, заработок и черновик мастера создания (живёт только в приложении).

const Ctx = createContext(null)
export const useData = () => useContext(Ctx)

export function DataProvider({ children }) {
  const [config, setConfig] = useState(null)
  const [state, setState] = useState(null)
  const [details, setDetails] = useState({})
  const [earnings, setEarnings] = useState(null)
  const [draft, setDraft] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    const s = await api.state()
    setState(s)
    return s
  }, [])

  useEffect(() => {
    Promise.all([api.config(), api.state()])
      .then(([c, s]) => {
        setConfig(c)
        setState(s)
      })
      .catch(setError)
  }, [])

  /** Сохранить подробные данные бота и обновить его строку в списке. */
  const putBot = useCallback((bot) => {
    setDetails((d) => ({ ...d, [bot.id]: bot }))
    setState((s) => {
      const short = { id: bot.id, username: bot.username, title: bot.title, link: bot.link, cover_version: bot.cover_version }
      const exists = s.bots.some((b) => b.id === bot.id)
      return { ...s, bots: exists ? s.bots.map((b) => (b.id === bot.id ? short : b)) : [...s.bots, short] }
    })
  }, [])

  const dropBot = useCallback((id) => {
    setDetails(({ [id]: _, ...rest }) => rest)
    setState((s) => ({ ...s, bots: s.bots.filter((b) => b.id !== id) }))
  }, [])

  const loadBot = useCallback(async (id) => {
    const { bot } = await api.bot(id)
    putBot(bot)
    return bot
  }, [putBot])

  const loadEarnings = useCallback(async () => {
    const e = await api.earnings()
    setEarnings(e)
    return e
  }, [])

  const setWallet = useCallback((wallet) => {
    setState((s) => ({ ...s, wallet }))
    setEarnings((e) => (e ? { ...e, wallet } : e))
  }, [])

  return (
    <Ctx.Provider
      value={{
        config, state, error, details, earnings, draft,
        refresh, putBot, dropBot, loadBot, loadEarnings, setEarnings, setWallet, setDraft,
      }}
    >
      {children}
    </Ctx.Provider>
  )
}

/** Подробные данные бота: из кэша сразу, затем свежие с сервера. */
export function useBot(id) {
  const { details, loadBot } = useData()
  const [error, setError] = useState(null)
  useEffect(() => {
    setError(null)
    loadBot(id).catch(setError)
  }, [id, loadBot])
  return { bot: details[id], error }
}

/** Заработок владельца: из кэша сразу, затем свежий с сервера. */
export function useEarnings() {
  const { earnings, loadEarnings } = useData()
  const [error, setError] = useState(null)
  useEffect(() => {
    setError(null)
    loadEarnings().catch(setError)
  }, [loadEarnings])
  return { earnings, error }
}
