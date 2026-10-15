"""Платформа для владельцев витрин (бот OWNER_PLATFORM_BOT_ID).

Любой пользователь бота-платформы подключает своего VPN-бота: юзернейм →
токен из BotFather → картинка. Бот становится копикетом группы
OWNER_BOTS_GROUP с владельцем `owner_user_id` — дальше его поднимает и
обслуживает CopycatRuntime, как любую витрину.

«Удалить» бота — выключить запись: вебхук снимается, пользователи, подписки
и настройки остаются за Telegram id бота. Тот же владелец подключает его
снова новым токеном — запись включается со всем прежним.

Заработок: оборот — реальные пополнения пользователей его ботов (через
платёжки, без ручных и реферальных начислений), прибыль — оборот × ревшара.
Вывести можно прибыль по пополнениям старше OWNER_PAYOUT_HOLD_DAYS дней за
вычетом заявок на вывод (отклонённые не вычитаются).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Optional

from aiogram import Bot
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    BotOwner,
    CopycatBot,
    OwnerPayout,
    Transaction,
    TransactionType,
    User,
    WithdrawalStatus,
)

logger = logging.getLogger(__name__)

MODE_OWNER = "owner"
MODE_VPN = "vpn"

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_GET_ME_TIMEOUT = 10

PLAN_TITLES = {"solo": "Solo", "plus": "Plus", "pro": "Pro"}
PLUS_OVER_SOLO = Decimal("1.10")
PRO_OVER_PLUS = Decimal("1.20")

USDT_STEP = Decimal("0.01")


# ---------------------------------------------------------------- валидация


def _strip_at(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        text = text.split("t.me/", 1)[1]
    elif text.startswith("t.me/"):
        text = text[len("t.me/"):]
    return text.lstrip("@").strip()


def normalize_bot_username(text: str) -> Optional[str]:
    """Юзернейм бота без @: 5–32 символа латиницей, цифрами и _, на конце bot."""
    username = _strip_at(text)
    if not _USERNAME_RE.match(username) or not username.lower().endswith("bot"):
        return None
    return username


def normalize_support_username(text: str) -> Optional[str]:
    """Юзернейм саппорта без @: 5–32 символа латиницей, цифрами и _."""
    username = _strip_at(text)
    if not _USERNAME_RE.match(username):
        return None
    return username


def looks_like_token(text: str) -> bool:
    return bool(_TOKEN_RE.match((text or "").strip()))


def is_reserved_bot_id(bot_id: int) -> bool:
    """Основной бот и сама платформа не подключаются как витрины."""
    from app.utils.bot_registry import is_primary_bot

    return is_primary_bot(bot_id) or settings.is_owner_platform_bot(bot_id)


# ---------------------------------------------------------------- владелец


async def get_owner(db: AsyncSession, user_id: int) -> Optional[BotOwner]:
    result = await db.execute(select(BotOwner).where(BotOwner.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create_owner(db: AsyncSession, user_id: int, mode: str = MODE_OWNER) -> BotOwner:
    owner = await get_owner(db, user_id)
    if owner is None:
        owner = BotOwner(user_id=user_id, mode=mode)
        db.add(owner)
        await db.flush()
    return owner


async def set_mode(db: AsyncSession, user_id: int, mode: str) -> BotOwner:
    """Запоминает выбранный режим. Строка заводится при первом выборе любого
    режима: по ней /start понимает, что экран выбора человек уже видел."""
    owner = await get_or_create_owner(db, user_id, mode)
    owner.mode = mode
    await db.commit()
    return owner


async def list_owner_bots(db: AsyncSession, user_id: int, *, enabled_only: bool = False) -> list[CopycatBot]:
    query = select(CopycatBot).where(CopycatBot.owner_user_id == user_id).order_by(CopycatBot.id)
    if enabled_only:
        query = query.where(CopycatBot.enabled.is_(True))
    return list((await db.execute(query)).scalars().all())


async def get_owner_bot(db: AsyncSession, user_id: int, row_id: int) -> Optional[CopycatBot]:
    result = await db.execute(
        select(CopycatBot).where(CopycatBot.id == row_id, CopycatBot.owner_user_id == user_id)
    )
    return result.scalar_one_or_none()


def revshare_percent(owner: Optional[BotOwner]) -> int:
    if owner is not None and owner.revshare_percent is not None:
        return int(owner.revshare_percent)
    return int(settings.OWNER_DEFAULT_REVSHARE_PERCENT)


# ---------------------------------------------------------------- подключение


@dataclass(frozen=True)
class Occupancy:
    """Чем занят бот: free — свободен; active — подключён и работает;
    restore — выключен, это бот того же владельца; foreign — выключен, чужой."""

    status: str
    row: Optional[CopycatBot] = None


def classify(row: Optional[CopycatBot], user_id: int) -> Occupancy:
    if row is None:
        return Occupancy("free")
    if row.enabled:
        return Occupancy("active", row)
    if row.owner_user_id == user_id:
        return Occupancy("restore", row)
    return Occupancy("foreign", row)


async def occupancy_by_username(db: AsyncSession, username: str, user_id: int) -> Occupancy:
    result = await db.execute(
        select(CopycatBot)
        .where(func.lower(CopycatBot.username) == username.lower())
        .order_by(CopycatBot.enabled.desc(), CopycatBot.id.desc())
    )
    return classify(result.scalars().first(), user_id)


async def occupancy_by_bot_id(db: AsyncSession, bot_id: int, user_id: int) -> Occupancy:
    result = await db.execute(select(CopycatBot).where(CopycatBot.telegram_bot_id == bot_id))
    return classify(result.scalars().first(), user_id)


async def fetch_bot_identity(token: str):
    """getMe по токену; None — токен не работает."""
    if not looks_like_token(token):
        return None
    bot = Bot(token=token.strip())
    try:
        return await asyncio.wait_for(bot.get_me(), timeout=_GET_ME_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - любой отказ = токен не подходит
        logger.info("Токен бота владельца не прошёл getMe: %s", error)
        return None
    finally:
        await bot.session.close()


async def connect_bot(
    db: AsyncSession,
    *,
    owner_user_id: int,
    token: str,
    bot_id: int,
    username: str,
    title: str,
    logo: Optional[str],
    support: Optional[str],
) -> CopycatBot:
    """Новая витрина владельца. Telegram id, юзернейм и название — сразу из
    getMe: без названия бренда запись до первой синхронизации считалась бы
    зеркалом Leto."""
    from app.database.crud.copycat import get_or_create_copycat_group

    group = await get_or_create_copycat_group(db, settings.OWNER_BOTS_GROUP)
    row = CopycatBot(
        token=token.strip(),
        telegram_bot_id=bot_id,
        username=username,
        title=title,
        group_id=group.id,
        enabled=True,
        is_copycat=True,
        logo=logo,
        support=f"@{support}" if support else None,
        owner_user_id=owner_user_id,
    )
    db.add(row)
    await db.commit()
    return row


async def restore_bot(db: AsyncSession, row: CopycatBot, *, token: str, username: str, title: str) -> CopycatBot:
    """Тот же владелец подключает выключенного бота снова: новый токен,
    всё остальное — как было."""
    row.token = token.strip()
    row.username = username
    row.title = title
    row.enabled = True
    await db.commit()
    return row


async def disable_bot(db: AsyncSession, row: CopycatBot) -> None:
    row.enabled = False
    await db.commit()


async def refresh_runtime() -> None:
    """Поднять/погасить витрину сразу, не дожидаясь синхронизации раз в минуту."""
    from app.services.copycat_service import copycat_runtime

    try:
        await copycat_runtime.sync()
    except Exception as error:  # noqa: BLE001 - подхватит плановая синхронизация
        logger.error("Синхронизация витрин после правки владельцем упала: %s", error)


async def bot_is_working(row: CopycatBot) -> bool:
    """🟢 — токен жив и (в webhook-режиме) вебхук смотрит на нас."""
    if not row.enabled:
        return False
    bot = Bot(token=row.token)
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=_GET_ME_TIMEOUT)
        if settings.get_bot_run_mode() == "polling" or not settings.WEBHOOK_URL:
            return True
        from app.services.copycat_service import mirror_webhook_url

        info = await asyncio.wait_for(bot.get_webhook_info(), timeout=_GET_ME_TIMEOUT)
        return (info.url or "") == mirror_webhook_url(me.id)
    except Exception as error:  # noqa: BLE001 - любой отказ = не работает
        logger.info("Бот владельца #%s не отвечает: %s", row.id, error)
        return False
    finally:
        await bot.session.close()


async def save_picture(bot: Bot, file_id: str, bot_id: int, *, suffix: str = ".jpg") -> str:
    """Скачивает картинку бота в OWNER_BOT_IMAGES_DIR; возвращает путь для `logo`.

    Имя каждый раз новое: под старым путём мог закешироваться file_id."""
    directory = Path(settings.OWNER_BOT_IMAGES_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{bot_id}_{int(time.time())}{suffix}"
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=path)
    return str(path)


# ---------------------------------------------------------------- цены


def floor_monthly_rub(plan_code: str) -> int:
    """Минимальная цена тарифа за месяц для покупателей, ₽."""
    from app.services.price_scheme_service import floor_prices

    floor = floor_prices(plan_code).get(30, 0) // 100
    if plan_code == "solo":
        floor = max(floor, int(settings.OWNER_PRICE_MIN_SOLO_RUB))
    return int(floor)


def min_plus_rub(solo_rub: int) -> int:
    return int(math.ceil(Decimal(solo_rub) * PLUS_OVER_SOLO))


def min_pro_rub(plus_rub: int) -> int:
    return int(math.ceil(Decimal(plus_rub) * PRO_OVER_PLUS))


def parse_price(text: str) -> Optional[int]:
    text = (text or "").strip().replace(" ", "").replace("₽", "")
    if not text.isdigit():
        return None
    return int(text)


def price_error(plan_code: str, value: int, *, solo: Optional[int] = None, plus: Optional[int] = None) -> Optional[tuple[str, dict]]:
    """Ключ текста ошибки и подстановки, None — цена подходит."""
    floor = floor_monthly_rub(plan_code)
    if plan_code == "solo" and value < floor:
        return "OWNER_PRICE_SOLO_MIN", {"min": floor}
    if plan_code == "plus" and solo is not None and value < min_plus_rub(solo):
        return "OWNER_PRICE_PLUS_STEP", {"min": min_plus_rub(solo)}
    if plan_code == "pro" and plus is not None and value < min_pro_rub(plus):
        return "OWNER_PRICE_PRO_STEP", {"min": min_pro_rub(plus)}
    if value < floor:
        return "OWNER_PRICE_FLOOR", {"plan": PLAN_TITLES.get(plan_code, plan_code), "min": floor}
    return None


def current_monthly_prices(row: CopycatBot) -> dict[str, int]:
    """Месячные цены бота в рублях: из его схемы (своей или группы), иначе
    цены основного бота для новых."""
    from app.services.price_scheme_service import base_prices, get_scheme, scheme_prices_for_plan
    from app.utils.bot_registry import get_price_scheme_id

    scheme = get_scheme(get_price_scheme_id(row.telegram_bot_id))
    result = {}
    for code in PLAN_TITLES:
        prices = scheme_prices_for_plan(scheme, code) if scheme else base_prices(code)
        result[code] = int(prices.get(30, 0)) // 100
    return result


async def save_owner_prices(db: AsyncSession, row: CopycatBot, monthly: dict[str, int]) -> None:
    """Пишет цены в собственную схему бота (заводит её копией текущей) и
    сразу обновляет снимок цен."""
    from app.services import price_scheme_service as schemes
    from app.utils import bot_registry

    scheme_row = None
    if row.price_scheme_id is not None:
        scheme_row = await schemes.get_scheme_row(db, row.price_scheme_id)
    if scheme_row is None:
        source = schemes.get_scheme(bot_registry.get_price_scheme_id(row.telegram_bot_id))
        name = f"owner-{row.telegram_bot_id or row.id}"
        existing = await schemes.get_scheme_by_name(db, name)
        if existing is not None:
            scheme_row = existing
        else:
            scheme_row = schemes.clone_scheme(source, name, enforce_floor=True)
            db.add(scheme_row)
            await db.flush()
        row.price_scheme_id = scheme_row.id
    scheme_row.enforce_floor = True
    for code, rubles in monthly.items():
        schemes.set_tariff(scheme_row, code, prices=schemes.derive_period_prices(rubles, code))
    await db.commit()

    await schemes.reload_schemes()
    if row.telegram_bot_id is not None:
        bot_registry.set_price_scheme(row.telegram_bot_id, scheme_row.id)


# ---------------------------------------------------------------- статистика


def _yesterday_utc(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = now or datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today


@dataclass(frozen=True)
class OwnerStats:
    active_bots: int
    users_total: int
    users_yesterday: int


async def owner_stats(db: AsyncSession, user_id: int, *, now: Optional[datetime] = None) -> OwnerStats:
    bots = await list_owner_bots(db, user_id)
    bot_ids = [row.telegram_bot_id for row in bots if row.telegram_bot_id]
    active = sum(1 for row in bots if row.enabled)
    if not bot_ids:
        return OwnerStats(active, 0, 0)
    start, end = _yesterday_utc(now)
    total = await db.scalar(select(func.count(User.id)).where(User.bot_id.in_(bot_ids)))
    yesterday = await db.scalar(
        select(func.count(User.id)).where(
            User.bot_id.in_(bot_ids), User.created_at >= start, User.created_at < end
        )
    )
    return OwnerStats(active, int(total or 0), int(yesterday or 0))


# ---------------------------------------------------------------- заработок


def _turnover_query(bot_ids: list[int]):
    return (
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0))
        .join(User, User.id == Transaction.user_id)
        .where(
            User.bot_id.in_(bot_ids),
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed.is_(True),
            Transaction.payment_method.is_not(None),
            Transaction.payment_method != "manual",
            Transaction.amount_kopeks > 0,
        )
    )


def _share(kopeks: int, percent: int) -> int:
    return int(Decimal(kopeks) * Decimal(percent) / Decimal(100))


@dataclass(frozen=True)
class OwnerEarnings:
    revshare_percent: int
    turnover_total: int
    turnover_yesterday: int
    profit_total: int
    profit_yesterday: int
    payouts_kopeks: int
    available_kopeks: int


async def owner_earnings(db: AsyncSession, owner: BotOwner, *, now: Optional[datetime] = None) -> OwnerEarnings:
    now = now or datetime.utcnow()
    percent = revshare_percent(owner)
    bots = await list_owner_bots(db, owner.user_id)
    bot_ids = [row.telegram_bot_id for row in bots if row.telegram_bot_id]

    total = yesterday = matured = 0
    if bot_ids:
        start, end = _yesterday_utc(now)
        total = int(await db.scalar(_turnover_query(bot_ids)) or 0)
        yesterday = int(
            await db.scalar(
                _turnover_query(bot_ids).where(Transaction.created_at >= start, Transaction.created_at < end)
            )
            or 0
        )
        hold_until = now - timedelta(days=int(settings.OWNER_PAYOUT_HOLD_DAYS))
        matured = int(
            await db.scalar(_turnover_query(bot_ids).where(Transaction.created_at < hold_until)) or 0
        )

    payouts = int(
        await db.scalar(
            select(func.coalesce(func.sum(OwnerPayout.amount_rub_kopeks), 0)).where(
                OwnerPayout.owner_id == owner.id,
                OwnerPayout.status != WithdrawalStatus.REJECTED.value,
            )
        )
        or 0
    )
    available = max(0, _share(matured, percent) - payouts)
    return OwnerEarnings(
        revshare_percent=percent,
        turnover_total=total,
        turnover_yesterday=yesterday,
        profit_total=_share(total, percent),
        profit_yesterday=_share(yesterday, percent),
        payouts_kopeks=payouts,
        available_kopeks=available,
    )


# ---------------------------------------------------------------- вывод


@dataclass(frozen=True)
class PayoutRate:
    cbr_rate: Decimal
    rate: Decimal


async def get_payout_rate() -> Optional[PayoutRate]:
    """Курс вывода: курс ЦБ + OWNER_PAYOUT_RATE_MARKUP_PERCENT. None — курса нет."""
    from app.utils.currency_converter import currency_converter

    cbr = await currency_converter.get_cbr_usd_rate()
    if cbr is None:
        return None
    markup = Decimal(str(settings.OWNER_PAYOUT_RATE_MARKUP_PERCENT)) / Decimal(100)
    rate = (cbr * (Decimal(1) + markup)).quantize(Decimal("0.0001"))
    return PayoutRate(cbr_rate=cbr, rate=rate)


def rub_to_usdt(kopeks: int, rate: Decimal) -> Decimal:
    """Рубли в USDT по курсу вывода, вниз до центов."""
    if rate <= 0:
        return Decimal(0)
    return (Decimal(kopeks) / Decimal(100) / rate).quantize(USDT_STEP, rounding=ROUND_DOWN)


def usdt_to_rub_kopeks(amount: Decimal, rate: Decimal) -> int:
    return int((amount * rate * Decimal(100)).quantize(Decimal(1)))


def payout_fee() -> Decimal:
    return Decimal(str(settings.OWNER_PAYOUT_FEE_USDT)).quantize(USDT_STEP)


def parse_usdt(text: str) -> Optional[Decimal]:
    text = (text or "").strip().replace(",", ".").replace(" ", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        return None
    return Decimal(text).quantize(USDT_STEP)


def payout_amount_error(amount: Decimal, available_usdt: Decimal) -> Optional[str]:
    if amount > available_usdt:
        return "OWNER_PAYOUT_TOO_MUCH"
    if amount <= payout_fee():
        return "OWNER_PAYOUT_BELOW_FEE"
    return None


async def create_payout(
    db: AsyncSession,
    owner_id: int,
    amount_usdt: Decimal,
    rate: PayoutRate,
) -> Optional[OwnerPayout]:
    """Заявка на вывод. Доступное пересчитывается под блокировкой строки
    владельца — две заявки подряд не выведут больше заработанного.
    None — сумма уже больше доступного."""
    owner = (
        await db.execute(select(BotOwner).where(BotOwner.id == owner_id).with_for_update())
    ).scalar_one()
    # Отказ закрывает транзакцию коммитом, а не откатом: откат снял бы
    # загруженные объекты сессии у вызывающего, а менять тут нечего.
    if not owner.trc20_address:
        await db.commit()
        return None
    earnings = await owner_earnings(db, owner)
    available_usdt = rub_to_usdt(earnings.available_kopeks, rate.rate)
    if payout_amount_error(amount_usdt, available_usdt) is not None:
        await db.commit()
        return None
    payout = OwnerPayout(
        owner_id=owner.id,
        amount_rub_kopeks=min(usdt_to_rub_kopeks(amount_usdt, rate.rate), earnings.available_kopeks),
        amount_usdt=amount_usdt,
        fee_usdt=payout_fee(),
        cbr_rate=rate.cbr_rate,
        rate=rate.rate,
        trc20_address=owner.trc20_address,
        status=WithdrawalStatus.PENDING.value,
    )
    db.add(payout)
    await db.commit()
    return payout


async def notify_admins_new_payout(user: User, payout: OwnerPayout) -> None:
    """Уведомление в админ-чат основным ботом с кнопками «Выплачено»/«Отклонить»."""
    import html

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if not settings.BOT_TOKEN:
        return
    bot: Optional[Bot] = None
    try:
        from app.services.admin_notification_service import AdminNotificationService

        to_send = Decimal(payout.amount_usdt) - Decimal(payout.fee_usdt)
        username = f"@{user.username}" if user.username else "—"
        text = (
            "🏦 <b>ВЫВОД ЗАРАБОТКА ВЛАДЕЛЬЦА БОТОВ</b>\n\n"
            f"🧾 Заявка: <b>№{payout.id}</b>\n"
            f"💵 Отправить: <b>{to_send} USDT</b> (TRC-20)\n"
            f"   списано {payout.amount_usdt} USDT, комиссия {payout.fee_usdt} USDT\n"
            f"💰 В рублях: {payout.amount_rub_kopeks / 100:.2f} ₽ по курсу {payout.rate} "
            f"(ЦБ {payout.cbr_rate})\n"
            f"📮 Кошелёк: <code>{html.escape(payout.trc20_address)}</code>\n\n"
            f"👤 {html.escape(user.full_name or '')} {html.escape(username)}\n"
            f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n"
            "⏱ Срок: 48 ч с момента заявки."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💸 Выплачено", callback_data=f"admin_owner_payout_paid_{payout.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_owner_payout_reject_{payout.id}"),
        ]])
        bot = Bot(token=settings.BOT_TOKEN)
        await AdminNotificationService(bot)._send_message(text, reply_markup=keyboard)
    except Exception as error:  # noqa: BLE001 - заявка уже в БД
        logger.error("Не удалось уведомить админов о выводе владельца №%s: %s", payout.id, error)
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:  # pragma: no cover
                pass


async def process_payout(db: AsyncSession, payout_id: int, *, paid: bool, admin_tg_id: int) -> Optional[OwnerPayout]:
    """Админ отметил выплату или отклонил. None — заявки нет или уже обработана."""
    payout = (
        await db.execute(select(OwnerPayout).where(OwnerPayout.id == payout_id).with_for_update())
    ).scalar_one_or_none()
    if payout is None or payout.status != WithdrawalStatus.PENDING.value:
        await db.commit()
        return None
    payout.status = WithdrawalStatus.PAID.value if paid else WithdrawalStatus.REJECTED.value
    payout.processed_by = admin_tg_id
    payout.processed_at = datetime.utcnow()
    await db.commit()
    return payout


async def payout_recipient(db: AsyncSession, payout: OwnerPayout) -> Optional[User]:
    result = await db.execute(
        select(User).join(BotOwner, and_(BotOwner.user_id == User.id, BotOwner.id == payout.owner_id))
    )
    return result.scalar_one_or_none()
