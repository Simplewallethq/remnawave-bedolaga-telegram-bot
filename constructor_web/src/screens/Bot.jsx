import { useEffect, useState } from 'react'
import { api } from '../api'
import { Avatar, Empty, Intro, Loading, Page, Pill, Row, Section, Stats } from '../components/ui'
import { useBot, useData } from '../data'
import { money, num } from '../format'
import { IBot, ICopy, IGallery, IHandMoney, ILink, ISupport, ITag, ITrash } from '../icons'
import { useNav } from '../nav'
import { confirmPopup, copyText, openLink } from '../tg'
import { useToast } from '../toast'
import { CoverEditor, PricesEditor, SupportEditor } from './editors'

function useBotOrState(id) {
  const { bot, error } = useBot(id)
  const nav = useNav()
  if (error && !bot) {
    return [null, <Page key="e"><Empty icon={IBot} title="Бот не найден" text={error.message} action="На главную" onAction={() => nav.reset([['home']])} /></Page>]
  }
  if (!bot) return [null, <Page key="l"><Loading /></Page>]
  return [bot, null]
}

const STARTING_POLL_MS = 3000
const STARTING_POLL_LIMIT = 40 // ~2 минуты

/**
 * Работает ли бот: токен жив и вебхук смотрит на нас (запрос к Telegram — отдельно).
 * null — проверяем; 'starting' — только что подключён, сервер его ещё поднимает
 * (переспрашиваем, пока не поднимется); true / false — ответ.
 */
function useWorking(id) {
  const [working, setWorking] = useState(null)
  useEffect(() => {
    let cancelled = false
    let timer = null
    let attempts = 0
    const check = () => {
      api.botStatus(id)
        .then((s) => {
          if (cancelled) return
          if (s.starting && attempts++ < STARTING_POLL_LIMIT) {
            setWorking('starting')
            timer = setTimeout(check, STARTING_POLL_MS)
          } else setWorking(s.starting ? 'starting' : s.working)
        })
        .catch(() => !cancelled && setWorking(null))
    }
    check()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [id])
  return working
}

export default function Bot({ id }) {
  const [bot, placeholder] = useBotOrState(id)
  const { state, dropBot, refresh } = useData()
  const working = useWorking(id)
  const nav = useNav()
  const toast = useToast()
  if (!bot) return placeholder

  const st = bot.stats
  const monthly = Object.values(bot.prices.monthly)

  const remove = async () => {
    const ok = await confirmPopup({
      title: `Удалить @${bot.username}?`,
      message: 'Бот перестанет отвечать клиентам. Подписчики и настройки сохранятся: если подключите этого бота снова, всё вернётся.',
      ok: 'Удалить',
      destructive: true,
    })
    if (!ok) return
    try {
      await api.botDelete(bot.id)
      toast.success(`Бот @${bot.username} удалён`)
      nav.reset([['home']], () => dropBot(bot.id))
      refresh().catch(() => {})
    } catch (e) {
      toast.error(e.message)
    }
  }

  const edit = (field) => nav.push('edit', { id: bot.id, field })

  return (
    <Page>
      <Intro
        media={<Avatar id={bot.id} name={bot.title || bot.username} size={80} />}
        title={bot.title || bot.username}
        text={
          <>
            <button type="button" className="tm-link-more" onClick={() => openLink(bot.link)}>@{bot.username}</button>
            <div style={{ marginTop: 10 }}>
              {working === null ? <Pill>Проверяем…</Pill>
                : working === 'starting' ? <Pill tone="warning">Запускается…</Pill>
                : working ? <Pill tone="good">Работает</Pill>
                : <Pill tone="danger">Не работает</Pill>}
            </div>
          </>
        }
      />

      {working === false && (
        <section className="tm-section">
          <div className="tm-notice">
            <div className="tm-notice-text">
              Токен бота больше не действует. Удалите бота и подключите заново с новым токеном из @BotFather — подписчики и настройки сохранятся.
            </div>
          </div>
        </section>
      )}

      <Section>
        <Stats
          items={[
            { label: 'Пользователей', value: num(st.users_total) },
            { label: 'Новых вчера', value: num(st.users_yesterday) },
            { label: 'Оборот', value: money(st.turnover) },
          ]}
        />
      </Section>

      <Section title="Настройки">
        <Row icon={ISupport} title="Поддержка" value={bot.support ? `@${bot.support}` : 'не указана'} onClick={() => edit('support')} />
        <Row icon={IGallery} title="Обложка" value={bot.custom_cover ? 'своя' : 'стандартная'} onClick={() => edit('cover')} />
        <Row icon={ITag} title="Цены" value={`${money(Math.min(...monthly))} – ${money(Math.max(...monthly))}`} onClick={() => edit('prices')} />
      </Section>

      <Section title="Монетизация" footer={`Вы получаете ${state.revshare}% с каждого пополнения. Вывод — в USDT (TRC‑20), общий для всех ботов.`}>
        <Row icon={IHandMoney} title="Заработок и вывод" value={money(state.available)} valueTone="white" onClick={() => nav.push('earnings')} />
      </Section>

      <Section title="Действия">
        <Row icon={ILink} tone="accent" title={`Открыть @${bot.username}`} onClick={() => openLink(bot.link)} />
        <Row
          icon={ICopy}
          tone="accent"
          title="Скопировать ссылку"
          onClick={async () => (await copyText(bot.link)) && toast.success('Ссылка скопирована')}
        />
        <Row icon={ITrash} tone="destructive" title="Удалить бота" onClick={remove} />
      </Section>
    </Page>
  )
}

// --- Редактирование поля бота ----------------------------------------------------
const FIELDS = {
  support: { title: 'Поддержка', Editor: SupportEditor },
  cover: { title: 'Обложка бота', Editor: CoverEditor },
  prices: { title: 'Цены тарифов', Editor: PricesEditor },
}

export function Edit({ id, field }) {
  const [bot, placeholder] = useBotOrState(id)
  const nav = useNav()
  const toast = useToast()
  if (!bot) return placeholder
  const { title, Editor } = FIELDS[field] ?? FIELDS.support
  const onDone = (notice) => {
    if (notice) toast.success(notice)
    nav.back()
  }
  // у обложки картинка и так на весь экран — аватар не нужен
  const media = field === 'cover' ? null : <Avatar id={bot.id} name={bot.title || bot.username} size={56} />
  return (
    <Page>
      <Intro media={media} title={title} text={`@${bot.username} · настройки`} />
      <Editor scope={bot.id} target={bot} mode="edit" onDone={onDone} />
    </Page>
  )
}

export { useBotOrState }
