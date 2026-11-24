"""Выделенный IP — допродажа к платной тарифной подписке.

Пул заранее заведён в панели: на каждый адрес свой инбаунд (вход = выход), хост
«🔒 Мой IP» и сквад с этим одним инбаундом. Бот не трогает конфиг ноды — он только
кладёт сквад слота юзеру (``Subscription.dedicated_ip_squad_uuid`` → сборка сквадов в
``app/utils/panel_squads.py``) и снимает его. Панель досылает юзера в инбаунд на лету.

Правила (Trello #141):

* продаётся только к платной тарифной подписке основного бренда;
* цена — ``DEDICATED_IP_PRICE_KOPEKS`` за 30 дней; к действующей подписке — за остаток
  дней (290 / 30 × дней, вверх до 10 ₽), при покупке/продлении тарифа — за весь период;
* IP живёт столько же, сколько подписка (``paid_until`` = ``end_date``);
* не продлили (подписка истекла или продлили без IP) — адрес держим ``DEDICATED_IP_HOLD_DAYS``
  дней (``hold``, сквад снят), потом слот возвращается в пул;
* продление выключено — IP работает до ``paid_until`` и освобождается;
* свободных слотов нет — продажа выключена (кнопки не показываются), у кого IP есть —
  продлевают как обычно; админам уведомление о заканчивающемся пуле.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import DedicatedIpSlot, Subscription, SubscriptionStatus, TransactionType, User

logger = logging.getLogger(__name__)

SOURCE_BOT = "bot"
SOURCE_SUPPORT = "ip_support"
SOURCE_TARIFF = "tariff_checkout"
SOURCE_AUTOPAY = "autopay"

_CHOICE_KEY = "dedicated_ip_choice:{user_id}"
_CHOICE_TTL = 3600
_SOURCE_KEY = "dedicated_ip_source:{user_id}"
_SOURCE_TTL = 86400

# Остаток пула, о котором уже предупредили админов (чтобы не слать на каждый цикл).
_last_stock_alert: Optional[int] = None


class DedicatedIpError(Exception):
    pass


class NoFreeSlotError(DedicatedIpError):
    pass


class InsufficientBalanceError(DedicatedIpError):
    pass


# ---------------------------------------------------------------- цены

def price_per_month_kopeks() -> int:
    return int(settings.DEDICATED_IP_PRICE_KOPEKS)


def price_for_period_kopeks(period_days: int) -> int:
    """Цена за купленный период тарифа: 1 мес +290, 3 мес +870 и т.д."""
    if period_days <= 0:
        return 0
    return int(round(price_per_month_kopeks() * period_days / 30))


def _ceil_to_10_rub(kopeks: float) -> int:
    return int(math.ceil(kopeks / 1000.0) * 1000)


def remaining_days(end_date: Optional[datetime], now: Optional[datetime] = None) -> int:
    if end_date is None:
        return 0
    now = now or datetime.utcnow()
    seconds = (end_date - now).total_seconds()
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 86400))


def price_for_remaining_kopeks(end_date: Optional[datetime], now: Optional[datetime] = None) -> int:
    """290 / 30 × дней до конца подписки, вверх до 10 ₽."""
    days = remaining_days(end_date, now)
    if days <= 0:
        return 0
    return _ceil_to_10_rub(price_per_month_kopeks() / 30 * days)


def format_rub(kopeks: int) -> str:
    rub = int(kopeks) // 100
    rest = int(kopeks) % 100
    return f"{rub} ₽" if rest == 0 else f"{rub},{rest:02d} ₽"


def country_label() -> str:
    return settings.DEDICATED_IP_COUNTRY


# ---------------------------------------------------------------- доступность

def is_feature_enabled_for(user: Optional[User]) -> bool:
    """Фича включена и юзер из основного бренда (не копикет)."""
    if not settings.DEDICATED_IP_ENABLED or user is None:
        return False
    from app.utils.bot_registry import is_copycat_user

    if is_copycat_user(user):
        return False
    if settings.DEDICATED_IP_ADMINS_ONLY and not settings.is_admin(getattr(user, "telegram_id", 0)):
        return False
    return True


def subscription_is_eligible(subscription: Optional[Subscription], now: Optional[datetime] = None) -> bool:
    """Платная тарифная подписка, действующая сейчас."""
    if subscription is None:
        return False
    now = now or datetime.utcnow()
    if getattr(subscription, "plan_id", None) is None:
        return False
    if getattr(subscription, "is_trial", False) or getattr(subscription, "is_paid_trial", False):
        return False
    if getattr(subscription, "is_partner", False):
        return False
    if subscription.status != SubscriptionStatus.ACTIVE.value:
        return False
    return bool(subscription.end_date and subscription.end_date > now)


def subscription_can_take_ip(subscription: Optional[Subscription]) -> bool:
    """Тарифная платная подписка (возможно, ещё не купленная/истёкшая) — для экрана оплаты тарифа."""
    if subscription is None:
        return True
    return not getattr(subscription, "is_partner", False)


# ---------------------------------------------------------------- пул

async def refresh_pool_cache(db: AsyncSession) -> int:
    from app.utils.panel_squads import set_dedicated_ip_pool_squads

    result = await db.execute(select(DedicatedIpSlot.squad_uuid))
    squads = [row[0] for row in result.all()]
    set_dedicated_ip_pool_squads(squads)
    return len(squads)


async def count_free_slots(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(DedicatedIpSlot.id)).where(DedicatedIpSlot.status == DedicatedIpSlot.STATUS_FREE)
    )
    return int(result.scalar() or 0)


async def pool_stats(db: AsyncSession) -> dict:
    result = await db.execute(
        select(DedicatedIpSlot.status, func.count(DedicatedIpSlot.id)).group_by(DedicatedIpSlot.status)
    )
    stats = {row[0]: int(row[1]) for row in result.all()}
    stats["total"] = sum(stats.values())
    return stats


async def get_user_slot(db: AsyncSession, user_id: int) -> Optional[DedicatedIpSlot]:
    """Слот юзера: оплаченный или удерживаемый."""
    result = await db.execute(
        select(DedicatedIpSlot)
        .where(
            and_(
                DedicatedIpSlot.user_id == user_id,
                DedicatedIpSlot.status.in_([DedicatedIpSlot.STATUS_ACTIVE, DedicatedIpSlot.STATUS_HOLD]),
            )
        )
        .order_by(DedicatedIpSlot.status.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def is_on_sale(db: AsyncSession, user: User) -> bool:
    """Показывать ли покупку: фича включена и есть свободный слот (или у юзера свой)."""
    if not is_feature_enabled_for(user):
        return False
    if await get_user_slot(db, user.id) is not None:
        return True
    return await count_free_slots(db) > 0


async def _pick_free_slot(db: AsyncSession) -> Optional[DedicatedIpSlot]:
    """Свободный слот, освобождённый раньше всех: свежий адрес не уходит сразу другому."""
    stmt = (
        select(DedicatedIpSlot)
        .where(DedicatedIpSlot.status == DedicatedIpSlot.STATUS_FREE)
        .order_by(DedicatedIpSlot.released_at.asc().nullsfirst(), DedicatedIpSlot.slot_no.asc())
        .limit(1)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------- выбор на счёте

async def get_invoice_choice(user: User, slot: Optional[DedicatedIpSlot]) -> bool:
    """Положение кнопки IP на счёте тарифа: сохранённый выбор, иначе — по состоянию слота."""
    try:
        from app.services.user_cart_service import user_cart_service

        raw = await user_cart_service.redis_client.get(_CHOICE_KEY.format(user_id=user.id))
        if raw is not None:
            return (raw.decode() if isinstance(raw, bytes) else str(raw)) == "1"
    except Exception as error:  # pragma: no cover - Redis недоступен
        logger.debug("dedicated_ip: не прочитан выбор на счёте %s: %s", user.id, error)
    return default_invoice_choice(slot)


def default_invoice_choice(slot: Optional[DedicatedIpSlot]) -> bool:
    """IP есть и продление включено — «Убрать» (IP в счёте); иначе — «Добавить»."""
    return bool(slot is not None and slot.autorenew)


async def set_invoice_choice(user: User, value: bool) -> None:
    try:
        from app.services.user_cart_service import user_cart_service

        await user_cart_service.redis_client.setex(
            _CHOICE_KEY.format(user_id=user.id), _CHOICE_TTL, "1" if value else "0"
        )
    except Exception as error:  # pragma: no cover
        logger.warning("dedicated_ip: не сохранён выбор на счёте %s: %s", user.id, error)


async def clear_invoice_choice(user_id: int) -> None:
    try:
        from app.services.user_cart_service import user_cart_service

        await user_cart_service.redis_client.delete(_CHOICE_KEY.format(user_id=user_id))
    except Exception:  # pragma: no cover
        pass


async def remember_source(user_id: int, source: str) -> None:
    """Метка источника (диплинк ip_support) — пишется в слот при покупке."""
    try:
        from app.services.user_cart_service import user_cart_service

        await user_cart_service.redis_client.setex(_SOURCE_KEY.format(user_id=user_id), _SOURCE_TTL, source)
    except Exception:  # pragma: no cover
        pass


async def _pop_source(user_id: int, default: Optional[str]) -> Optional[str]:
    try:
        from app.services.user_cart_service import user_cart_service

        key = _SOURCE_KEY.format(user_id=user_id)
        raw = await user_cart_service.redis_client.get(key)
        if raw is not None:
            await user_cart_service.redis_client.delete(key)
            return raw.decode() if isinstance(raw, bytes) else str(raw)
    except Exception:  # pragma: no cover
        pass
    return default


async def invoice_addon(db: AsyncSession, user: User, period_days: int) -> dict:
    """Что показать на счёте тарифа: {"available", "selected", "price_kopeks"}.

    available=False — кнопки нет (фича выключена, копикет, пул пуст и своего IP нет).
    """
    if not is_feature_enabled_for(user):
        return {"available": False, "selected": False, "price_kopeks": 0}
    if not subscription_can_take_ip(await _user_subscription(db, user)):
        return {"available": False, "selected": False, "price_kopeks": 0}
    slot = await get_user_slot(db, user.id)
    if slot is None and await count_free_slots(db) <= 0:
        return {"available": False, "selected": False, "price_kopeks": 0}
    selected = await get_invoice_choice(user, slot)
    return {
        "available": True,
        "selected": selected,
        "price_kopeks": price_for_period_kopeks(period_days),
    }


# ---------------------------------------------------------------- выдача / продление

async def _sync_panel(db: AsyncSession, subscription: Subscription) -> None:
    try:
        from app.services.subscription_service import SubscriptionService

        await SubscriptionService().update_remnawave_user(db, subscription)
    except Exception as error:
        logger.warning("dedicated_ip: не удалось синхронизировать подписку %s с панелью: %s", subscription.id, error)


async def _user_subscription(db: AsyncSession, user: User) -> Optional[Subscription]:
    """user.subscription без ленивой загрузки (в async она падает с MissingGreenlet)."""
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(user, raiseerr=False)
    if state is not None and "subscription" not in state.unloaded:
        return user.subscription
    return await _load_subscription(db, user.id)


async def _load_subscription(db: AsyncSession, user_id: int) -> Optional[Subscription]:
    result = await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def charge_and_apply(
    db: AsyncSession,
    user: User,
    subscription: Subscription,
    *,
    price_kopeks: int,
    source: Optional[str] = None,
    bot=None,
    sync_panel: bool = True,
) -> DedicatedIpSlot:
    """Списывает ``price_kopeks`` с баланса и выдаёт/продлевает IP до конца подписки.

    Тот же адрес, если у юзера есть слот (оплаченный или удерживаемый), иначе — новый
    из пула. Бросает NoFreeSlotError / InsufficientBalanceError, ничего не списав.
    """
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance

    slot = await get_user_slot(db, user.id)
    is_new = slot is None
    if is_new:
        slot = await _pick_free_slot(db)
        if slot is None:
            raise NoFreeSlotError()

    if price_kopeks > 0 and user.balance_kopeks < price_kopeks:
        raise InsufficientBalanceError()

    paid_until = subscription.end_date
    description = f"Выделенный IP до {paid_until.strftime('%d.%m.%Y')}"
    if price_kopeks > 0:
        ok = await subtract_user_balance(
            db, user, price_kopeks, description=description, create_transaction=False, commit=False
        )
        if not ok:
            await db.rollback()
            raise InsufficientBalanceError()

    now = datetime.utcnow()
    try:
        if is_new:
            slot.user_id = user.id
            slot.assigned_at = now
            # Метка диплинка поддержки важнее пути оплаты: считаем продажи из поддержки.
            slot.source = await _pop_source(user.id, None) or source or SOURCE_BOT
        slot.status = DedicatedIpSlot.STATUS_ACTIVE
        slot.paid_until = paid_until
        slot.hold_until = None
        slot.autorenew = True
        subscription.dedicated_ip_squad_uuid = slot.squad_uuid
        await db.flush()
        if price_kopeks > 0:
            await create_transaction(
                db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_kopeks,
                description=description,
            )
        else:
            await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(slot)
    await db.refresh(subscription)
    logger.info(
        "🔒 Выделенный IP %s (слот %s) %s юзеру %s до %s, списано %s",
        slot.ip, slot.slot_no, "выдан" if is_new else "продлён", user.telegram_id, paid_until, price_kopeks,
    )
    if sync_panel:
        await _sync_panel(db, subscription)
    await clear_invoice_choice(user.id)
    if is_new:
        await notify_low_stock_if_needed(db, bot)
    return slot


async def apply_after_tariff(
    db: AsyncSession,
    user: User,
    subscription: Subscription,
    period_days: int,
    *,
    price_kopeks: Optional[int] = None,
    source: str = SOURCE_TARIFF,
    bot=None,
) -> Optional[DedicatedIpSlot]:
    """IP из состава оплаченного счёта тарифа — после успешной покупки/продления.

    Не бросает: нет слота или не хватило денег — деньги остаются на балансе,
    юзеру и админам уходит сообщение.
    """
    price = price_for_period_kopeks(period_days) if price_kopeks is None else int(price_kopeks)
    try:
        await db.refresh(user)
        return await charge_and_apply(db, user, subscription, price_kopeks=price, source=source, bot=bot)
    except NoFreeSlotError:
        logger.warning("🔒 Выделенный IP не выдан юзеру %s: пул пуст, %s коп. на балансе", user.telegram_id, price)
        await _notify_user(bot, user, "DEDICATED_IP_SOLD_OUT_AFTER_PAYMENT", (
            "😔 Свободные выделенные IP закончились, пока шла оплата.\n\n"
            "Подписка оформлена, а {amount} за IP остались у тебя на балансе."
        ), amount=format_rub(price))
        await notify_low_stock_if_needed(db, bot, force=True)
    except InsufficientBalanceError:
        logger.warning("🔒 Выделенный IP не выдан юзеру %s: не хватило баланса на %s", user.telegram_id, price)
    except Exception as error:
        logger.error("🔒 Ошибка выдачи выделенного IP юзеру %s: %s", user.telegram_id, error, exc_info=True)
    return None


async def renewal_addon_kopeks(db: AsyncSession, subscription: Subscription, period_days: int) -> int:
    """Сколько добавить к автопродлению: IP оплачен и продление включено."""
    if not settings.DEDICATED_IP_ENABLED:
        return 0
    slot = await get_user_slot(db, subscription.user_id)
    if slot is None or slot.status != DedicatedIpSlot.STATUS_ACTIVE or not slot.autorenew:
        return 0
    return price_for_period_kopeks(period_days)


async def set_autorenew(db: AsyncSession, slot: DedicatedIpSlot, value: bool) -> DedicatedIpSlot:
    slot.autorenew = bool(value)
    await db.commit()
    await db.refresh(slot)
    if slot.user_id:
        await clear_invoice_choice(slot.user_id)
    return slot


async def release_slot(db: AsyncSession, slot: DedicatedIpSlot, *, reason: str, sync_panel: bool = True) -> None:
    user_id = slot.user_id
    slot.status = DedicatedIpSlot.STATUS_FREE
    slot.user_id = None
    slot.paid_until = None
    slot.hold_until = None
    slot.autorenew = True
    slot.source = None
    slot.assigned_at = None
    slot.released_at = datetime.utcnow()
    subscription = await _load_subscription(db, user_id) if user_id else None
    if subscription is not None and subscription.dedicated_ip_squad_uuid == slot.squad_uuid:
        subscription.dedicated_ip_squad_uuid = None
    await db.commit()
    logger.info("🔒 Слот %s (%s) освобождён: %s (юзер %s)", slot.slot_no, slot.ip, reason, user_id)
    if subscription is not None and sync_panel:
        await _sync_panel(db, subscription)


async def put_on_hold(db: AsyncSession, slot: DedicatedIpSlot, *, now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    slot.status = DedicatedIpSlot.STATUS_HOLD
    slot.hold_until = now + timedelta(days=int(settings.DEDICATED_IP_HOLD_DAYS))
    subscription = await _load_subscription(db, slot.user_id) if slot.user_id else None
    if subscription is not None and subscription.dedicated_ip_squad_uuid == slot.squad_uuid:
        subscription.dedicated_ip_squad_uuid = None
    await db.commit()
    logger.info("🔒 Слот %s (%s) удерживается за юзером %s до %s", slot.slot_no, slot.ip, slot.user_id, slot.hold_until)
    if subscription is not None:
        await _sync_panel(db, subscription)


async def process_expirations(db: AsyncSession, bot=None) -> dict:
    """Шаг цикла мониторинга: оплата IP кончилась → hold или пул; hold кончился → пул."""
    now = datetime.utcnow()
    counters = {"hold": 0, "released": 0}

    result = await db.execute(
        select(DedicatedIpSlot).where(
            and_(
                DedicatedIpSlot.status == DedicatedIpSlot.STATUS_ACTIVE,
                DedicatedIpSlot.paid_until <= now,
            )
        )
    )
    for slot in result.scalars().all():
        user = await _load_user(db, slot.user_id)
        if slot.autorenew:
            await put_on_hold(db, slot, now=now)
            counters["hold"] += 1
            await _notify_user(bot, user, "DEDICATED_IP_ON_HOLD", (
                "🔒 Выделенный IP {ip} не продлён и отключён.\n\n"
                "Держим адрес за тобой до {date}: продли подписку с IP или подключи IP "
                "в «Управление подпиской → 🔒 Выделенный IP» — вернётся тот же адрес."
            ), ip=slot.ip, date=_fmt_date(slot.hold_until))
        else:
            ip = slot.ip
            await release_slot(db, slot, reason="срок оплаты истёк, продление выключено")
            counters["released"] += 1
            await _notify_user(bot, user, "DEDICATED_IP_EXPIRED", (
                "🔒 Срок выделенного IP {ip} закончился, адрес отключён.\n\n"
                "Подключить новый можно в «Управление подпиской → 🔒 Выделенный IP»."
            ), ip=ip)

    result = await db.execute(
        select(DedicatedIpSlot).where(
            and_(
                DedicatedIpSlot.status == DedicatedIpSlot.STATUS_HOLD,
                DedicatedIpSlot.hold_until <= now,
            )
        )
    )
    for slot in result.scalars().all():
        await release_slot(db, slot, reason="удержание истекло")
        counters["released"] += 1

    if counters["hold"] or counters["released"]:
        logger.info("🔒 Выделенные IP: в удержание %s, в пул %s", counters["hold"], counters["released"])
    return counters


async def resync_subscription_squad(db: AsyncSession, subscription: Subscription) -> bool:
    """Сверяет копию сквада в подписке со слотом (на случай ручных правок в базе)."""
    slot = await get_user_slot(db, subscription.user_id)
    expected = slot.squad_uuid if slot is not None and slot.status == DedicatedIpSlot.STATUS_ACTIVE else None
    if subscription.dedicated_ip_squad_uuid != expected:
        subscription.dedicated_ip_squad_uuid = expected
        await db.commit()
        return True
    return False


async def seed_slots(db: AsyncSession, slots: Iterable[dict]) -> dict:
    """Добавляет слоты в пул (новые — free); существующим обновляет host_uuid. Ничего не удаляет."""
    created = updated = 0
    for item in slots:
        slot_no = int(item["slot_no"])
        result = await db.execute(select(DedicatedIpSlot).where(DedicatedIpSlot.slot_no == slot_no))
        slot = result.scalar_one_or_none()
        if slot is None:
            db.add(
                DedicatedIpSlot(
                    slot_no=slot_no,
                    ip=str(item["ip"]),
                    squad_uuid=str(item["squad_uuid"]),
                    host_uuid=item.get("host_uuid"),
                    status=DedicatedIpSlot.STATUS_DISABLED if item.get("disabled") else DedicatedIpSlot.STATUS_FREE,
                )
            )
            created += 1
        else:
            if slot.ip != str(item["ip"]) or slot.squad_uuid != str(item["squad_uuid"]):
                raise DedicatedIpError(f"слот {slot_no}: ip/сквад не совпадают с базой ({slot.ip})")
            if item.get("host_uuid") and slot.host_uuid != item.get("host_uuid"):
                slot.host_uuid = item.get("host_uuid")
                updated += 1
    await db.commit()
    await refresh_pool_cache(db)
    return {"created": created, "updated": updated}


# ---------------------------------------------------------------- уведомления

def _fmt_date(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    from app.utils.timezone import format_local_datetime

    return format_local_datetime(value, "%d.%m.%Y")


async def _load_user(db: AsyncSession, user_id: Optional[int]) -> Optional[User]:
    if not user_id:
        return None
    from app.database.crud.user import get_user_by_id

    return await get_user_by_id(db, user_id)


async def _notify_user(bot, user: Optional[User], key: str, default: str, **fmt) -> None:
    if bot is None or user is None or not getattr(user, "telegram_id", None):
        return
    try:
        from app.branding.context import use_brand_for_user
        from app.localization.texts import get_texts
        from app.utils.bot_registry import bot_for_user

        target_bot = bot_for_user(user, bot)
        with use_brand_for_user(user):
            texts = get_texts(getattr(user, "language", "ru"))
            await target_bot.send_message(user.telegram_id, texts.t(key, default).format(**fmt), parse_mode="HTML")
    except Exception as error:
        logger.warning("dedicated_ip: не отправлено уведомление %s юзеру %s: %s", key, getattr(user, "telegram_id", None), error)


async def notify_low_stock_if_needed(db: AsyncSession, bot=None, *, force: bool = False) -> None:
    """Админам: свободных слотов ≤ порога (по разу на каждое значение) или пул пуст."""
    global _last_stock_alert
    if bot is None:
        return
    free = await count_free_slots(db)
    threshold = int(settings.DEDICATED_IP_LOW_STOCK_THRESHOLD)
    if free > threshold and not force:
        _last_stock_alert = None
        return
    if _last_stock_alert == free and not force:
        return
    _last_stock_alert = free
    stats = await pool_stats(db)
    if free <= 0:
        text = (
            "🔒 <b>Выделенные IP закончились</b>\n\n"
            f"Свободных: 0 из {stats.get('total', 0)} — продажа выключена.\n"
            "Нужно зарезервировать новые адреса (панель + scripts/seed_dedicated_ips.py)."
        )
    else:
        text = (
            "🔒 <b>Выделенные IP заканчиваются</b>\n\n"
            f"Свободных: {free} из {stats.get('total', 0)} "
            f"(занято {stats.get(DedicatedIpSlot.STATUS_ACTIVE, 0)}, "
            f"в удержании {stats.get(DedicatedIpSlot.STATUS_HOLD, 0)})."
        )
    try:
        from app.services.admin_notification_service import AdminNotificationService

        await AdminNotificationService(bot)._send_message(text)
    except Exception as error:
        logger.warning("dedicated_ip: не отправлено уведомление админам: %s", error)


def slot_squads(slots: Iterable[DedicatedIpSlot]) -> List[str]:
    return [slot.squad_uuid for slot in slots]
