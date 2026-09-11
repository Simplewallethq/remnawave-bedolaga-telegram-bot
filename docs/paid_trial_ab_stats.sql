-- Статистика A/B «гейт за 1 ₽ вместо триала». БД бота на проде: Postgres 5454
-- (через туннель ssh -L 5555:127.0.0.1:5454 → psql -h 127.0.0.1 -p 5555 -U bedolaga_user bedolaga_bot).
-- Варианты: control — обычный триал, paid_trial — гейт. bot_id — бот регистрации (Лето / копикэты).

-- 1. Воронка по вариантам и ботам
with cohort as (
  select u.id, u.bot_id, u.trial_offer_variant as variant, u.created_at
  from users u
  where u.trial_offer_variant is not null
),
paid_gate as (
  select distinct user_id from subscription_events
  where event_type = 'purchase' and extra->>'source' = 'paid_trial_offer'
),
recurring_ok as (
  select distinct user_id from onepayment_payments
  where is_recurring and is_paid
),
converted as (
  select distinct user_id from subscription_events
  where event_type in ('purchase','renewal')
    and coalesce(extra->>'source','') <> 'paid_trial_offer'
    and coalesce(amount_kopeks,0) > 100
)
select c.variant, c.bot_id,
       count(*)                                  as registered,
       count(pg.user_id)                         as paid_1rub,
       count(r.user_id)                          as first_recurring_ok,
       count(cv.user_id)                         as any_real_payment,
       round(100.0*count(pg.user_id)/nullif(count(*),0),1)  as pct_paid_1rub,
       round(100.0*count(cv.user_id)/nullif(count(*),0),1)  as pct_converted
from cohort c
left join paid_gate pg on pg.user_id = c.id
left join recurring_ok r on r.user_id = c.id
left join converted cv on cv.user_id = c.id
group by 1,2 order by 1,2;

-- 2. Состояние привязок у когорты «за 1 ₽»
select b.status, count(*)
from onepayment_bindings b join users u on u.id = b.user_id
where u.trial_offer_variant = 'paid_trial'
group by 1;

-- 3. Последние оплаты гейта
select u.telegram_id, u.bot_id, p.amount_kopeks, p.status, p.is_paid, p.created_at
from onepayment_payments p join users u on u.id = p.user_id
where p.metadata_json->>'paid_trial' is not null
order by p.created_at desc limit 20;

-- 4. Подключение к VPN по когортам за период (Grafana «🧪 Доступ → подключение → оплата»).
--    Когорты: control — триал сразу; 1 ₽ — оплатили гейт / фоллбэк-триал через
--    TRIAL_PAID_OFFER_FALLBACK_TRIAL_MINUTES (users.paid_trial_fallback_at) / купили сами / без доступа.
--    В Grafana $__timeFilter(u.created_at) — здесь последняя неделя.
-- Сводка по когортам за период (по дате регистрации): доступ, подключение к VPN,
-- реальные оплаты (>1 ₽). Флаг подключения приходит с задержкой синка панели.
WITH c AS (
  SELECT u.id, u.has_connected_to_vpn,
         CASE
           WHEN u.trial_offer_variant = 'control' THEN '1. control: триал сразу'
           WHEN EXISTS (SELECT 1 FROM subscription_events e
                        WHERE e.user_id = u.id AND e.event_type = 'purchase'
                          AND e.extra->>'source' = 'paid_trial_offer') THEN '2. 1 ₽: оплатили гейт'
           WHEN u.paid_trial_fallback_at IS NOT NULL THEN '3. 1 ₽: фоллбэк-триал'
           WHEN EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.id) THEN '4. 1 ₽: купили сами'
           ELSE '5. 1 ₽: без доступа'
         END AS cohort
  FROM users u
  WHERE $__timeFilteru.created_at > now() - interval '7 days' AND u.trial_offer_variant IS NOT NULL
),
paid AS (
  SELECT user_id, SUM(amount_kopeks) AS amt FROM transactions
  WHERE type = 'deposit' AND is_completed AND amount_kopeks > 100
  GROUP BY 1
)
SELECT
  c.cohort                                                       AS "Когорта",
  COUNT(*)                                                       AS "Юзеров",
  COUNT(*) FILTER (WHERE c.has_connected_to_vpn)                 AS "Подключились",
  ROUND(100.0 * COUNT(*) FILTER (WHERE c.has_connected_to_vpn) / COUNT(*), 1) AS "Подключились, %",
  COUNT(p.user_id)                                               AS "Платили >1 ₽",
  ROUND(100.0 * COUNT(p.user_id) / COUNT(*), 1)                  AS "Платили, %",
  COALESCE(SUM(p.amt), 0) / 100                                  AS "Выручка, ₽"
FROM c
LEFT JOIN paid p ON p.user_id = c.id
GROUP BY 1
ORDER BY 1;

-- 5. Фоллбэк-триалы по дню выдачи: выдано / подключились к VPN.
-- Фоллбэк-триалы по моменту выдачи (users.paid_trial_fallback_at):
-- сколько выдано и сколько из них уже подключились к VPN.
SELECT
  $__timeGroupdate_trunc('day', paid_trial_fallback_at) AS "time",
  COUNT(*)                                        AS "Выдано фоллбэк-триалов",
  COUNT(*) FILTER (WHERE has_connected_to_vpn)    AS "Подключились к VPN"
FROM users
WHERE $__timeFilterpaid_trial_fallback_at > now() - interval '7 days'
  AND trial_offer_variant = 'paid_trial'
GROUP BY 1
ORDER BY 1;
