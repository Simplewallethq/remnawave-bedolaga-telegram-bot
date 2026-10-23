import { useState } from 'react'
import { useMainButton } from '../buttons'
import { Avatar, Intro, LinkMore, Logo, Page, Row, SearchField, Section, Stats } from '../components/ui'
import { useData } from '../data'
import { fuzzyMatch, money, num, plural } from '../format'
import { IAddCircle, ICard, IEye, IHandMoney, IMagic, IQuestion, IServer, IShield, IWallet, IWalletMoney } from '../icons'
import { useNav } from '../nav'
import { openLink } from '../tg'
import { useToast } from '../toast'

/** Шаг мастера, с которого продолжить черновик (STEPS: tok → sup → img). */
export const resumeStep = (d) => (!d.token ? 'tok' : !d.support ? 'sup' : 'img')
const STEP_NUM = { tok: 1, sup: 2, img: 3 }

export function useCreateBot() {
  const { state, config, setDraft } = useData()
  const nav = useNav()
  const toast = useToast()
  return () => {
    if (state.bots.length >= config.max_bots) return toast.error(`У вас уже ${config.max_bots} ботов — это максимум`)
    setDraft({})
    nav.push('wizard', { step: 'tok' })
  }
}

export default function Home() {
  const { state, config, draft } = useData()
  const nav = useNav()
  const create = useCreateBot()
  const [query, setQuery] = useState('')
  const { bots, stats } = state
  const hasBots = bots.length > 0

  // главная кнопка — «Создать свой VPN», пока ботов нет
  useMainButton(hasBots ? null : { text: 'Создать свой VPN', onClick: create })

  const shown = query ? bots.filter((b) => fuzzyMatch(query, `${b.title} ${b.username}`)) : bots
  // деньги есть, а кошелька нет — вывести некуда
  const noWallet = state.available > 0 && !state.wallet
  const draftStep = draft?.token ? resumeStep(draft) : null

  return (
    <Page>
      <Intro
        media={<Logo icon={IShield} size={72} />}
        title="VPN Constructor"
        text={
          <>
            {hasBots ? (
              <>С возвращением! Ваши VPN‑сервисы работают. </>
            ) : (
              <>Запустите собственный VPN‑сервис в Telegram за 5 минут — бесплатно и без программирования. </>
            )}
            <LinkMore onClick={() => nav.push('how')}>Подробнее</LinkMore>
          </>
        }
      />

      {hasBots ? (
        <>
          <section className="tm-section">
            <Stats
              items={[
                { label: plural(bots.length, 'Бот', 'Бота', 'Ботов'), value: bots.length },
                { label: 'Пользователей', value: num(stats.users_total) },
                { label: 'К выводу', value: money(state.available) },
              ]}
            />
            {noWallet && (
              <div className="tm-notice">
                <IWallet width={22} height={22} className="tm-notice-icon" />
                <div className="tm-notice-text">Укажите кошелёк, чтобы вывести <b>{money(state.available)}</b></div>
                <button type="button" className="tm-notice-btn" onClick={() => nav.push('wallet')}>
                  Указать
                </button>
              </div>
            )}
          </section>
          {bots.length > 3 && <SearchField value={query} onChange={setQuery} />}
        </>
      ) : (
        <Section title="Что вы получаете">
          <Row icon={IServer} multiline title="Серверы, протоколы и ключи" desc="Всё на нас — никаких серверов и настроек" />
          <Row icon={ICard} multiline title="Приём оплат и выдача доступа" desc="Клиенты платят и получают ключ автоматически" />
          <Row icon={IWalletMoney} multiline title={`${state.revshare}% с каждой оплаты — ваши`} desc="Вывод в USDT (TRC‑20)" />
        </Section>
      )}

      {(hasBots || draftStep) && (
        <Section title="Мои боты">
          {!query && hasBots && <Row icon={IAddCircle} tone="accent" title="Создать нового бота" onClick={create} />}
          {!query && draftStep && (
            <Row
              icon={IMagic}
              title="Продолжить создание"
              desc={`${draft.username ? `@${draft.username} · ` : ''}шаг ${STEP_NUM[draftStep]} из 3`}
              onClick={() => nav.push('wizard', { step: draftStep })}
            />
          )}
          {shown.map((b) => (
            <Row
              key={b.id}
              className="tm-row-entity"
              media={<Avatar id={b.id} name={b.title || b.username} />}
              title={b.title || b.username}
              desc={`@${b.username}`}
              onClick={() => nav.push('bot', { id: b.id })}
            />
          ))}
          {query && !shown.length && <div className="tm-results-empty">Ничего не найдено по запросу «{query}»</div>}
        </Section>
      )}

      {hasBots && (
        <Section title="Монетизация" footer={`Вы получаете ${state.revshare}% с каждого пополнения клиентов ваших ботов.`}>
          <Row icon={IHandMoney} title="Заработок и вывод" value={money(state.available)} valueTone="white" onClick={() => nav.push('earnings')} />
        </Section>
      )}

      <Section title="Помощь">
        <Row icon={IQuestion} title="Как это работает?" onClick={() => nav.push('how')} />
        {config.example_url && <Row icon={IEye} title="Пример рабочего VPN‑сервиса" onClick={() => openLink(config.example_url)} />}
      </Section>
    </Page>
  )
}
