import { useMainButton } from '../buttons'
import { Expand, Intro, Logo, Page, Row, Section } from '../components/ui'
import { useData } from '../data'
import { IEye, IQuestion } from '../icons'
import { openLink } from '../tg'
import { useCreateBot } from './Home'

// Тексты — как на экране «Как это работает» в боте (app/constructor/screens.py)
const STEPS = [
  ['Создаёте бота', 'В @BotFather — и присылаете нам токен'],
  ['Указываете поддержку и обложку', 'Цены уже настроены — меняются на странице бота'],
  ['Клиенты покупают VPN', 'Прямо в вашем боте'],
  ['Вы выводите доход', 'В USDT (TRC‑20)'],
]

export default function How() {
  const { config, state } = useData()
  const create = useCreateBot()
  useMainButton(state.bots.length ? null : { text: 'Создать свой VPN', onClick: create })

  return (
    <Page>
      <Intro
        media={<Logo icon={IQuestion} size={72} tone="violet" />}
        title="Как это работает"
        text="Свой VPN‑сервис в Telegram без серверов и кода. Вы — владелец бота, мы — вся техническая часть."
      />
      <Section title={`${STEPS.length} шага`}>
        {STEPS.map(([title, desc], i) => (
          <Row key={title} media={<span className="tm-row-num">{i + 1}</span>} title={title} desc={desc} multiline />
        ))}
      </Section>
      <Section title="Вопросы">
        <Expand title="Что если VPN заблокируют?">
          Серверы и протоколы обновляем мы — ваш бот и клиенты продолжают работать без ваших действий.
        </Expand>
        <Expand title="Сколько я зарабатываю?">
          Вы получаете <b>{state.revshare}%</b> с каждого пополнения ваших клиентов. Цены по умолчанию уже настроены,
          изменить их можно на странице бота.
        </Expand>
        <Expand title="Как вывести деньги?">
          Укажите кошелёк USDT TRC‑20 и создайте заявку в разделе «Заработок». Выводятся продажи старше{' '}
          {config.hold_days} дней, комиссия сети — {config.payout_fee_usdt} USDT.
        </Expand>
        <Expand title="Это правда бесплатно?">
          Да. Создание и обслуживание бота — бесплатно, мы зарабатываем на доле с продаж.
        </Expand>
      </Section>
      {config.example_url && (
        <Section footer="Посмотрите работающий VPN‑бот глазами клиента.">
          <Row icon={IEye} title="Пример рабочего VPN‑сервиса" onClick={() => openLink(config.example_url)} />
        </Section>
      )}
    </Page>
  )
}
