import { useMainButton } from '../buttons'
import { Intro, Page, Row, Section } from '../components/ui'
import { useData } from '../data'
import { ICopy, ISettings } from '../icons'
import { useNav } from '../nav'
import { copyText, haptic, openLink } from '../tg'
import { useToast } from '../toast'

export default function Success({ id, restored }) {
  const { details } = useData()
  const nav = useNav()
  const toast = useToast()
  const bot = details[id]

  useMainButton(bot ? { text: `Открыть @${bot.username}`, onClick: () => openLink(bot.link) } : null)

  if (!bot) return null
  const copy = async () => {
    haptic.impact('light')
    if (await copyText(bot.link)) toast.success('Ссылка скопирована')
  }
  return (
    <Page>
      <Intro
        media={<div className="tm-done-icon" />}
        title={restored ? 'Бот снова работает!' : 'Бот запущен!'}
        text={
          restored
            ? <><b>@{bot.username}</b> снова подключён. Подписчики, цены и настройки сохранились.</>
            : <><b>@{bot.username}</b> уже принимает клиентов. Отправьте ссылку друзьям или разместите в своём канале.</>
        }
      />
      <section className="tm-section">
        {/* поле со ссылкой копируется по иконке (и по тапу на само поле) */}
        <button type="button" className="tm-link-card" onClick={copy} aria-label="Скопировать ссылку">
          <span>{bot.link}</span>
          <ICopy width={22} height={22} className="tm-link-card-icon" />
        </button>
      </section>
      <Section footer="Цены стоят стандартные — их можно поменять на странице бота. Там же статистика и вывод заработка.">
        <Row icon={ISettings} title="Управление ботом" onClick={() => nav.back()} />
      </Section>
    </Page>
  )
}
