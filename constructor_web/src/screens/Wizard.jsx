import { useState } from 'react'
import { api } from '../api'
import { useMainButton, useSecondaryButton } from '../buttons'
import { Avatar, Empty, Expand, Field, Hint, Intro, Logo, Page, Row, Section, Warning } from '../components/ui'
import { useData } from '../data'
import { IBot, ICheckCircle, IGallery, IMagic, IQuestion, ISupport } from '../icons'
import { useNav } from '../nav'
import { confirmPopup, haptic, openLink, pasteText, setClosingConfirmation, tgUser } from '../tg'
import { useToast } from '../toast'
import { CoverEditor, SupportEditor } from './editors'
import { resumeStep, useCreateBot } from './Home'

// Мастер создания — те же 3 шага, что в боте (app/constructor/screens.py):
// бот и токен → поддержка → обложка → запуск. Цены стоят стандартные, меняются на странице бота.
// Черновик живёт в приложении; на сервер уходит одним запросом при запуске.
export const STEPS = ['tok', 'sup', 'img']
const META = {
  tok: { title: 'Создание бота', icon: IBot, text: 'Создайте бота в @BotFather и вставьте его токен' },
  sup: { title: 'Поддержка', icon: ISupport, text: 'Куда клиентам писать с вопросами' },
  img: { title: 'Обложка', icon: IGallery, text: 'Картинка в сообщениях вашего бота' },
}

// токен внутри любого текста: чистый токен или весь ответ BotFather (как в боте)
const TOKEN_FIND = /(?<![A-Za-z0-9_:-])(\d{5,}:[A-Za-z0-9_-]{30,})(?![A-Za-z0-9_-])/
const WEBHOOK_WARNING = 'Этот бот уже подключён к другому сервису. Если продолжить, там он перестанет работать.'

function StepHead({ step, media }) {
  const n = STEPS.indexOf(step) + 1
  const meta = META[step]
  return (
    <>
      <div className="tm-steps" aria-label={`Шаг ${n} из ${STEPS.length}`}>
        {STEPS.map((s, i) => <span key={s} className={i < n ? 'done' : ''} />)}
      </div>
      <Intro
        media={media ?? <Logo icon={meta.icon} size={64} />}
        title={meta.title}
        text={
          <>
            <div className="tm-step-label">Шаг {n} из {STEPS.length}</div>
            {meta.text}
          </>
        }
      />
    </>
  )
}

/** Запуск бота: черновик → сервер → страница бота с экраном «Бот запущен». */
function useLaunch() {
  const { putBot, setDraft, refresh } = useData()
  const nav = useNav()
  const toast = useToast()
  return async (draft) => {
    setClosingConfirmation(true)
    try {
      const { bot, restored } = await api.createBot(draft)
      haptic.notify('success')
      nav.reset([['home'], ['bot', { id: bot.id }], ['success', { id: bot.id, restored }]], () => {
        putBot(bot)
        setDraft(null)
      })
      refresh().catch(() => {})
    } catch (e) {
      toast.error(e.message)
      if (e.field === 'token') nav.push('wizard', { step: 'tok' })
      else if (e.field === 'support') nav.push('wizard', { step: 'sup' })
    } finally {
      setClosingConfirmation(false)
    }
  }
}

export default function Wizard({ step }) {
  const { draft } = useData()
  const nav = useNav()
  const create = useCreateBot()

  if (!draft) {
    return (
      <Page>
        <Empty icon={IMagic} title="Черновик не найден" text="Приложение перезапускалось — начните создание заново." action="Начать заново" onAction={create} />
      </Page>
    )
  }
  // шаг нельзя открыть раньше, чем пройдены предыдущие
  const allowed = resumeStep(draft)
  const current = STEPS.includes(step) && STEPS.indexOf(step) <= STEPS.indexOf(allowed) ? step : allowed
  const next = () => nav.push('wizard', { step: STEPS[STEPS.indexOf(current) + 1] })

  return (
    <Page key={current}>
      {current === 'tok' && <TokenStep draft={draft} onDone={next} />}
      {current === 'sup' && <SupportStep draft={draft} onDone={next} />}
      {current === 'img' && <CoverStep draft={draft} />}
    </Page>
  )
}

// --- Шаг 1. Бот и токен ------------------------------------------------------
function TokenStep({ draft, onDone }) {
  const { setDraft } = useData()
  const launch = useLaunch()
  const toast = useToast()
  const [value, setValue] = useState('')
  const [error, setError] = useState(null)
  const [webhook, setWebhook] = useState(null) // { token, username, title } — у бота вебхук другого сервиса
  const [busy, setBusy] = useState(false)
  const token = TOKEN_FIND.exec(value)?.[1]
  const empty = !value.trim()
  const canContinue = !!draft.token && empty // вернулись с шага 2 — можно продолжить

  const connect = async () => {
    if (canContinue) return onDone()
    if (!token) return
    setBusy(true)
    setError(null)
    try {
      const info = await api.checkToken(value)
      if (info.restore) {
        // свой выключенный бот: вернётся со всеми прежними настройками
        const ok = await confirmPopup({
          title: `Вернуть @${info.username}?`,
          message: 'Это ваш бот, удалённый раньше. Подписчики, цены и настройки сохранились.',
          ok: 'Подключить',
        })
        if (ok) await launch({ token })
        return
      }
      if (info.foreign_webhook) {
        setWebhook({ token, username: info.username, title: info.title })
        haptic.notify('warning')
        return
      }
      setDraft({ token, username: info.username, title: info.title, force: false })
      haptic.notify('success')
      onDone()
    } catch (e) {
      setError(e.message)
      haptic.notify('error')
    } finally {
      setBusy(false)
    }
  }

  const confirmWebhook = () => {
    setDraft({ ...webhook, force: true })
    onDone()
  }

  const cancelWebhook = () => {
    setWebhook(null)
    setValue('')
  }

  const paste = async () => {
    const text = await pasteText()
    if (!text.trim()) return toast.warning('Буфер обмена пуст или недоступен')
    haptic.select()
    setValue(text)
    setError(null)
    setWebhook(null)
  }

  useMainButton(
    webhook
      ? { text: 'Подключить', onClick: confirmWebhook }
      : { text: canContinue ? 'Далее' : 'Подключить', onClick: connect, progress: busy, disabled: !token && !canContinue },
  )
  useSecondaryButton(webhook ? { text: 'Отмена', onClick: cancelWebhook } : null)

  let hint = null
  if (error) hint = { state: 'error', text: error }
  else if (token && value.trim() !== token) hint = { state: 'success', text: `Нашли токен ${token.slice(0, 10)}…${token.slice(-4)}` }
  else if (!empty && !token) hint = { text: 'Не вижу токен — вставьте его или весь ответ BotFather' }
  else if (canContinue) hint = { state: 'success', text: `Подключён @${draft.username}. Вставьте другой токен, чтобы сменить бота` }

  return (
    <>
      <StepHead step="tok" />
      <Section>
        <Row icon={IBot} tone="accent" title="Открыть @BotFather" onClick={() => openLink('https://t.me/BotFather')} />
        <Expand icon={IQuestion} title="Как создать бота">
          <ol className="tm-ol">
            <li>Откройте @BotFather и отправьте <b>/newbot</b></li>
            <li>Придумайте название, например <i>Flash VPN</i></li>
            <li>Придумайте username, оканчивающийся на <b>bot</b></li>
            <li>Скопируйте токен (или весь ответ BotFather) и вставьте ниже</li>
          </ol>
        </Expand>
      </Section>
      <Section
        title="Токен бота"
        footer="Токен нужен только для работы вашего бота и никому не показывается."
        after={<Hint state={hint?.state}>{hint?.text}</Hint>}
      >
        <Field
          mono
          multiline
          rows={3}
          placeholder="1234567890:AAH5mXk2Lq9dWv0pR7sTzYb3NcE8fGhJ4uI"
          value={value}
          error={!!error}
          disabled={!!webhook || busy}
          onChange={(e) => {
            setValue(e.target.value)
            setError(null)
          }}
          right={empty && <button type="button" className="tm-field-btn" onClick={paste}>Вставить</button>}
        />
      </Section>
      {webhook && (
        <section className="tm-section">
          <Warning tone="danger">
            <b>{WEBHOOK_WARNING}</b>
            <div style={{ marginTop: 4 }}>Бот @{webhook.username}</div>
          </Warning>
        </section>
      )}
    </>
  )
}

// --- Шаг 2. Поддержка --------------------------------------------------------
function BotCard({ draft }) {
  return (
    <div className="tm-bot-card">
      <Avatar id={(draft.token || '0').split(':')[0]} name={draft.title || draft.username} size={44} />
      <div className="tm-bot-card-main">
        <div className="tm-bot-card-name">{draft.title || draft.username}</div>
        <div className="tm-bot-card-sub">@{draft.username}</div>
      </div>
      <span className="tm-bot-card-ok">
        <ICheckCircle width={18} height={18} />
        подключён
      </span>
    </div>
  )
}

function SupportStep({ draft, onDone }) {
  const { state } = useData()
  // username создателя из initData — им предзаполняется поле
  const own = state.user?.username || tgUser()?.username || null
  return (
    <>
      <StepHead step="sup" media={<BotCard draft={draft} />} />
      <SupportEditor scope="draft" target={draft} mode="wizard" onDone={() => onDone()} suggest={own} />
    </>
  )
}

// --- Шаг 3. Обложка и запуск -------------------------------------------------
function CoverStep({ draft }) {
  const launch = useLaunch()
  return (
    <>
      <StepHead step="img" />
      <CoverEditor scope="draft" target={draft} mode="wizard" onDone={() => launch(draft)} />
    </>
  )
}
