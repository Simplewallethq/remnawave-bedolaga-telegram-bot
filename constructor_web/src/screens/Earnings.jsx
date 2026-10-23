import { useState } from 'react'
import { api } from '../api'
import { useMainButton } from '../buttons'
import { Empty, Field, Hint, Intro, Loading, Logo, Page, Pill, Row, Section, Warning } from '../components/ui'
import { useData, useEarnings } from '../data'
import { date, maskWallet, money } from '../format'
import { IChart, IClock, IDanger, IGlobal, IHandMoney, IHistory, IWallet, IWalletMoney } from '../icons'
import { useNav } from '../nav'
import { confirmPopup, haptic } from '../tg'
import { useToast } from '../toast'
import { WalletEditor } from './editors'

// Заработок — один на владельца (со всех его ботов): оборот — реальные пополнения клиентов,
// прибыль — доля владельца, к выводу — продажи старше hold_days дней за вычетом заявок.

const STATUS = { pending: ['warning', 'В обработке'], paid: ['good', 'Выплачено'], rejected: ['danger', 'Отклонено'] }

function useEarningsOrState() {
  const { earnings, error } = useEarnings()
  if (error && !earnings) return [null, <Page key="e"><Empty icon={IDanger} title="Не удалось загрузить" text={error.message} /></Page>]
  if (!earnings) return [null, <Page key="l"><Loading /></Page>]
  return [earnings, null]
}

export default function Earnings() {
  const [e, placeholder] = useEarningsOrState()
  const nav = useNav()
  const toast = useToast()

  const withdraw = () => {
    if (!e.wallet) {
      toast.error('Сначала укажите кошелёк для вывода')
      return nav.push('wallet')
    }
    if (!e.rate) return toast.error('Курс ЦБ сейчас недоступен, попробуйте позже')
    if (+e.available_usdt <= +e.fee_usdt) return toast.error(`Доступно меньше комиссии сети (${e.fee_usdt} USDT)`)
    nav.push('withdraw')
  }
  useMainButton(e ? { text: 'Вывести средства', onClick: withdraw } : null)
  if (!e) return placeholder

  return (
    <Page>
      <Intro
        media={<Logo icon={IWalletMoney} size={72} tone="green" />}
        title={<span className="tm-main-intro-amount">{money(e.available)}</span>}
        text={<>Доступно к выводу{e.available_usdt ? <> · ≈ {e.available_usdt} USDT</> : null}</>}
      />
      <Section title="Статистика" footer={`К выводу — продажи старше ${e.hold_days} дней за вычетом заявок на вывод.`}>
        <Row icon={IChart} title="Оборот всего" value={money(e.turnover_total)} />
        <Row icon={IChart} title="Оборот за вчера" value={money(e.turnover_yesterday)} />
        <Row icon={IHandMoney} title={`Прибыль всего · ${e.revshare}%`} value={money(e.profit_total)} />
        <Row icon={IHandMoney} title="Прибыль за вчера" value={money(e.profit_yesterday)} />
        <Row icon={IHistory} title="Выведено и в заявках" value={money(e.paid_out)} />
      </Section>
      <Section
        title="Кошелёк"
        footer={e.rate ? `Выплата в USDT по курсу ${e.rate} ₽, комиссия сети ${e.fee_usdt} USDT.` : 'Выплата в USDT по курсу ЦБ.'}
      >
        <Row
          icon={IWallet}
          title="USDT · TRC‑20"
          value={maskWallet(e.wallet) || 'Указать'}
          valueTone={e.wallet ? null : 'accent'}
          onClick={() => nav.push('wallet')}
        />
      </Section>
      <Section title="Заявки на вывод">
        {e.payouts.length ? (
          e.payouts.map((p) => (
            <Row key={p.id} icon={IClock} title={`№${p.id} · ${p.send_usdt} USDT`} desc={`${date(p.created_at)} · ${maskWallet(p.wallet)}`}>
              <Pill tone={STATUS[p.status]?.[0]}>{STATUS[p.status]?.[1] ?? p.status}</Pill>
            </Row>
          ))
        ) : (
          <div className="tm-results-empty">Заявок на вывод пока не было</div>
        )}
      </Section>
    </Page>
  )
}

export function Wallet() {
  const { state } = useData()
  const nav = useNav()
  const toast = useToast()
  return (
    <Page>
      <Intro media={<Logo icon={IWallet} size={64} tone="green" />} title="Кошелёк для вывода" text="Один на все ваши боты" />
      <WalletEditor
        current={state.wallet}
        onDone={(notice) => {
          if (notice) toast.success(notice)
          nav.back()
        }}
      />
    </Page>
  )
}

export function Withdraw() {
  const [e, placeholder] = useEarningsOrState()
  const { setEarnings, refresh } = useData()
  const nav = useNav()
  const toast = useToast()
  const [value, setValue] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const amount = /^\d+(\.\d{1,2})?$/.test(value) ? +value : 0

  const submit = async () => {
    const fee = +e.fee_usdt
    if (!amount) return setError('Введите сумму числом, например 50'), toast.error('Введите сумму')
    if (amount > +e.available_usdt) return setError(`Доступно ${e.available_usdt} USDT`), toast.error('Недостаточно средств')
    if (amount <= fee) return setError(`Сумма должна быть больше комиссии (${e.fee_usdt} USDT)`), toast.error('Сумма меньше комиссии')
    const send = (amount - fee).toFixed(2)
    const ok = await confirmPopup({
      title: 'Подтвердите вывод',
      message: `Будет отправлено ${send} USDT (${value} − комиссия ${e.fee_usdt}) на ${e.wallet}.\n\nДействие нельзя отменить. Срок — до 48 часов.`,
      ok: 'Подтвердить',
    })
    if (!ok) return
    setBusy(true)
    try {
      const { payout, earnings } = await api.payout(value)
      setEarnings(earnings)
      refresh().catch(() => {})
      toast.success(`Заявка №${payout.id} на ${payout.send_usdt} USDT принята`, 2600)
      nav.back()
    } catch (err) {
      setError(err.message)
      toast.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  useMainButton(e ? { text: amount ? `Вывести ${value} USDT` : 'Вывести', onClick: submit, progress: busy } : null)
  if (!e) return placeholder

  const setAll = () => {
    haptic.select()
    setValue(e.available_usdt)
    setError(null)
  }

  return (
    <Page>
      <Intro
        media={<Logo icon={IHandMoney} size={64} tone="green" />}
        title="Вывод средств"
        text={<>Доступно: <b>{e.available_usdt} USDT</b> · {money(e.available)}</>}
      />
      <Section title="Сумма в USDT" after={<Hint state="error">{error}</Hint>}>
        <Field
          inputMode="decimal"
          enterKeyHint="done"
          placeholder={`больше ${e.fee_usdt}`}
          value={value}
          suffix="USDT"
          error={error}
          onChange={(ev) => {
            setValue(ev.target.value.replace(',', '.').replace(/[^\d.]/g, '').slice(0, 12))
            setError(null)
          }}
          right={<button type="button" className="tm-field-btn" onClick={setAll}>Всё</button>}
        />
      </Section>
      <Section>
        <Row icon={IGlobal} title="Сеть" value="USDT · TRC‑20" />
        <Row icon={IWallet} title="Кошелёк" value={maskWallet(e.wallet)} valueTone="white" />
        <Row icon={IHandMoney} title="Комиссия сети" value={`${e.fee_usdt} USDT`} />
        <Row icon={IClock} title="Срок" value="до 48 часов" />
      </Section>
      <section className="tm-section">
        <Warning>Проверьте адрес — средства на чужой кошелёк вернуть невозможно.</Warning>
      </section>
    </Page>
  )
}
