# Уведомления личного кабинета (лента + SSE)

Персистентная лента уведомлений для фронта LetoVPNSite: read/unread-состояние,
пагинация и realtime-доставка через Server-Sent Events, пока вкладка открыта.

## Архитектура

- Таблица `cabinet_notifications` (модель `CabinetNotification` в `app/database/models.py`).
- Единая точка создания — `app/services/cabinet_notification_service.py::notify()`:
  сохраняет строку и публикует событие в `CabinetNotificationHub` (in-process
  fan-out на `asyncio.Queue`; бот и uvicorn работают в одном процессе с одним
  воркером, поэтому Redis pub/sub не нужен — при выносе web API в отдельный
  процесс заменить внутренности хаба на Redis pub/sub, публичный API не меняется).
- Сбой создания уведомления никогда не пробрасывается наружу — не ломает
  платежи, мониторинг и Telegram-отправки.
- Retention: строки старше `CABINET_NOTIFICATIONS_RETENTION_DAYS` и сверх
  `CABINET_NOTIFICATIONS_MAX_PER_USER` на пользователя удаляются раз в ~6 часов
  из цикла мониторинга.

## Типы уведомлений и источники

| type | Событие | Точка в коде |
|---|---|---|
| `topup_success` | Пополнение от платёжного провайдера | `crud/transaction.py::create_transaction` (DEPOSIT + payment_method) |
| `subscription_purchased` | Покупка подписки | `crud/subscription_event.py` (event_type=purchase) |
| `subscription_renewed` | Продление (вручную) | `crud/subscription_event.py` (renewal) |
| `autopay_success` | Автопродление успешно | `crud/subscription_event.py` (renewal, source=autopay) |
| `autopay_failed` | Не хватило средств на автопродление | `monitoring_service._process_autopayments` |
| `subscription_expiring` | Подписка истекает через N дней | `monitoring_service._check_expiring_subscriptions` |
| `subscription_expired` | Подписка истекла | `monitoring_service._check_expired_subscriptions` |
| `trial_ending` | Триал заканчивается (~2ч) | `monitoring_service._check_trial_expiring_soon` |
| `referral_joined` | Новый реферал | `referral_service.process_referral_registration` |
| `referral_commission` | Начислена комиссия | `referral_service.process_referral_topup` |
| `ticket_reply` | Ответ поддержки в тикете | `handlers/admin/tickets.py::notify_user_about_ticket_reply` |
| `broadcast` | Админ-рассылка | `broadcast_service._run_broadcast` |
| `promo_offer` | Промо-предложение (зарезервировано) | — |

## Настройки (`app/config.py` / env)

```
CABINET_NOTIFICATIONS_ENABLED=true
CABINET_NOTIFICATIONS_RETENTION_DAYS=90
CABINET_NOTIFICATIONS_MAX_PER_USER=200
CABINET_NOTIFICATIONS_SSE_MAX_CONNECTIONS=5
CABINET_NOTIFICATIONS_SSE_TICKET_TTL_SECONDS=300
CABINET_NOTIFICATIONS_HEARTBEAT_SECONDS=25
```

## API-контракт

Все REST-запросы — с обычным cabinet Bearer JWT.

### `GET /cabinet/notifications?limit=50&offset=0&unreadOnly=false`

```json
{
  "items": [
    {
      "id": "ntf_123",
      "type": "topup_success",
      "title": "Баланс пополнен",
      "body": "Средства зачислены на баланс.",
      "payload": {"amountKopeks": 50000, "paymentMethod": "yookassa"},
      "isRead": false,
      "createdAt": "2026-07-09T12:34:56"
    }
  ],
  "total": 120,
  "unreadCount": 3
}
```

### Остальные REST-эндпоинты

| Эндпоинт | Ответ |
|---|---|
| `GET /cabinet/notifications/unread-count` | `{"unreadCount": 3}` |
| `POST /cabinet/notifications/{id}/read` (числовой id) | `{"success": true, "unreadCount": 2}`, 404 если чужое/нет |
| `POST /cabinet/notifications/read-all` | `{"success": true, "marked": 3}` |
| `POST /cabinet/notifications/stream-token` | `{"token": "<sse-jwt>", "expiresIn": 300}` |

### `GET /cabinet/notifications/stream?ticket=<sse-jwt>&since=<lastNumericId>`

SSE-стрим (`text/event-stream`). Auth — короткоживущий тикет в query
(`EventSource` не умеет Authorization-заголовок). `since` (или заголовок
`Last-Event-ID`) — backfill пропущенных событий (до 50). Heartbeat-комментарий
`: ping` каждые 25 секунд. Формат события:

```
id: 124
event: notification
data: {"id":"ntf_124","type":"ticket_reply","title":"Ответ поддержки","body":"...","payload":{"ticketId":7,"preview":"..."},"isRead":false,"createdAt":"2026-07-09T12:40:00"}
```

## Интеграция на фронте (LetoVPNSite)

1. При загрузке кабинета: `GET /cabinet/notifications?limit=20` → бейдж
   колокольчика из `unreadCount`, лента из `items`.
2. `POST /cabinet/notifications/stream-token`, затем:
   ```js
   const es = new EventSource(`${API}/cabinet/notifications/stream?ticket=${token}&since=${lastNumericId}`);
   es.addEventListener("notification", (e) => {
     const n = JSON.parse(e.data);
     prependToFeed(n);
     badge++;
     toast(n);
     lastNumericId = parseInt(n.id.slice(4), 10);
   });
   ```
3. `es.onerror`: закрыть EventSource, подождать 2–5 с (джиттер), **заново
   запросить тикет** (TTL 5 минут — нативный auto-reconnect не переживёт его),
   пересоздать EventSource с актуальным `since`.
4. Прочтение: `POST /cabinet/notifications/{id}/read` или `.../read-all`;
   бейдж обновлять из `unreadCount` ответа.
5. Рендер текста — по `type` + `payload` локалями фронта; `title`/`body` —
   fallback для неизвестных типов и обязательный источник для `broadcast`
   (свободный текст рассылки лежит в `body`).

## Reverse-proxy (nginx)

Для пути `/cabinet/notifications/stream`:

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_read_timeout 90s;   # heartbeat каждые 25с держит соединение
# буферизацию отключает сам бэкенд заголовком X-Accel-Buffering: no
```
