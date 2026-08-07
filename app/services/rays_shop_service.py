"""Магазин Наград: каталог призов и обмен лучей.

Два вида призов:
  - подписки Leto VPN — активируются сразу (лучи списываются, подписка
    создаётся/продлевается в той же транзакции БД);
  - техника — по заявке: лучи резервируются (списываются) при оформлении,
    возвращаются при отмене, пока заявка «в обработке».
"""

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ray_prize_claim import (
    create_prize_claim,
    mark_claim_cancelled,
    mark_claim_completed,
)
from app.database.crud.rays import add_user_rays, subtract_user_rays
from app.database.crud.server_squad import get_active_server_squads
from app.database.models import (
    RayPrizeClaim,
    RayPrizeClaimStatus,
    RayTransactionType,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services.plan_pricing_service import get_plan_by_code

logger = logging.getLogger(__name__)

PRIZE_KIND_SUBSCRIPTION = "subscription"
PRIZE_KIND_PHYSICAL = "physical"


@dataclass(frozen=True)
class RayPrize:
    number: int          # № приза в каталоге (и суффикс callback'ов)
    code: str
    title: str           # короткое имя для кнопок и заявок: «Plus 6 мес»
    cost: int            # цена в лучах
    kind: str            # PRIZE_KIND_*
    catalog_title: str = ""             # полное имя в каталоге: «AirPods 4»
    plan_code: Optional[str] = None     # для подписок: код SubscriptionPlan
    period_days: Optional[int] = None   # для подписок: срок
    period_label: Optional[str] = None  # «6 месяцев» — для заголовков/успеха
    plan_title: Optional[str] = None    # «Plus» — для заголовков/успеха


RAY_PRIZES: Tuple[RayPrize, ...] = (
    RayPrize(1, "plus_6m", "Plus 6 мес", 3, PRIZE_KIND_SUBSCRIPTION,
             catalog_title="Подписка Plus • 6 месяцев",
             plan_code="plus", period_days=180, period_label="6 месяцев", plan_title="Plus"),
    RayPrize(2, "pro_1y", "Pro 1 год", 9, PRIZE_KIND_SUBSCRIPTION,
             catalog_title="Подписка Pro • 1 год",
             plan_code="pro", period_days=360, period_label="1 год", plan_title="Pro"),
    RayPrize(3, "pro_2y", "Pro 2 года", 15, PRIZE_KIND_SUBSCRIPTION,
             catalog_title="Подписка Pro • 2 года",
             plan_code="pro", period_days=720, period_label="2 года", plan_title="Pro"),
    RayPrize(4, "speaker", "Колонка", 50, PRIZE_KIND_PHYSICAL,
             catalog_title="Колонка JBL Flip 7"),
    RayPrize(5, "airpods", "AirPods", 100, PRIZE_KIND_PHYSICAL,
             catalog_title="AirPods 4"),
    RayPrize(6, "watch", "Apple Watch", 300, PRIZE_KIND_PHYSICAL,
             catalog_title="Apple Watch SE (2nd Gen, GPS 40 мм)"),
    RayPrize(7, "iphone", "iPhone", 700, PRIZE_KIND_PHYSICAL,
             catalog_title="iPhone 16 (128 GB)"),
    RayPrize(8, "macbook", "MacBook", 1000, PRIZE_KIND_PHYSICAL,
             catalog_title='MacBook Air 13" (M4, 16 GB / 256 GB)'),
)


def get_prize_by_number(number: int) -> Optional[RayPrize]:
    for prize in RAY_PRIZES:
        if prize.number == number:
            return prize
    return None


async def redeem_subscription_prize(
    db: AsyncSession,
    db_user: User,
    prize: RayPrize,
) -> Optional[Subscription]:
    """Списывает лучи и активирует призовую подписку. Всё в одной транзакции БД.

    Активная подписка того же плана продлевается на срок приза; в остальных
    случаях подписка заменяется планом приза (в точности как покупка тарифа,
    только платёж — лучами). Возвращает подписку или None (нехватка лучей /
    план недоступен).
    """
    plan = await get_plan_by_code(db, prize.plan_code)
    if plan is None or not plan.is_active:
        logger.error("План %s для приза %s не найден или неактивен", prize.plan_code, prize.code)
        return None

    # При нехватке лучей subtract_user_rays возвращает None, не тронув БД,
    # а при ошибке откатывается сам — явный rollback здесь не нужен (он бы
    # экспайрил все объекты сессии, включая db_user).
    ray_tx = await subtract_user_rays(
        db, db_user, prize.cost,
        type=RayTransactionType.SPEND,
        description=f"Приз: подписка {prize.title}",
        commit=False,
    )
    if ray_tx is None:
        return None

    try:
        now = datetime.utcnow()
        existing_sub = (
            await db.execute(
                select(Subscription).where(Subscription.user_id == db_user.id)
            )
        ).scalar_one_or_none()

        same_plan_active = (
            existing_sub is not None
            and existing_sub.plan_id == plan.id
            and not existing_sub.is_trial
            and existing_sub.status == SubscriptionStatus.ACTIVE.value
            and existing_sub.end_date
            and existing_sub.end_date > now
        )

        if same_plan_active:
            existing_sub.extend_subscription(prize.period_days)
            subscription = existing_sub
        else:
            squads = await get_active_server_squads(db)
            connected_squads = [s.squad_uuid for s in squads if getattr(s, "squad_uuid", None)]
            end_date = now + timedelta(days=prize.period_days)

            if existing_sub is not None:
                existing_sub.status = SubscriptionStatus.ACTIVE.value
                existing_sub.is_trial = False
                existing_sub.start_date = now
                existing_sub.end_date = end_date
                existing_sub.traffic_limit_gb = plan.traffic_limit_gb
                existing_sub.device_limit = plan.device_limit
                existing_sub.connected_squads = connected_squads
                existing_sub.plan_id = plan.id
                existing_sub.plan_period_days = prize.period_days
                subscription = existing_sub
            else:
                subscription = Subscription(
                    user_id=db_user.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    start_date=now,
                    end_date=end_date,
                    traffic_limit_gb=plan.traffic_limit_gb,
                    device_limit=plan.device_limit,
                    connected_squads=connected_squads,
                    autopay_enabled=False,
                    autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
                    plan_id=plan.id,
                    plan_period_days=prize.period_days,
                )
                db.add(subscription)

        db_user.has_had_paid_subscription = True

        await db.flush()
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(subscription)
    await db.refresh(db_user)

    logger.info(
        "🎁 Приз %s активирован пользователю %s: −%s ☀️, остаток %s",
        prize.code, db_user.telegram_id, prize.cost, db_user.rays_balance,
    )

    try:
        from app.services.subscription_service import SubscriptionService
        await SubscriptionService().create_remnawave_user(db, subscription)
    except Exception as e:
        logger.warning(f"Не удалось синхронизировать призовую подписку {subscription.id}: {e}")

    return subscription


async def create_physical_prize_claim(
    db: AsyncSession,
    db_user: User,
    prize: RayPrize,
    bot: Optional[Bot] = None,
    contact: Optional[str] = None,
) -> Optional[RayPrizeClaim]:
    """Резервирует лучи и создаёт заявку на технику. None — нехватка лучей.

    contact — TG-контакт из формы кабинета сайта (в боте не передаётся).
    """
    ray_tx = await subtract_user_rays(
        db, db_user, prize.cost,
        type=RayTransactionType.SPEND,
        description=f"Заявка на приз: {prize.title}",
        commit=False,
    )
    if ray_tx is None:
        return None

    try:
        claim = await create_prize_claim(
            db,
            user_id=db_user.id,
            prize_code=prize.code,
            prize_title=prize.title,
            cost_rays=prize.cost,
            spend_transaction_id=ray_tx.id,
            contact=contact,
            commit=False,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(claim)
    await db.refresh(db_user)

    logger.info(
        "📦 Заявка №%s на приз %s создана пользователем %s (резерв %s ☀️)",
        claim.id, prize.code, db_user.telegram_id, prize.cost,
    )

    if bot:
        await _notify_admins_new_claim(bot, db_user, claim)

    return claim


async def cancel_prize_claim(
    db: AsyncSession,
    claim: RayPrizeClaim,
    claim_user: User,
    *,
    cancelled_by_admin: bool = False,
) -> bool:
    """Отменяет заявку «в обработке» и возвращает зарезервированные лучи."""
    if claim.status != RayPrizeClaimStatus.PENDING.value:
        return False

    refund_tx = await add_user_rays(
        db, claim_user, claim.cost_rays,
        type=RayTransactionType.REFUND,
        description=f"Возврат лучей за отмену заявки №{claim.id} ({claim.prize_title})",
        commit=False,
        count_lifetime=False,
    )
    if refund_tx is None:
        return False

    try:
        await mark_claim_cancelled(
            db, claim, refund_transaction_id=refund_tx.id, commit=False,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(claim)
    await db.refresh(claim_user)

    logger.info(
        "↩️ Заявка №%s отменена (%s), возвращено %s ☀️ пользователю %s",
        claim.id, "админом" if cancelled_by_admin else "пользователем",
        claim.cost_rays, claim_user.telegram_id,
    )
    return True


async def complete_prize_claim(db: AsyncSession, claim: RayPrizeClaim) -> bool:
    """Помечает заявку выполненной (приз выдан). Лучи остаются списанными."""
    if claim.status != RayPrizeClaimStatus.PENDING.value:
        return False
    await mark_claim_completed(db, claim)
    logger.info("✅ Заявка №%s (%s) выполнена", claim.id, claim.prize_code)
    return True


async def _notify_admins_new_claim(bot: Bot, db_user: User, claim: RayPrizeClaim) -> None:
    """Уведомляет админ-чат о новой заявке с кнопками Выполнено / Отклонить."""
    try:
        from app.services.admin_notification_service import AdminNotificationService

        username = f"@{db_user.username}" if db_user.username else "—"
        contact_line = (
            f"📮 Контакт из кабинета: {html.escape(claim.contact)}\n"
            if claim.contact else ""
        )
        text = (
            "🎁 <b>НОВАЯ ЗАЯВКА НА ПРИЗ</b>\n\n"
            f"📦 Приз: <b>{html.escape(claim.prize_title)}</b>\n"
            f"☀️ Зарезервировано: <b>{claim.cost_rays}</b> лучей\n"
            f"🧾 Заявка: <b>№{claim.id}</b>\n\n"
            f"👤 Пользователь: {html.escape(db_user.full_name or '')}\n"
            f"🆔 Telegram ID: <code>{db_user.telegram_id}</code>\n"
            f"📱 Username: {html.escape(username)}\n"
            f"{contact_line}\n"
            "Свяжитесь с пользователем для уточнения доставки."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Выполнено",
                callback_data=f"admin_rays_claim_done_{claim.id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить (вернуть лучи)",
                callback_data=f"admin_rays_claim_reject_{claim.id}",
            ),
        ]])
        await AdminNotificationService(bot)._send_message(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Не удалось уведомить админов о заявке №{claim.id}: {e}")
