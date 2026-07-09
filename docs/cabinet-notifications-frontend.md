# Уведомления кабинета — гайд для фронтенда (LetoVPNSite)

Бэкенд отдаёт персистентную ленту уведомлений (read/unread, пагинация) и
realtime-доставку через **Server-Sent Events**. Всё живёт под `/cabinet/notifications*`.

Что нужно сделать на фронте:

1. Колокольчик с бейджем непрочитанных.
2. Панель/страница ленты с пагинацией.
3. Живая доставка: EventSource + тост при новом событии.
4. Отметка прочитанного (по одному и «прочитать все»).

---

## 1. Аутентификация

- **Все REST-запросы** — обычный cabinet JWT: `Authorization: Bearer <token>` (тот же, что для `/cabinet/me`).
- **SSE-стрим** — отдельный короткоживущий тикет (браузерный `EventSource` не умеет ставить заголовки):
  1. `POST /cabinet/notifications/stream-token` (с Bearer) → `{"token": "...", "expiresIn": 300}`;
  2. тикет подставляется в query стрима: `?ticket=<token>`.

Тикет живёт **5 минут** и нужен только в момент открытия соединения (открытое
соединение живёт дольше). На каждое переподключение запрашивайте новый тикет.

---

## 2. REST API

### `GET /cabinet/notifications`

Параметры query: `limit` (1–200, по умолчанию 50), `offset` (по умолчанию 0), `unreadOnly` (`true`/`false`).

```json
{
  "items": [
    {
      "id": "ntf_124",
      "type": "topup_success",
      "title": "Баланс пополнен",
      "body": "Средства зачислены на баланс.",
      "payload": { "amountKopeks": 50000, "paymentMethod": "yookassa" },
      "isRead": false,
      "createdAt": "2026-07-09T12:34:56"
    }
  ],
  "total": 120,
  "unreadCount": 3
}
```

- `id` — строка вида `ntf_<число>`. Числовая часть нужна для `since` и для `POST .../{id}/read`.
- `items` отсортированы от новых к старым.
- `createdAt` — ISO-строка **в UTC без суффикса `Z`** — при парсинге добавляйте его сами: `new Date(n.createdAt + "Z")`.

### Остальные эндпоинты

| Метод и путь | Ответ | Примечания |
|---|---|---|
| `GET /cabinet/notifications/unread-count` | `{"unreadCount": 3}` | дешёвый запрос для фонового обновления бейджа |
| `POST /cabinet/notifications/{id}/read` | `{"success": true, "unreadCount": 2}` | `{id}` — **числовая** часть (`124`, не `ntf_124`); `404` — не найдено/чужое/уже прочитано |
| `POST /cabinet/notifications/read-all` | `{"success": true, "marked": 3}` | |
| `POST /cabinet/notifications/stream-token` | `{"token": "<jwt>", "expiresIn": 300}` | |

После `.../read` и `.../read-all` обновляйте бейдж из ответа (`unreadCount`) или
запросите `unread-count` — не декрементируйте вслепую.

---

## 3. SSE-стрим

```
GET /cabinet/notifications/stream?ticket=<sse-jwt>&since=<lastNumericId>
```

- `ticket` — обязателен, из `stream-token`. Невалидный/просроченный → `401`.
- `since` — необязателен: числовой id последнего уже полученного уведомления.
  Сервер сначала дошлёт всё, что новее (**до 50 событий**), затем перейдёт в live-режим.
  Браузер при авто-reconnect также шлёт заголовок `Last-Event-ID` — сервер его понимает,
  но полагаться на нативный reconnect не стоит (см. ниже).

Формат событий:

```
id: 124
event: notification
data: {"id":"ntf_124","type":"ticket_reply","title":"Ответ поддержки","body":"...","payload":{"ticketId":7,"preview":"..."},"isRead":false,"createdAt":"2026-07-09T12:40:00"}
```

`data` — тот же JSON-объект, что и элемент `items` в REST-ленте.

Каждые **25 секунд** сервер шлёт keep-alive-комментарий `: ping` — на фронте он
не виден (EventSource игнорирует комментарии), обрабатывать не нужно.

Лимит — **5 одновременных стримов на пользователя**: при открытии шестого
сервер молча закрывает самый старый. Держите **одно соединение на вкладку**
(или одно на браузер через SharedWorker/BroadcastChannel, если хочется сэкономить).

---

## 4. Рекомендуемый клиент

```js
class NotificationStream {
  constructor(api, getAuthHeaders, { onNotification }) {
    this.api = api;
    this.getAuthHeaders = getAuthHeaders;
    this.onNotification = onNotification;
    this.lastId = 0;       // числовой id последнего полученного уведомления
    this.es = null;
    this.retryDelay = 2000;
    this.stopped = false;
  }

  async start() {
    this.stopped = false;
    await this.connect();
  }

  async connect() {
    if (this.stopped) return;

    // Тикет живёт 5 минут — берём свежий перед КАЖДЫМ подключением
    let ticket;
    try {
      const r = await fetch(`${this.api}/cabinet/notifications/stream-token`, {
        method: "POST",
        headers: this.getAuthHeaders(),
      });
      if (!r.ok) throw new Error(`stream-token ${r.status}`);
      ticket = (await r.json()).token;
    } catch (e) {
      this.scheduleReconnect();
      return;
    }

    const url = `${this.api}/cabinet/notifications/stream?ticket=${encodeURIComponent(ticket)}`
      + (this.lastId ? `&since=${this.lastId}` : "");
    this.es = new EventSource(url);

    this.es.addEventListener("notification", (e) => {
      const n = JSON.parse(e.data);
      this.lastId = Math.max(this.lastId, parseInt(n.id.slice(4), 10));
      this.retryDelay = 2000; // соединение живо — сбрасываем backoff
      this.onNotification(n);
    });

    this.es.onerror = () => {
      // Не полагаемся на нативный auto-reconnect: тикет в URL уже мог протухнуть.
      this.es.close();
      this.scheduleReconnect();
    };
  }

  scheduleReconnect() {
    if (this.stopped) return;
    const jitter = Math.random() * 1000;
    setTimeout(() => this.connect(), this.retryDelay + jitter);
    this.retryDelay = Math.min(this.retryDelay * 2, 30000);
  }

  stop() {
    this.stopped = true;
    this.es?.close();
  }
}
```

Использование:

```js
// 1. Начальная загрузка ленты и бейджа
const feed = await fetch(`${API}/cabinet/notifications?limit=20`, { headers: auth() })
  .then(r => r.json());
renderFeed(feed.items);
setBadge(feed.unreadCount);

// 2. Живой стрим
const stream = new NotificationStream(API, auth, {
  onNotification(n) {
    prependToFeed(n);
    incrementBadge();
    showToast(n);
  },
});
if (feed.items.length) {
  stream.lastId = parseInt(feed.items[0].id.slice(4), 10); // не дублировать уже показанное
}
stream.start();

// 3. Прочтение
async function markRead(n) {
  const r = await fetch(`${API}/cabinet/notifications/${n.id.slice(4)}/read`, {
    method: "POST", headers: auth(),
  });
  if (r.ok) setBadge((await r.json()).unreadCount);
}

// 4. Логаут / размонтирование
stream.stop();
```

---

## 5. Типы уведомлений и payload

Основной способ рендера — **по `type` + `payload` вашими i18n-строками**.
`title`/`body` — русский fallback с сервера: используйте их для неизвестных
типов (вперёд-совместимость) и **обязательно** для `broadcast` (там `body` —
свободный текст рассылки от админа).

Все суммы — в **копейках** (`50000` = 500 ₽). Все даты — ISO UTC.

| `type` | Когда приходит | Поля `payload` |
|---|---|---|
| `topup_success` | Успешное пополнение баланса | `amountKopeks`, `paymentMethod` (`yookassa`, `cryptobot`, `stars`, …), `description` |
| `subscription_purchased` | Куплена подписка | `amountKopeks?`, `periodDays?`, `newEndDate?`, `source?` |
| `subscription_renewed` | Подписка продлена вручную | `amountKopeks?`, `periodDays?`, `newEndDate?`, `source?` |
| `autopay_success` | Автопродление прошло | `amountKopeks?`, `periodDays?`, `newEndDate?`, `source: "autopay"` |
| `autopay_failed` | Не хватило средств на автопродление | `balanceKopeks`, `requiredKopeks` |
| `subscription_expiring` | Подписка истекает через N дней | `days`, `endDate`, `autopayEnabled` |
| `subscription_expired` | Подписка истекла | `endDate` |
| `trial_ending` | Триал заканчивается (~2 часа) | `endDate` |
| `referral_joined` | Новый реферал по вашей ссылке | `referralName`, `commissionPercent` |
| `referral_commission` | Начислена реферальная комиссия | `amountKopeks`, `percent`, `fromName` |
| `ticket_reply` | Ответ поддержки в тикете | `ticketId`, `preview` (первые 100 символов) |
| `broadcast` | Объявление от администрации | `broadcastId` — **текст берите из `body`** |
| `promo_offer` | Промо-предложение (зарезервировано) | — |

Поля с `?` могут отсутствовать — рендерите defensively. Неизвестные `type`
показывайте через `title`/`body`, не роняйте ленту.

Полезные CTA по типам: `subscription_expiring` / `subscription_expired` /
`trial_ending` / `autopay_failed` → на страницу продления/пополнения;
`ticket_reply` → в тикет `payload.ticketId`; `referral_*` → на страницу рефералки.

Пример TS-типа:

```ts
interface CabinetNotification {
  id: `ntf_${number}`;
  type: string;             // см. таблицу; может появиться новый — не падать
  title: string | null;
  body: string | null;
  payload: Record<string, unknown>;
  isRead: boolean;
  createdAt: string;        // ISO UTC без "Z"
}
```

---

## 6. Edge cases и правила

- **Тикет ≠ JWT.** Основной cabinet JWT в query стрима не сработает (401) — только тикет из `stream-token`.
- **Reconnect = новый тикет.** Никогда не переиспользуйте старый URL: тикет протухает за 5 минут.
- **Дедупликация.** После reconnect с `since` сервер может прислать то, что вы уже видели из REST-ленты — дедупите по `id`.
- **Backfill ограничен 50 событиями.** Если вкладка была закрыта долго, надёжнее перезагрузить ленту REST-запросом, а стрим открыть без `since` от свежайшего id.
- **Стрим не заменяет ленту.** События приходят по стриму только пока соединение открыто; источник истины — `GET /cabinet/notifications`.
- **404 на `/read`** — уведомление уже прочитано или id чужой/несуществующий; просто обновите ленту.
- **Мульти-таб.** Больше 5 вкладок → старые стримы закрываются сервером. `onerror`-логика выше корректно попробует переподключиться; чтобы не бороться за слоты, можно шарить одно соединение через `BroadcastChannel`.
- **`visibilitychange` (опционально).** Можно закрывать стрим на скрытой вкладке и переоткрывать с `since` при возврате — сэкономит соединения.
- **Пуш ≠ прочитано.** Показ тоста не помечает уведомление прочитанным — это делает только явный `POST .../read`.
