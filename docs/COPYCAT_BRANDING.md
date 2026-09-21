# Копикеты: другой бренд в том же процессе

Копикет — это бот из `mirror_bots.yaml` со своим брендом. Один процесс, одна
база, один образ: рядом с основным ботом живут витрины со своими названиями,
картинками, саппортом и нейтральным доменом ключ-ссылки. Отдельная ветка или
отдельный контейнер не нужны.

## Как завести копикет

Достаточно обычной записи в `mirror_bots.yaml` — каждый бот оттуда и есть
витрина (пример — `mirror_bots.example.yaml`):

```yaml
bots:
  - token: "234567:BBB"
    logo: "shuka.jpg"
```

Имя проекта берётся из названия бота в Telegram, саппорт и домен ключ-ссылки —
из `COPYCAT_SUPPORT_USERNAME` и `COPYCAT_SUBSCRIPTION_DOMAIN`. Любое поле
переопределяется у конкретной витрины:

```yaml
  - token: "234567:BBB"
    logo: "shuka.jpg"
    name: "Shuka"
    privacy_url: https://telegra.ph/shuka-privacy
    terms_url: https://telegra.ph/shuka-terms
    channel_link: https://t.me/shuka_channel
    channel_id: "@shuka_channel"
    support: "@shuka_support"
    subscription_domain: sub.another-domain.com
```

| Поле | Что делает | Если не задано |
|---|---|---|
| `copycat: false` | Зеркало остаётся под основным брендом | запись — витрина |
| `name` | Имя проекта: `{project_name}` в текстах, замена слова «Leto» | название бота в Telegram (`getMe`) |
| `logo` | main_pic копикета (файл в `BOT_IMAGES_DIR`) | картинка основного бота |
| `privacy_url`, `terms_url` | Юр-ссылки: `{privacy_url}`, `{terms_url}`, кнопки в «Инфо», правила при регистрации | из `PRIVACY_POLICY_URL` / `TERMS_URL` основного бота |
| `channel_link` | Блок «Подпишись на наш канал» (`{channel_link}`) | блок скрыт; канал основного бренда не наследуется |
| `channel_id` | Обязательная подписка на канал копикета (бот должен быть админом канала) | проверка подписки для копикета выключена |
| `support` | Саппорт (`@username`, `t.me/...`, URL): `{support_contact}`, кнопка поддержки | `COPYCAT_SUPPORT_USERNAME`, затем `SUPPORT_USERNAME` |
| `subscription_domain` | Хост ключ-ссылки: бот подменяет домен в `subscription_url`, путь/токен те же | `COPYCAT_SUBSCRIPTION_DOMAIN` |
| `referral_terms_url` | Ссылка «Полные условия программы» в рефералке | ссылка скрыта |
| `has_own_app` | Есть своё приложение (кнопки «Скачать <бренд>») | `false` — только Happ/Incy |
| `rays_enabled` | Лучи и магазин наград | `false` — только рефералка 50 % |

Зеркало, которое должно остаться под основным брендом, помечается
`copycat: false`. Запись без имени (ни в yaml, ни в Telegram) тоже остаётся
зеркалом. Неизвестные ключи (`image_url` и т.п.) игнорируются, legacy-формат
`MIRROR_BOTS` в `.env` проходит через тот же парсер.

Имя «Adrenalin VPN» в шаблоне «{project_name} VPN» не удваивает слово: если в
имени уже есть VPN, оно подставляется как есть.

## Что получает копикет

- **Тексты.** Все строки проходят через `Texts._get_value`: там подставляются
  `{project_name}`, `{privacy_url}`, `{terms_url}`, `{channel_link}`,
  `{support_contact}`, `{support_url}`, `{support_email}` (строка выбрасывается
  целиком, если все её плейсхолдеры пустые), а слово «Leto» в ещё не
  переведённых инлайн-дефолтах заменяется на имя бренда с сохранением
  регистра. Домены и хэндлы (`letovpn.com`, `@letovpn_bot`) не трогаются.
- **Клиенты.** Только Happ (Android/Windows) и Incy (iOS/macOS): кнопок
  «Скачать Leto» нет, у Android есть кнопка Happ, тексты Connect — нейтральные
  (`*_HAPP`, `*_NO_APP` ключи). Share-ссылки ведут в Happ/Incy.
- **Рефералка.** Только комиссия (`REFERRAL_COMMISSION_PERCENT`), без лучей и
  магазина; лучи не начисляются рефереру из копикета.
- **Юридика.** При регистрации показывается `RULES_TEXT_COPYCAT` со ссылками
  копикета, экран политики из БД пропускается, кнопка «Оферта» скрыта, кнопки
  «Политика» и «Правила» — URL копикета.
- **Поддержка.** Режим `contact` (кнопка-ссылка на саппорт копикета), тикеты
  основного бренда не показываются.
- **Ключ-ссылка.** `get_raw_subscription_link` / `get_display_subscription_link`
  переписывают хост на `subscription_domain`; Happ-криптоссылка для копикета не
  используется (она зашифрована панелью от исходного URL).
- **TV-пары** (`/start tv_...`) — только у основного бренда.

## Рассылки

Пользователь принадлежит боту (`users.bot_id`), и любой отправитель пишет ему
из этого бота (`bot_for_user`) и в его бренде (`use_brand_for_user`).

- **Базовые уведомления** (истечение/истекла подписка, триал, автоплатёж,
  1Payment) приходят всем — в бренде получателя.
- **Воронки основного бренда** (cold solo, hot invoice, win-back истёкших,
  бонус за депозит, legacy-офферы, отзыв за бонус, A/B «триал за рубль»,
  запрос оценки Android, опрос после истечения, догоняющие письма после
  истечения, напоминание об отписке от канала) копикетов не касаются:
  выборки фильтруются `not_copycat_user_clause()`, отправители дополнительно
  проверяют `is_copycat_recipient`.
- **Ручные рассылки из админки** имеют выбор ботов: «Основной бот» (по
  умолчанию), «Все витрины», «Все боты», а когда витрин немного — ещё и кнопка
  на каждую. Через web API — поле `bot_scope` (`leto` | `copycats` |
  `copycat:<bot_id>` | `all`).

## Как это устроено

- `app/branding/profile.py` — `BrandProfile`; `primary_profile()` строится из
  `settings` при каждом обращении (значения приходят из .env и из БД через
  админку); `mirror_profile_from_config()` — из записи yaml.
- `app/utils/bot_registry.py` — реестр: логотип, экземпляр бота и запись
  бренда по `bot_id`; `get_brand_for_bot`, `copycat_bot_ids`, `is_copycat_user`.
- `app/branding/context.py` — `current_brand()` из ContextVar; в хендлерах его
  выставляет `BrandContextMiddleware` (`dp.update.outer_middleware`), в фоне —
  `with use_brand_for_user(user):` вокруг рендера и отправки. ContextVar
  копируется в `create_task` снимком, поэтому скоуп ставится на месте
  отправки каждому получателю, а не вокруг цикла.
- `settings.get_support_contact_*`, `brand_has_own_app()`,
  `is_brand_channel_enabled()`, `is_rays_program_enabled_for_brand()` — обёртки
  над `current_brand()`; старые вызовы работают без правок.

## Один бренд на весь процесс (v1)

`VPN_BRAND_NAME`, `BRAND_HAS_OWN_APP`, `BRAND_RAYS_ENABLED`,
`BRAND_CHANNEL_ENABLED` в `.env` по-прежнему переименовывают основной бот
(и все его обычные зеркала). Это тот же профиль, что у копикета, только
собранный из настроек.

## Инфраструктура нейтрального домена (вне бота)

- DNS + TLS для `sub.<домен копикета>`; nginx `proxy_pass` на sub-хост панели
  Remnawave по тому же пути, `proxy_set_header Host <panel-sub-host>`.
- Happ читает конфиг из заголовков ответа панели — на прокси их подменяем
  (`proxy_hide_header` + `add_header`): `profile-title` (base64 имени
  копикета), `routing`, `announce`, `support-url`, `profile-web-page-url`.
  `subscription-userinfo` и тело подписки не трогаем.
- Панель должна принимать запросы с подменённым Host.
