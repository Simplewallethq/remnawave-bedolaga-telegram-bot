import { WebApp } from './tg'

// API конструктора (app/constructor/api.py). Путь относительный: см. vite.config.js.
const BASE = 'api'

export class ApiError extends Error {
  constructor(message, field, status, data) {
    super(message)
    this.field = field
    this.status = status
    this.data = data || {} // доп. поля ошибки (code, username, errors …)
  }
}

async function request(method, path, body) {
  const headers = {}
  // подпись Telegram — сервер проверяет её токеном бота-конструктора
  if (WebApp?.initData) headers.Authorization = `tma ${WebApp.initData}`
  let payload
  if (body instanceof FormData) payload = body
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch(`${BASE}${path}`, { method, headers, body: payload })
  } catch {
    throw new ApiError('Нет соединения с сервером', null, 0)
  }
  let data = null
  try {
    data = await res.json()
  } catch {
    /* пустой ответ */
  }
  if (!res.ok) {
    const err = data?.detail
    if (err && typeof err === 'object' && !Array.isArray(err)) throw new ApiError(err.error, err.field, res.status, err)
    throw new ApiError(`Ошибка сервера (${res.status})`, null, res.status)
  }
  return data
}

export const api = {
  config: () => request('GET', '/config'),
  state: () => request('GET', '/state'),

  /** text — токен или весь ответ BotFather */
  checkToken: (text) => request('POST', '/token', { text }),
  /** { token, support, force, image? } → { bot, restored } */
  createBot({ token, support, force, image }) {
    const fd = new FormData()
    fd.append('token', token)
    fd.append('support', support || '')
    fd.append('force', force ? 'true' : 'false')
    if (image) fd.append('image', image, image.name || 'cover.jpg')
    return request('POST', '/bots', fd)
  },

  bot: (id) => request('GET', `/bots/${id}`),
  botStatus: (id) => request('GET', `/bots/${id}/status`),
  botPatch: (id, fields) => request('PATCH', `/bots/${id}`, fields),
  botDelete: (id) => request('DELETE', `/bots/${id}`),
  pricesPreview: (prices) => request('POST', '/prices/preview', { prices }),
  uploadCover(id, file) {
    const fd = new FormData()
    fd.append('image', file, file.name || 'cover.jpg')
    return request('POST', `/bots/${id}/cover`, fd)
  },
  resetCover: (id) => request('DELETE', `/bots/${id}/cover`),

  earnings: () => request('GET', '/earnings'),
  setWallet: (address) => request('PUT', '/wallet', { address }),
  payout: (amount) => request('POST', '/payouts', { amount }),
}

export const coverUrl = (bot) => `${BASE}/bots/${bot.id}/cover?v=${bot.cover_version}`
export const assetUrl = (name) => `${import.meta.env.BASE_URL}${name}`
