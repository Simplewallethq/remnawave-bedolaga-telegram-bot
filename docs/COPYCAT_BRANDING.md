# Копикеты: другой бренд в том же процессе

Копикет — это бот из таблицы `copycat_bots` со своим брендом. Один процесс,
одна база, один образ: рядом с основным ботом живут витрины со своими
названиями, картинками, саппортом и нейтральным доменом ключ-ссылки. Отдельная
ветка или отдельный контейнер не нужны.

## Где живут копикеты

- `copycat_bots` — витрина: токен, картинка, поля бренда, группа, `enabled`,
  `is_copycat` (false — зеркало под основным брендом), веса роутера, владелец
  и его доля. `telegram_bot_id`, `username`, `title` бот заполняет сам при
  первом `getMe`.
- `copycat_groups` — группа витрин: значения бренда по умолчанию для своих
  ботов, веса роутера, доля владельца.

`mirror_bots.yaml` (или `MIRROR_BOTS` в `.env`) читается один раз — когда
таблица пуста, все записи переносятся в неё; витрины попадают в группу
`default`, зеркала (`copycat: false`) — без группы. Дальше файл игнорируется
(в логе предупреждение); новые записи из него можно дозалить
`python -m scripts.copycats import-yaml`.

Бот перечитывает таблицы раз в `COPYCAT_SYNC_INTERVAL_SECONDS` (60 с):
новая витрина поднимается и получает вебхук, выключенная (`enabled=false`)
снимает вебхук и перестаёт отвечать, правки бренда, групп и весов роутера
подхватываются без рестарта. Витрина, не поднявшаяся на старте (сеть до
api.telegram.org), повторяется при каждой синхронизации. В polling-режиме
набор ботов фиксирован до рестарта — меняются только бренд и веса.

Вебхук витрины — `<WEBHOOK_PATH>/mirror/<telegram_bot_id>`: путь не зависит от
порядка записей, поэтому выключение или удаление витрины не сдвигает чужие
вебхуки.

Выключенная витрина остаётся брендом для своих пользователей (воронки основного
бренда и рассылки «Основной бот» их по-прежнему не трогают). Удалённая
забывается целиком — её пользователи становятся пользователями основного
бренда. Поэтому витрины выключают, а не удаляют.

## Как завести копикет

```bash
python -m scripts.copycats add 234567:BBB logo=shuka.jpg            # в группу default
python -m scripts.copycats add 345678:CCC --group partners name="Shuka"
python -m scripts.copycats add 456789:DDD --mirror                  # зеркало Leto
python -m scripts.copycats set @shuka_bot support=@shuka_support channel_link=
python -m scripts.copycats disable @shuka_bot
python -m scripts.copycats list | show BOT | groups
```

Бот указывается id записи, Telegram id, `@username` или токеном. Пустое
значение (`channel_link=`) сбрасывает поле в NULL — тогда оно наследуется.
Картинку положить в `BOT_IMAGES_DIR` (`scp` в `bot_images/`).

Имя проекта берётся из названия бота в Telegram. Поле бренда берётся у
витрины, пустое — у её группы, дальше — `COPYCAT_*` настройки и основной бот:

| Поле | Что делает | Если не задано |
|---|---|---|
| `is_copycat` | false — зеркало остаётся под основным брендом | витрина |
| `name` | Имя проекта: `{project_name}` в текстах, замена слова «Leto» | название бота в Telegram (`getMe`) |
| `logo` | main_pic копикета (файл в `BOT_IMAGES_DIR`) | картинка основного бота |
| `privacy_url`, `terms_url` * | Юр-ссылки: `{privacy_url}`, `{terms_url}`, кнопки в «Инфо», правила при регистрации | `COPYCAT_PRIVACY_POLICY_URL` / `COPYCAT_TERMS_URL`, затем `PRIVACY_POLICY_URL` / `TERMS_URL` основного бота |
| `channel_link` | Блок «Подпишись на наш канал» (`{channel_link}`) | блок скрыт; канал основного бренда не наследуется |
| `channel_id` | Обязательная подписка на канал копикета (бот должен быть админом канала) | проверка подписки для копикета выключена |
| `support` * | Саппорт (`@username`, `t.me/...`, URL): `{support_contact}`, кнопка поддержки | `COPYCAT_SUPPORT_USERNAME`, затем `SUPPORT_USERNAME` |
| `subscription_domain` * | Хост ключ-ссылки: бот подменяет домен в `subscription_url`, путь/токен те же | `COPYCAT_SUBSCRIPTION_DOMAIN` |
| `happ_redirect_template` * | Редирект кнопки Happ | `COPYCAT_HAPP_REDIRECT_TEMPLATE` |
| `referral_terms_url` * | Ссылка «Полные условия программы» в рефералке | ссылка скрыта |
| `has_own_app` | Есть своё приложение (кнопки «Скачать <бренд>») | `false` — только Happ/Incy |
| `rays_enabled` | Лучи и магазин наград | `false` — только рефералка 50 % |

\* — поле можно задать и группе (`group-set NAME support=@x`).

## Группы и платёжный роутер

```bash
python -m scripts.copycats group-add partners owner_share_percent=40 support=@partners_support
python -m scripts.copycats set @shuka_bot group=partners
python -m scripts.copycats weights group:partners platega=10 wata=1
python -m scripts.copycats weights @shuka_bot wata=1     # своё у бота
python -m scripts.copycats weights @shuka_bot --clear    # снова как у группы
```

Веса роутера ищутся у витрины, затем у её группы, затем берутся глобальные
`PAYMENT_ROUTER_WEIGHT_*`. Свой словарь весов **заменяет** глобальный целиком:
шлюза нет в словаре — он этой витрине не выпадает. Остальные правила роутера
общие: шлюз должен быть настроен и включён, 1Payment — только на поверхностях
`ONEPAYMENT_ROUTER_SOURCES`, рубильник и поверхности — `PAYMENT_ROUTER_*`.

Отдельная касса витрины — AuraPay (`AURAPAY_*` в `.env`, вебхук
`{WEBHOOK_URL}/aurapay-webhook`). Её глобальный вес 0, поэтому она выпадает только
там, где задана весами: `weights @quattra_vpn_bot aurapay=1`. Сообщение об успехе и
удаление счёта идут через бота пользователя.

Чьи веса: бота, который отвечает пользователю (контекст апдейта); вне бота
(кабинет, миниапп) — бота, где пользователь зарегистрирован. В
`metadata_json.payment_router` платежа пишутся `bot_id` и `weights_source`
(`global` | `bot` | `group:<имя>`).

`owner_share_percent` — доля владельца витрины в выручке, % (остальное —
наше); у бота перебивает групповую. Пока только хранится — расчёт и выплаты
появятся вместе с ботом для владельцев витрин (`owner_user_id`).

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
  админку); `mirror_profile_from_config()` — из записи витрины.
- `app/services/copycat_service.py` — сборка записи бренда из бота и группы
  (`brand_config_for`), веса (`effective_gateway_weights`), импорт yaml и
  `CopycatRuntime`: держит живых ботов в соответствии с таблицей.
- `app/webserver/telegram.py` — `MirrorWebhookHub`: один маршрут
  `/mirror/{bot_id}` и очередь на каждую витрину.
- `scripts/copycats.py` — CLI управления.
- `app/utils/bot_registry.py` — реестр: логотип, экземпляр бота и запись
  бренда по `bot_id`, веса роутера витрины; `get_brand_for_bot`,
  `copycat_bot_ids`, `is_copycat_user`, `get_payment_weights`.
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
