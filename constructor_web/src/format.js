// Форматирование сумм, дат и имён

export const CURRENCY = '₽'

export const money = (n) => `${Math.round(n).toLocaleString('ru-RU').replace(/ |,/g, ' ')} ${CURRENCY}`

export const num = (n) => Math.round(n).toLocaleString('ru-RU').replace(/ |,/g, ' ')

export const maskWallet = (w) => (w ? `${w.slice(0, 6)}…${w.slice(-4)}` : null)

export function plural(n, one, few, many) {
  const m10 = n % 10
  const m100 = n % 100
  if (m10 === 1 && m100 !== 11) return one
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few
  return many
}

export const devices = (n) => `${n} ${plural(n, 'устройство', 'устройства', 'устройств')}`

/** Дата из ISO-строки сервера (время UTC без зоны). */
export function date(iso) {
  if (!iso) return ''
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`)
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })
}

export function initials(name = '') {
  const words = name.replace(/[^\p{L}\p{N}\s]/gu, ' ').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '?'
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)).toUpperCase()
}

// Палитра аватаров Telegram (градиенты как у пустых аватарок)
const AVATARS = [
  ['#ff885e', '#ff516a'],
  ['#ffcd6a', '#ffa85c'],
  ['#82b1ff', '#665fff'],
  ['#a0de7e', '#54cb68'],
  ['#53edd6', '#28c9b7'],
  ['#72d5fd', '#2a9ef1'],
  ['#e0a2f3', '#d669ed'],
]
export const avatarGradient = (id) => {
  const [a, b] = AVATARS[Math.abs(Number(id) || 0) % AVATARS.length]
  return `linear-gradient(180deg, ${a}, ${b})`
}

/** Нечёткий поиск как в референсе (BotList.eSearchInput → fuzzyMatch). */
export function fuzzyMatch(query, text) {
  const q = query.toLowerCase().replace(/^@/, '')
  const t = text.toLowerCase()
  let i = 0
  for (const ch of t) if (ch === q[i]) i++
  return i === q.length
}
