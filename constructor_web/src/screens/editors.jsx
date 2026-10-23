import { useEffect, useRef, useState } from 'react'
import { api, assetUrl, coverUrl } from '../api'
import { useMainButton } from '../buttons'
import { Field, Hint, Row, Section, Spinner, Warning } from '../components/ui'
import { useData } from '../data'
import { devices, maskWallet, money } from '../format'
import { ICamera, IGlobal } from '../icons'
import { haptic, hideKeyboard, pasteText } from '../tg'
import { useToast } from '../toast'

// Редакторы полей. Поддержка и обложка — общие для мастера (scope='draft', черновик в приложении)
// и настроек бота (scope=<id>, сразу на сервер). Цены — только настройки бота, кошелёк — владельца.
// Проверки здесь — для подсказок; окончательно всё проверяет сервер.

export const USERNAME_RX = /^(?:@|https?:\/\/t\.me\/|t\.me\/)?([A-Za-z0-9_]{5,32})\/?$/
export const TRON_RX = /^T[1-9A-HJ-NP-Za-km-z]{33}$/

const buttonText = (mode, fallback = 'Далее') => (mode === 'edit' ? 'Сохранить' : fallback)

// --- Поддержка ---------------------------------------------------------------
/** suggest — username создателя из initData: им предзаполняется поле. */
export function SupportEditor({ scope, target, mode, onDone, suggest }) {
  const { setDraft, putBot } = useData()
  const toast = useToast()
  const [value, setValue] = useState(() => (target.support || suggest || '').replace(/^@/, ''))
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const m = USERNAME_RX.exec(value.trim())
    if (!m) {
      setError(value.trim() ? 'Нужен username: латиница, цифры и _ (5–32 символа)' : 'Укажите аккаунт поддержки')
      return toast.error('Проверьте username')
    }
    hideKeyboard()
    const support = m[1]
    if (support === target.support) return onDone()
    if (scope === 'draft') {
      setDraft({ ...target, support })
      return onDone()
    }
    setBusy(true)
    try {
      const { bot } = await api.botPatch(scope, { support })
      putBot(bot)
      onDone(`Поддержка: @${support}`)
    } catch (e) {
      setError(e.message)
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  // [Далее] активна сразу: пустое или неверное значение подсветится ошибкой
  useMainButton({ text: buttonText(mode), onClick: submit, progress: busy })

  return (
    <Section
      title="Аккаунт поддержки"
      footer="Кнопка «Поддержка» в вашем боте будет вести на этот аккаунт."
      after={
        <>
          <Hint state="error">{error}</Hint>
          {suggest && value !== suggest && (
            <div className="tm-chips">
              <button type="button" className="tm-chip" onClick={() => { haptic.select(); setValue(suggest); setError(null) }}>
                Мой аккаунт @{suggest}
              </button>
            </div>
          )}
        </>
      }
    >
      <Field
        prefix="@"
        placeholder="flashvpn_support"
        value={value}
        error={error}
        enterKeyHint="done"
        onChange={(e) => {
          setValue(e.target.value.replace(/^(?:https?:\/\/)?t\.me\//, '').replace(/^@+/, '').replace(/[\s/]/g, ''))
          setError(null)
        }}
        onKeyDown={(e) => e.key === 'Enter' && submit()}
      />
    </Section>
  )
}

// --- Кошелёк (владельца, один на все боты) ----------------------------------
export function WalletEditor({ current, onDone }) {
  const { setWallet } = useData()
  const toast = useToast()
  const [value, setValue] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const trimmed = value.trim()

  const submit = async () => {
    if (!trimmed) {
      if (current) return onDone()
      setError('Вставьте адрес кошелька')
      return toast.error('Вставьте адрес кошелька')
    }
    if (!TRON_RX.test(trimmed)) {
      setError('Неверный адрес TRC‑20. Он начинается с T и содержит 34 символа')
      return toast.error('Неверный адрес TRC‑20')
    }
    setBusy(true)
    try {
      const { wallet } = await api.setWallet(trimmed)
      setWallet(wallet)
      onDone(`Кошелёк сохранён: ${maskWallet(wallet)}`)
    } catch (e) {
      setError(e.message)
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const paste = async () => {
    const text = (await pasteText()).trim()
    if (text) {
      haptic.select()
      setValue(text)
      setError(null)
    } else toast.warning('Буфер обмена пуст или недоступен')
  }

  useMainButton({ text: 'Сохранить', onClick: submit, progress: busy })

  return (
    <>
      <Section
        title="Адрес USDT (TRC‑20)"
        footer="На этот кошелёк будут приходить выплаты со всех ваших ботов."
        after={<Hint state={error ? 'error' : trimmed && TRON_RX.test(trimmed) ? 'success' : null}>
          {error || (trimmed && TRON_RX.test(trimmed) ? 'Адрес выглядит верно' : null)}
        </Hint>}
      >
        <Field
          mono
          multiline
          rows={2}
          placeholder={current || 'T…'}
          value={value}
          error={error}
          enterKeyHint="done"
          onChange={(e) => {
            setValue(e.target.value.replace(/\s/g, ''))
            setError(null)
          }}
          right={!value && <button type="button" className="tm-field-btn" onClick={paste}>Вставить</button>}
        />
      </Section>
      <Section>
        <Row icon={IGlobal} title="Сеть" value="TRON · TRC‑20" />
        <Row title="Сейчас" media={<span style={{ width: 26 }} />} value={maskWallet(current) || 'не указан'} valueTone={current ? 'white' : null} />
      </Section>
      <section className="tm-section">
        <Warning>Адрес начинается с <b>T</b> и содержит 34 символа. Перевод в другую сеть будет потерян.</Warning>
      </section>
    </>
  )
}

// --- Обложка -----------------------------------------------------------------
/**
 * Картинка на всю ширину — главный элемент: тап по ней = загрузить свою.
 * В мастере файл держится в черновике и уходит на сервер при запуске; у бота — загружается сразу.
 * onDone может быть async (в мастере это запуск бота) — на время выполнения кнопка с прогрессом.
 */
export function CoverEditor({ scope, target, mode, onDone }) {
  const { setDraft, putBot } = useData()
  const toast = useToast()
  const input = useRef(null)
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const isDraft = scope === 'draft'
  const custom = isDraft ? !!target.image : target.custom_cover

  // превью выбранного в мастере файла
  const [draftUrl, setDraftUrl] = useState(null)
  useEffect(() => {
    if (!isDraft || !target.image) return setDraftUrl(null)
    const url = URL.createObjectURL(target.image)
    setDraftUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [isDraft, target.image])
  useEffect(() => () => preview && URL.revokeObjectURL(preview), [preview])

  const pick = () => {
    if (busy) return
    haptic.impact('light')
    input.current?.click()
  }

  const upload = async (file) => {
    if (!file) return
    if (!file.type.startsWith('image/')) return toast.error('Нужна картинка (JPG или PNG)')
    if (file.size > 10 * 1024 * 1024) return toast.error('Картинка больше 10 МБ')
    if (isDraft) {
      setDraft({ ...target, image: file })
      return
    }
    setPreview(URL.createObjectURL(file))
    setBusy(true)
    try {
      const { bot } = await api.uploadCover(scope, file)
      putBot(bot)
      toast.success('Обложка обновлена')
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
      setPreview(null)
    }
  }

  const reset = async () => {
    haptic.impact('light')
    if (isDraft) return setDraft({ ...target, image: null })
    try {
      const { bot } = await api.resetCover(scope)
      putBot(bot)
      toast.success('Стандартная обложка')
    } catch (e) {
      toast.error(e.message)
    }
  }

  const finish = async () => {
    setFinishing(true)
    try {
      await onDone()
    } finally {
      setFinishing(false)
    }
  }

  useMainButton({
    text: mode === 'wizard' ? 'Запустить бота' : 'Готово',
    onClick: finish,
    progress: finishing,
    disabled: busy,
  })

  const src = preview || (isDraft ? draftUrl : custom ? coverUrl(target) : null) || (isDraft ? assetUrl('default-cover.png') : coverUrl(target))
  return (
    <section className="tm-section">
      <button type="button" className={`tm-cover${busy ? ' loading' : ''}`} onClick={pick} aria-label="Загрузить свою картинку">
        <img src={src} alt="" draggable={false} />
        <span className="tm-cover-tag">{preview ? 'Загружаем…' : custom ? 'Ваша обложка' : 'Стандартная'}</span>
        {busy && <Spinner size={30} />}
      </button>
      <p className="help-text">Лучше горизонтальная 1280×640 с логотипом</p>
      <div className="tm-cover-actions">
        <button type="button" className="set-photo-button" disabled={busy} onClick={pick}>
          <ICamera width={18} height={18} />
          {custom ? 'Заменить' : 'Загрузить свою'}
        </button>
        {custom && (
          <button type="button" className="tm-text-btn danger" disabled={busy} onClick={reset}>
            Вернуть стандартную
          </button>
        )}
      </div>
      <input ref={input} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={(e) => {
        upload(e.target.files?.[0])
        e.target.value = ''
      }} />
    </section>
  )
}

// --- Таблица цен -------------------------------------------------------------
const PERIOD_TITLES = { 30: '1 мес', 90: '3 мес', 180: '6 мес', 360: '1 год', 720: '2 года' }

export function PriceTable({ plans, periods }) {
  const days = [...new Set(Object.values(periods).flatMap((p) => Object.keys(p).map(Number)))].sort((a, b) => a - b)
  return (
    <div className="tm-table-wrap">
      <table className="tm-price-table">
        <thead>
          <tr>
            <th>Тариф</th>
            {days.map((d) => <th key={d}>{PERIOD_TITLES[d] || `${d} дн`}</th>)}
          </tr>
        </thead>
        <tbody>
          {Object.entries(plans).map(([key, plan]) => (
            <tr key={key}>
              <td>{plan.title}</td>
              {days.map((d) => <td key={d}>{periods[key]?.[d] !== undefined ? money(periods[key][d]) : '—'}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --- Цены --------------------------------------------------------------------
/** Месячные цены Solo/Plus/Pro; цены за 3 мес, год и 2 года сервер считает сам (с минимальными). */
export function PricesEditor({ target, onDone }) {
  const { config, putBot } = useData()
  const toast = useToast()
  const monthly = target.prices.monthly
  const [values, setValues] = useState(() => Object.fromEntries(Object.entries(monthly).map(([k, v]) => [k, String(v)])))
  const [preview, setPreview] = useState({ errors: {}, periods: target.prices.periods })
  const [busy, setBusy] = useState(false)
  const numbers = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, /^\d+$/.test(v) ? +v : null]))
  const complete = Object.values(numbers).every((v) => v)
  const changed = Object.keys(monthly).some((k) => numbers[k] !== monthly[k])

  // ошибки и итоговые цены — с сервера, по тем же правилам, что при сохранении
  useEffect(() => {
    if (!complete) return
    const timer = setTimeout(() => {
      api.pricesPreview(numbers).then(setPreview).catch(() => {})
    }, 300)
    return () => clearTimeout(timer)
  }, [values]) // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    if (!complete) return toast.error('Цена — целое число рублей')
    hideKeyboard()
    if (!changed) return onDone()
    setBusy(true)
    try {
      const { bot } = await api.botPatch(target.id, { prices: numbers })
      putBot(bot)
      onDone('Цены сохранены')
    } catch (e) {
      if (e.data?.errors) setPreview((p) => ({ ...p, errors: e.data.errors }))
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  useMainButton({ text: 'Сохранить', onClick: submit, progress: busy })

  const errors = preview.errors || {}
  return (
    <>
      <Section title="Цена за 1 месяц" footer="Plus — минимум на 10% дороже Solo, Pro — на 20% дороже Plus.">
        {Object.entries(config.plans).map(([key, plan]) => (
          <Row key={key} title={plan.title} desc={errors[key] || `${devices(plan.devices)} · от ${money(plan.floor)}`} className="tm-row-entity" chevron={false}>
            <label className={`tm-inline-price${errors[key] || numbers[key] === null ? ' error' : ''}`}>
              <input
                className="form-control"
                inputMode="numeric"
                enterKeyHint="done"
                maxLength={6}
                value={values[key]}
                onFocus={(e) => e.target.select()}
                onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value.replace(/\D/g, '') }))}
              />
              <span>₽</span>
            </label>
          </Row>
        ))}
      </Section>
      <Section title="Итоговые цены" footer="Цены за длинные периоды считаются автоматически из месячной.">
        <PriceTable plans={config.plans} periods={preview.periods} />
      </Section>
    </>
  )
}
