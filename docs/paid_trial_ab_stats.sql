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
