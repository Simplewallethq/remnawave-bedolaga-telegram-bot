"""Tariff selection, purchase, upgrade and renewal flow for the tiered subscription system
(App / Solo / Plus / Pro). Legacy à-la-carte purchases remain in `purchase.py`.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F, types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import get_active_server_squads
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import (
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    TransactionType,
    User,
)
from app.handlers.subscription.notifications import (
    send_extension_notification,
    send_purchase_notification,
)
from app.keyboards.inline import (
    get_payment_methods_keyboard,
    get_insufficient_balance_keyboard,
    get_renew_periods_keyboard,
    get_tariff_periods_keyboard,
    get_tariff_upgrade_keyboard,
    get_tariffs_keyboard,
)
from app.localization.texts import get_texts
from app.utils.photo_message import edit_or_answer_photo
from app.services.plan_pricing_service import (
    SUPPORTED_PERIOD_DAYS,
    calculate_upgrade_delta,
    get_current_plan_price_for_period,
    get_lowest_monthly_price,
    get_plan_by_code,
    get_plan_by_id,
    get_plan_price,
    list_active_plans,
    resolve_pricing_cohort,
    select_prices_for_cohort,
)
from app.services.cold_solo_offer_service import cold_solo_offer_service
from app.services.hot_invoice_offer_service import hot_invoice_offer_service
from app.services.expired_subscription_offer_service import expired_subscription_offer_service
from app.services.legacy_pro_offer_service import legacy_pro_offer_service
from app.services.subscription_service import SubscriptionService
from app.utils.success_notifications import (
    build_success_management_keyboard,
    format_subscription_purchase_success,
    format_subscription_renewal_success,
    subscription_plan_name,
)

logger = logging.getLogger(__name__)

PAY_IMAGE_PATH = os.path.join("images", "pay.jpg")


_PERIOD_LABEL_KEYS = {
    30: ("PLAN_PERIOD_LABEL_1M", "1 мес"),
    90: ("PLAN_PERIOD_LABEL_3M", "3 мес"),
    180: ("PLAN_PERIOD_LABEL_6M", "6 мес"),
    360: ("PLAN_PERIOD_LABEL_1Y", "1 год"),
    720: ("PLAN_PERIOD_LABEL_2Y", "2 года"),
}


def _format_rub_short(amount_kopeks: int) -> str:
    return f"{int(round(amount_kopeks / 100))}₽"


def _period_label(period_days: int, texts) -> str:
    key, fallback = _PERIOD_LABEL_KEYS.get(period_days, ("", f"{period_days} дн."))
    return texts.t(key, fallback) if key else fallback


async def _resolve_active_subscription(db_user: User) -> Optional[Subscription]:
    """Return user's subscription only when it's an active, non-expired tiered plan."""
    sub = db_user.subscription
    if not sub or sub.plan_id is None:
        return None
    if sub.end_date <= datetime.utcnow():
        return None
    if sub.status not in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value):
        return None
    return sub


async def _resolve_renewable_subscription(db_user: User) -> Optional[Subscription]:
    active_sub = await _resolve_active_subscription(db_user)
    if active_sub is not None:
        return active_sub

    sub = db_user.subscription
    if not sub or sub.plan_id is None:
        return None
    if sub.is_trial or sub.is_partner:
        return None
    if sub.status == SubscriptionStatus.EXPIRED.value or sub.end_date <= datetime.utcnow():
        return sub
    return None


async def _save_tariff_intent_cart(
    db_user: User,
    *,
    tariff_op: str,
    plan: SubscriptionPlan,
    period_days: int,
    total_price: int,
    offer_id: Optional[int] = None,
    price_override_kopeks: Optional[int] = None,
    offer_type: Optional[str] = None,
    base_price_kopeks: Optional[int] = None,
) -> None:
    """Persist an intent cart so the tariff purchase auto-completes after a balance top-up.

    Mirrors the legacy à-la-carte intent flow: any successful top-up triggers
    auto_purchase_saved_cart_after_topup(), which reads this cart and finalizes the buy.
    tariff_op is one of: purchase | renew | upgrade.
    """
    from app.services.user_cart_service import user_cart_service
    from app.services.tariff_partial_payment_service import build_partial_breakdown

    cart_data = {
        "cart_mode": "tariff",
        "tariff_op": tariff_op,
        "plan_id": plan.id,
        "plan_code": plan.code,
        "period_days": period_days,
        "total_price": total_price,
        "intent": True,
    }
    if offer_id is not None and price_override_kopeks is not None:
        cart_data.update(
            {
                "offer_id": offer_id,
                "offer_type": offer_type or cold_solo_offer_service.NOTIFICATION_TYPE,
                "price_override_kopeks": price_override_kopeks,
            }
        )
    if tariff_op == "purchase":
        partial = build_partial_breakdown(
            db_user.balance_kopeks, total_price, base_price_kopeks
        )
        if partial:
            cart_data["partial_payment"] = partial

    try:
        await user_cart_service.save_user_cart(db_user.id, cart_data, ttl=3600)
        logger.info(
            "Сохранён intent тарифа для пользователя %s: %s %s/%sд",
            db_user.telegram_id, tariff_op, plan.code, period_days,
        )
    except Exception as e:
        logger.warning(
            "Не удалось сохранить intent тарифа для %s: %s", db_user.telegram_id, e
        )


async def _get_fixed_price_tariff_offer(
    db: AsyncSession,
    db_user: User,
    *,
    plan_code: str,
    period_days: int,
) -> tuple[Optional[int], Optional[object], Optional[str]]:
    offer_price, offer = await cold_solo_offer_service.get_price_override(
        db,
        db_user.id,
        plan_code=plan_code,
        period_days=period_days,
    )
    if offer_price is not None and offer is not None:
        return offer_price, offer, cold_solo_offer_service.NOTIFICATION_TYPE

    offer_price, offer = await legacy_pro_offer_service.get_price_override(
        db,
        db_user.id,
        plan_code=plan_code,
        period_days=period_days,
    )
    if offer_price is not None and offer is not None:
        return offer_price, offer, getattr(offer, "notification_type", None)

    return None, None, None


async def _mark_fixed_price_tariff_offer_claimed(db: AsyncSession, offer: Optional[object]) -> None:
    notification_type = getattr(offer, "notification_type", None)
    if notification_type in legacy_pro_offer_service.NOTIFICATION_TYPES:
        await legacy_pro_offer_service.mark_claimed_after_purchase(db, offer)
        return
    await cold_solo_offer_service.mark_claimed_after_purchase(db, offer)


async def _get_hot_invoice_tariff_offer(
    db: AsyncSession,
    db_user: User,
    *,
    plan_code: str,
    period_days: int,
    base_price_kopeks: Optional[int],
    activate: bool = False,
) -> tuple[Optional[int], Optional[object], Optional[str]]:
    offer_price, offer = await hot_invoice_offer_service.get_price_override(
        db,
        db_user.id,
        plan_code=plan_code,
        period_days=period_days,
        base_price_kopeks=base_price_kopeks,
        activate=activate,
    )
    if offer_price is None or offer is None:
        return None, None, None
    return offer_price, offer, hot_invoice_offer_service.NOTIFICATION_TYPE


async def _get_expired_subscription_tariff_offer(
    db: AsyncSession,
    db_user: User,
    *,
    plan_code: str,
    period_days: int,
    base_price_kopeks: Optional[int],
    activate: bool = False,
) -> tuple[Optional[int], Optional[object], Optional[str]]:
    offer_price, offer = await expired_subscription_offer_service.get_price_override(
        db,
        db_user.id,
        plan_code=plan_code,
        period_days=period_days,
        base_price_kopeks=base_price_kopeks,
        activate=activate,
    )
    if offer_price is None or offer is None:
        return None, None, None
    return offer_price, offer, expired_subscription_offer_service.NOTIFICATION_TYPE


async def _mark_tariff_offer_claimed(
    db: AsyncSession,
    offer: Optional[object],
    *,
    plan_code: str,
    period_days: int,
    base_price_kopeks: int,
    price_kopeks: int,
) -> None:
    if getattr(offer, "notification_type", None) == hot_invoice_offer_service.NOTIFICATION_TYPE:
        await hot_invoice_offer_service.mark_claimed_after_purchase(
            db,
            offer,
            plan_code=plan_code,
            period_days=period_days,
            base_price_kopeks=base_price_kopeks,
            price_kopeks=price_kopeks,
        )
        return
    if getattr(offer, "notification_type", None) == expired_subscription_offer_service.NOTIFICATION_TYPE:
        await expired_subscription_offer_service.mark_claimed_after_purchase(
            db,
            offer,
            plan_code=plan_code,
            period_days=period_days,
            base_price_kopeks=base_price_kopeks,
            price_kopeks=price_kopeks,
        )
        return
    await _mark_fixed_price_tariff_offer_claimed(db, offer)


async def show_tariffs_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Lists all active plans with description cards and price-from buttons."""
    texts = get_texts(db_user.language)
    plans = await list_active_plans(db)

    if not plans:
        await callback.answer(
            texts.t("TARIFFS_EMPTY", "Тарифы временно недоступны."),
            show_alert=True,
        )
        return

    cohort = resolve_pricing_cohort(db_user)
    plans_with_lowest = []
    hot_offer = await hot_invoice_offer_service.get_available_offer(db, db_user.id)
    expired_offer = None
    if hot_offer is None:
        expired_offer = await expired_subscription_offer_service.get_available_offer(db, db_user.id)
    for plan in plans:
        lowest_price = get_lowest_monthly_price(plan, cohort)
        hot_price, _, _ = await _get_hot_invoice_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=30,
            base_price_kopeks=lowest_price,
        )
        display_price = hot_price if hot_price is not None else lowest_price
        if hot_price is None and expired_offer is not None:
            expired_price, _, _ = await _get_expired_subscription_tariff_offer(
                db,
                db_user,
                plan_code=plan.code,
                period_days=30,
                base_price_kopeks=lowest_price,
            )
            if expired_price is not None:
                display_price = expired_price
        plans_with_lowest.append((plan, display_price))

    current_plan_id: Optional[int] = None
    current_plan_label: Optional[str] = None
    active_sub = await _resolve_active_subscription(db_user)
    if active_sub:
        current_plan_id = active_sub.plan_id
        period_price = await get_current_plan_price_for_period(db, active_sub, db_user)
        if period_price is not None and active_sub.plan is not None:
            current_plan_label = texts.t(
                "TARIFF_BUTTON_CURRENT",
                "✅ Текущий: {name} — {price} за {period}",
            ).format(
                name=active_sub.plan.display_name,
                price=_format_rub_short(period_price),
                period=_period_label(active_sub.plan_period_days or 30, texts),
            )

    cards = "\n\n".join(
        texts.t(f"TARIFF_CARD_{plan.code.upper()}", plan.description_md or plan.display_name)
        for plan in plans
    )
    message_text = "\n\n".join(
        part for part in [
            texts.t("TARIFFS_TITLE", "📋 <b>Тарифы</b>"),
            (
                "🎁 <b>Скидка −20% активна до 22:00 MSK</b>"
                if hot_offer is not None
                else None
            ),
            (
                "🎁 <b>Скидка −20% на восстановление активна до 22:00 MSK</b>"
                if expired_offer is not None
                else None
            ),
            cards,
            texts.t("TARIFFS_SUBTITLE", "Выберите подходящий тариф:"),
        ] if part
    )

    keyboard = get_tariffs_keyboard(
        plans_with_lowest,
        language=db_user.language,
        current_plan_id=current_plan_id,
        current_plan_label=current_plan_label,
    )

    image_path = os.path.join("images", "plans.jpg")
    await edit_or_answer_photo(
        callback,
        message_text,
        keyboard,
        photo_path=image_path if os.path.exists(image_path) else None,
    )
    await callback.answer()


async def show_tariff_periods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Step 2 — user picked a tier, now choose 1/3/6/12/24 months."""
    texts = get_texts(db_user.language)
    plan_code = callback.data.split(":", 1)[1] if ":" in callback.data else ""

    plan = await get_plan_by_code(db, plan_code)
    if not plan or not plan.is_active:
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return

    active_sub = await _resolve_active_subscription(db_user)
    if active_sub and active_sub.plan_id == plan.id:
        # User tapped their current tier — let them renew or upgrade-same-tier via the renew flow.
        await show_renew_current(callback, db_user, db)
        return

    if active_sub:
        # Mid-subscription tier switch: prorated upgrade screen.
        await _show_tier_switch(callback, db_user, db, active_sub, plan)
        return

    period_prices = select_prices_for_cohort(plan, resolve_pricing_cohort(db_user))
    hot_offer = await hot_invoice_offer_service.get_available_offer(db, db_user.id)
    expired_offer = None
    if hot_offer is None:
        expired_offer = await expired_subscription_offer_service.get_available_offer(db, db_user.id)
    if hot_offer is not None:
        await hot_invoice_offer_service.activate_offer(db, hot_offer)
    elif expired_offer is not None:
        await expired_subscription_offer_service.activate_offer(db, expired_offer)
    for period_days, base_price in list(period_prices.items()):
        hot_price, _, _ = await _get_hot_invoice_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=period_days,
            base_price_kopeks=base_price,
            activate=True,
        )
        if hot_price is not None:
            period_prices[period_days] = hot_price
            continue
        expired_price, _, _ = await _get_expired_subscription_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=period_days,
            base_price_kopeks=base_price,
            activate=True,
        )
        if expired_price is not None:
            period_prices[period_days] = expired_price
            continue
        fixed_price, _, _ = await _get_fixed_price_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=period_days,
        )
        if fixed_price is not None:
            period_prices[period_days] = fixed_price
    if (
        legacy_pro_offer_service.PERIOD_DAYS not in period_prices
        and hot_offer is None
        and expired_offer is None
    ):
        offer_price, _, _ = await _get_fixed_price_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=legacy_pro_offer_service.PERIOD_DAYS,
        )
        if offer_price is not None:
            period_prices[legacy_pro_offer_service.PERIOD_DAYS] = offer_price
    if not period_prices:
        await callback.answer(
            texts.t("TARIFFS_PRICES_MISSING", "Цены для этого тарифа не настроены."),
            show_alert=True,
        )
        return

    description = texts.t(
        f"TARIFF_CARD_{plan.code.upper()}",
        plan.description_md or plan.display_name,
    )
    title = texts.t(
        "TARIFF_PERIODS_TITLE",
        "💳 <b>{name}</b>\n\nВыберите период:",
    ).format(name=plan.display_name)
    message_text = f"{description}\n\n{title}"

    keyboard = get_tariff_periods_keyboard(
        plan.code,
        period_prices,
        language=db_user.language,
    )

    image_path = os.path.join("images", "plans.jpg")
    await edit_or_answer_photo(
        callback,
        message_text,
        keyboard,
        photo_path=image_path if os.path.exists(image_path) else None,
    )
    await callback.answer()


async def _show_tier_switch(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    active_sub: Subscription,
    new_plan: SubscriptionPlan,
):
    """Show the prorated-delta confirm screen for switching tiers mid-subscription."""
    texts = get_texts(db_user.language)
    period_days = active_sub.plan_period_days or 30
    cohort = resolve_pricing_cohort(db_user)

    new_price = await get_plan_price(db, new_plan.id, period_days, cohort=cohort)
    current_price = await get_current_plan_price_for_period(db, active_sub, db_user)

    if new_price is None:
        await callback.answer(
            texts.t("TARIFFS_PRICES_MISSING", "Цены для этого тарифа не настроены."),
            show_alert=True,
        )
        return

    hot_price, _, _ = await _get_hot_invoice_tariff_offer(
        db,
        db_user,
        plan_code=new_plan.code,
        period_days=period_days,
        base_price_kopeks=new_price,
        activate=True,
    )
    if hot_price is not None:
        new_price = hot_price

    delta = calculate_upgrade_delta(active_sub, new_plan, new_price, current_price)
    days_remaining = max(0, (active_sub.end_date - datetime.utcnow()).days)

    description = texts.t(
        f"TARIFF_CARD_{new_plan.code.upper()}",
        new_plan.description_md or new_plan.display_name,
    )
    title = texts.t(
        "TARIFF_UPGRADE_TITLE",
        "🔀 <b>Смена тарифа: {name}</b>\n\nОсталось дней: {days_left}\nДоплата считается пропорционально остатку.\n\nВыберите целевой период (для расчёта берётся ваш текущий период — {current_period}):",
    ).format(
        name=new_plan.display_name,
        days_left=days_remaining,
        current_period=_period_label(period_days, texts),
    )
    message_text = f"{description}\n\n{title}"

    keyboard = get_tariff_upgrade_keyboard(
        new_plan.code,
        delta,
        new_plan.display_name,
        language=db_user.language,
    )

    try:
        await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.answer(message_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def finalize_tariff_purchase(
    db: AsyncSession,
    db_user: User,
    plan: SubscriptionPlan,
    period_days: int,
    price_kopeks: int,
    bot: Bot = None,
) -> Optional[Tuple[Subscription, object, bool]]:
    """Charge price, create/replace the subscription with the tariff, activate in Remnawave.

    Caller MUST verify balance >= price_kopeks first. Used by both the interactive purchase
    handler and the post-top-up auto-purchase. Returns (subscription, transaction,
    was_trial_conversion) or None if the balance deduction failed.
    """
    texts = get_texts(db_user.language)
    description = texts.t(
        "TARIFF_PURCHASE_INVOICE_DESCRIPTION",
        "Подписка {name} на {period}",
    ).format(name=plan.display_name, period=_period_label(period_days, texts))

    # Charge, record the payment, and create the subscription in ONE DB
    # transaction. subtract_user_balance/create_transaction defer their commits
    # (commit=False) so a failure while building the subscription rolls the
    # charge back instead of leaving money debited without an active subscription.
    ok = await subtract_user_balance(
        db, db_user, price_kopeks, description=description,
        create_transaction=False, commit=False,
    )
    if not ok:
        await db.rollback()
        return None

    try:
        connected_squads = await _all_active_server_uuids(db)
        now = datetime.utcnow()
        end_date = now + timedelta(days=period_days)

        # Subscription.user_id is UNIQUE — for trial / expired-legacy / expired-tier users
        # we replace the existing row in-place instead of creating a new one.
        # Query explicitly instead of touching db_user.subscription: the relationship
        # may be unloaded/expired here, and a lazy load in async context raises
        # greenlet_spawn ("IO attempted in an unexpected place").
        existing_sub = (
            await db.execute(
                select(Subscription).where(Subscription.user_id == db_user.id)
            )
        ).scalar_one_or_none()
        was_trial_conversion = bool(existing_sub and existing_sub.is_trial)

        if existing_sub is not None:
            existing_sub.status = SubscriptionStatus.ACTIVE.value
            existing_sub.is_trial = False
            existing_sub.start_date = now
            existing_sub.end_date = end_date
            existing_sub.traffic_limit_gb = plan.traffic_limit_gb
            existing_sub.device_limit = plan.device_limit
            existing_sub.connected_squads = connected_squads
            existing_sub.plan_id = plan.id
            existing_sub.plan_period_days = period_days
            new_sub = existing_sub
        else:
            new_sub = Subscription(
                user_id=db_user.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now,
                end_date=end_date,
                traffic_limit_gb=plan.traffic_limit_gb,
                device_limit=plan.device_limit,
                connected_squads=connected_squads,
                autopay_enabled=False,
                autopay_days_before=3,
                plan_id=plan.id,
                plan_period_days=period_days,
            )
            db.add(new_sub)

        db_user.has_made_first_topup = True
        db_user.has_had_paid_subscription = True

        # Surface IntegrityError (e.g. UNIQUE on user_id) BEFORE the payment is committed.
        await db.flush()

        # This commit persists the balance change, the subscription row, and the
        # transaction together — atomically.
        transaction = await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=description,
        )
    except Exception:
        await db.rollback()
        raise

    await db.refresh(new_sub)
    await db.refresh(db_user)

    # Лучи рефереру за квалифицирующую покупку реферала (≥3 мес). Best-effort:
    # сбой начисления не должен ронять покупку. transaction.id — «payment_id»
    # для идемпотентности.
    try:
        from app.services.rays_service import award_rays_for_referral_purchase
        await award_rays_for_referral_purchase(
            db, db_user, period_days, transaction.id, bot=bot,
        )
    except Exception as e:
        logger.warning(f"Лучи не начислены за покупку {transaction.id}: {e}")

    try:
        await SubscriptionService().create_remnawave_user(db, new_sub)
    except Exception as e:
        logger.warning(f"Не удалось синхронизировать новую подписку {new_sub.id}: {e}")

    return new_sub, transaction, was_trial_conversion


async def finalize_tariff_renewal(
    db: AsyncSession,
    db_user: User,
    subscription: Subscription,
    plan: SubscriptionPlan,
    period_days: int,
    price_kopeks: int,
) -> Optional[Tuple[Subscription, object, datetime]]:
    """Charge price, extend the subscription by period_days, re-sync Remnawave.

    Caller MUST verify balance >= price_kopeks first. Returns (subscription, transaction,
    old_end_date) or None if the balance deduction failed.
    """
    texts = get_texts(db_user.language)
    description = texts.t(
        "TARIFF_RENEW_INVOICE_DESCRIPTION",
        "Продление подписки {name} на {period}",
    ).format(name=plan.display_name, period=_period_label(period_days, texts))

    # Charge + extend + record the payment atomically (single commit) so a
    # failure can't leave the user charged without the extension applied.
    ok = await subtract_user_balance(
        db, db_user, price_kopeks, description=description,
        create_transaction=False, commit=False,
    )
    if not ok:
        await db.rollback()
        return None

    try:
        old_end_date = subscription.end_date
        subscription.extend_subscription(period_days)
        subscription.plan_period_days = period_days
        await db.flush()

        transaction = await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=description,
        )
    except Exception:
        await db.rollback()
        raise

    await db.refresh(subscription)

    try:
        await SubscriptionService().create_remnawave_user(db, subscription)
    except Exception as e:
        logger.warning(f"Не удалось синхронизировать продление подписки {subscription.id}: {e}")

    return subscription, transaction, old_end_date


async def finalize_tier_switch(
    db: AsyncSession,
    db_user: User,
    active_sub: Subscription,
    plan: SubscriptionPlan,
    delta_kopeks: int,
) -> Subscription:
    """Charge the prorated delta (if > 0), swap plan_id + limits, re-sync Remnawave.

    Caller MUST verify balance >= delta_kopeks first.
    """
    texts = get_texts(db_user.language)

    # Charge the delta, swap the plan, and record the payment atomically.
    try:
        if delta_kopeks > 0:
            days_remaining = max(0, (active_sub.end_date - datetime.utcnow()).days)
            description = texts.t(
                "TARIFF_UPGRADE_INVOICE_DESCRIPTION",
                "Смена тарифа на {name} (доплата за {days_left} дн.)",
            ).format(name=plan.display_name, days_left=days_remaining)
            ok = await subtract_user_balance(
                db, db_user, delta_kopeks, description=description,
                create_transaction=False, commit=False,
            )
            if not ok:
                await db.rollback()
                return active_sub

            active_sub.plan_id = plan.id
            active_sub.device_limit = plan.device_limit
            active_sub.traffic_limit_gb = plan.traffic_limit_gb
            await db.flush()

            await create_transaction(
                db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=delta_kopeks,
                description=description,
            )
        else:
            active_sub.plan_id = plan.id
            active_sub.device_limit = plan.device_limit
            active_sub.traffic_limit_gb = plan.traffic_limit_gb
            await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(active_sub)

    try:
        await SubscriptionService().create_remnawave_user(db, active_sub)
    except Exception as e:
        logger.warning(f"Не удалось синхронизировать смену тарифа подписки {active_sub.id}: {e}")

    return active_sub


async def confirm_tier_upgrade(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Execute the prorated tier switch: charge delta, swap plan_id + plan_period_days + limits."""
    texts = get_texts(db_user.language)
    plan_code = callback.data.split(":", 1)[1] if ":" in callback.data else ""

    plan = await get_plan_by_code(db, plan_code)
    if not plan or not plan.is_active:
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return

    active_sub = await _resolve_active_subscription(db_user)
    if not active_sub:
        await callback.answer(
            texts.t("SUBSCRIPTION_NOT_ACTIVE", "Подписка не активна."),
            show_alert=True,
        )
        return

    if active_sub.plan_id == plan.id:
        await callback.answer(
            texts.t("TARIFF_ALREADY_CURRENT", "Это ваш текущий тариф."),
            show_alert=True,
        )
        return

    period_days = active_sub.plan_period_days or 30
    base_new_price = await get_plan_price(
        db, plan.id, period_days, cohort=resolve_pricing_cohort(db_user)
    )
    current_price = await get_current_plan_price_for_period(db, active_sub, db_user)

    if base_new_price is None:
        await callback.answer(
            texts.t("TARIFFS_PRICES_MISSING", "Цены для этого тарифа не настроены."),
            show_alert=True,
        )
        return
    new_price = base_new_price
    hot_price, hot_offer, hot_offer_type = await _get_hot_invoice_tariff_offer(
        db,
        db_user,
        plan_code=plan.code,
        period_days=period_days,
        base_price_kopeks=base_new_price,
        activate=True,
    )
    if hot_price is not None:
        new_price = hot_price

    delta = calculate_upgrade_delta(active_sub, plan, new_price, current_price)

    if delta > 0 and db_user.balance_kopeks < delta:
        missing = delta - db_user.balance_kopeks
        await _save_tariff_intent_cart(
            db_user,
            tariff_op="upgrade",
            plan=plan,
            period_days=period_days,
            total_price=delta,
            offer_id=getattr(hot_offer, "id", None),
            price_override_kopeks=hot_price,
            offer_type=hot_offer_type,
        )
        await edit_or_answer_photo(
            callback,
            texts.t(
                "ADDON_INSUFFICIENT_FUNDS_MESSAGE",
                "⚠️ <b>Недостаточно средств</b>\n\nСтоимость услуги: {required}\nНа балансе: {balance}\nНе хватает: {missing}\n\nВыберите способ пополнения. Сумма подставится автоматически.",
            ).format(
                required=_format_rub_short(delta),
                balance=_format_rub_short(db_user.balance_kopeks),
                missing=_format_rub_short(missing),
            ),
            get_insufficient_balance_keyboard(
                language=db_user.language,
                amount_kopeks=missing,
                has_saved_cart=True,
            ),
            photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
        )
        await callback.answer()
        return

    await finalize_tier_switch(db, db_user, active_sub, plan, delta)
    if hot_offer is not None:
        await hot_invoice_offer_service.mark_claimed_after_purchase(
            db,
            hot_offer,
            plan_code=plan.code,
            period_days=period_days,
            base_price_kopeks=base_new_price,
            price_kopeks=new_price,
        )

    await callback.answer(
        texts.t("TARIFF_UPGRADE_DONE", "Тариф изменён ✅"),
        show_alert=False,
    )
    # Refresh the subscription page so the user sees the new tier
    from app.handlers.subscription.purchase import show_subscription_info
    callback.data = "menu_subscription"
    await show_subscription_info(callback, db_user, db)


async def _resolve_tariff_purchase_price(
    db: AsyncSession,
    db_user: User,
    plan: SubscriptionPlan,
    period_days: int,
) -> Optional[tuple]:
    """Resolve the purchase price with offer overrides applied (hot-invoice →
    expired-sub → fixed-price). Returns (base_price, price, offer_price, offer,
    offer_type) or None when the plan has no configured price."""
    base_price_kopeks = await get_plan_price(
        db, plan.id, period_days, cohort=resolve_pricing_cohort(db_user)
    )
    if base_price_kopeks is None:
        return None

    price_kopeks = base_price_kopeks
    offer_price, offer, offer_type = await _get_hot_invoice_tariff_offer(
        db,
        db_user,
        plan_code=plan.code,
        period_days=period_days,
        base_price_kopeks=base_price_kopeks,
        activate=True,
    )
    if offer_price is not None:
        price_kopeks = offer_price
    else:
        offer_price, offer, offer_type = await _get_expired_subscription_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=period_days,
            base_price_kopeks=base_price_kopeks,
            activate=True,
        )
        if offer_price is not None:
            price_kopeks = offer_price
        else:
            offer_price, offer, offer_type = await _get_fixed_price_tariff_offer(
                db,
                db_user,
                plan_code=plan.code,
                period_days=period_days,
            )
            if offer_price is not None:
                price_kopeks = offer_price

    return base_price_kopeks, price_kopeks, offer_price, offer, offer_type


async def start_tariff_purchase(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Step 3 — user picked plan+period; create subscription for users without one."""
    texts = get_texts(db_user.language)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("bad_callback", show_alert=False)
        return
    plan_code, period_str = parts[1], parts[2]
    try:
        period_days = int(period_str)
    except ValueError:
        await callback.answer("bad_callback", show_alert=False)
        return
    if period_days not in SUPPORTED_PERIOD_DAYS:
        await callback.answer("bad_period", show_alert=False)
        return

    plan = await get_plan_by_code(db, plan_code)
    if not plan or not plan.is_active:
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return

    resolved = await _resolve_tariff_purchase_price(db, db_user, plan, period_days)
    if resolved is None:
        await callback.answer(
            texts.t("TARIFFS_PRICES_MISSING", "Цены для этого тарифа не настроены."),
            show_alert=True,
        )
        return
    base_price_kopeks, price_kopeks, offer_price, offer, offer_type = resolved

    active_sub = await _resolve_active_subscription(db_user)
    if active_sub and active_sub.plan_id == plan.id:
        # Buying current tier from the tariffs page == renewal at current tier.
        await _execute_renewal(callback, db_user, db, active_sub, plan, period_days)
        return
    if active_sub:
        # Different active tier — route to tier-switch path instead of purchase.
        await _show_tier_switch(callback, db_user, db, active_sub, plan)
        return

    if db_user.balance_kopeks < price_kopeks:
        await _save_tariff_intent_cart(
            db_user,
            tariff_op="purchase",
            plan=plan,
            period_days=period_days,
            total_price=price_kopeks,
            offer_id=getattr(offer, "id", None),
            price_override_kopeks=offer_price,
            offer_type=offer_type,
            base_price_kopeks=base_price_kopeks,
        )
        if db_user.balance_kopeks > 0:
            # Частичная оплата: скидка уже применена к цене, баланс зачитывается
            # к сниженной цене, счёт выставляется на разницу.
            await _render_tariff_partial_breakdown(
                callback, db_user, plan, period_days,
                base_price_kopeks=base_price_kopeks,
                price_kopeks=price_kopeks,
            )
            return
        missing = price_kopeks - db_user.balance_kopeks
        await edit_or_answer_photo(
            callback,
            texts.t(
                "ADDON_INSUFFICIENT_FUNDS_MESSAGE",
                "⚠️ <b>Недостаточно средств</b>\n\nСтоимость услуги: {required}\nНа балансе: {balance}\nНе хватает: {missing}\n\nВыберите способ пополнения. Сумма подставится автоматически.",
            ).format(
                required=_format_rub_short(price_kopeks),
                balance=_format_rub_short(db_user.balance_kopeks),
                missing=_format_rub_short(missing),
            ),
            get_insufficient_balance_keyboard(
                language=db_user.language,
                amount_kopeks=missing,
                has_saved_cart=True,
            ),
            photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
        )
        await callback.answer()
        return

    result = await finalize_tariff_purchase(
        db, db_user, plan, period_days, price_kopeks, bot=callback.bot,
    )
    if result is None:
        await callback.answer(
            texts.t("BALANCE_DEDUCTION_FAILED", "Не удалось списать средства."),
            show_alert=True,
        )
        return
    new_sub, transaction, was_trial_conversion = result
    await _mark_tariff_offer_claimed(
        db,
        offer,
        plan_code=plan.code,
        period_days=period_days,
        base_price_kopeks=base_price_kopeks,
        price_kopeks=price_kopeks,
    )

    try:
        await send_purchase_notification(
            callback, db, db_user, new_sub, transaction.id, period_days,
            was_trial_conversion=was_trial_conversion,
        )
    except Exception as e:
        logger.debug(f"Уведомление о покупке не отправлено: {e}")

    await callback.answer()
    await callback.message.edit_text(
        format_subscription_purchase_success(
            plan=subscription_plan_name(new_sub, plan),
            period=period_days,
            end_date=new_sub.end_date,
        ),
        reply_markup=build_success_management_keyboard(),
        parse_mode="HTML",
    )


async def _render_tariff_partial_breakdown(
    callback: types.CallbackQuery,
    db_user: User,
    plan: SubscriptionPlan,
    period_days: int,
    *,
    base_price_kopeks: int,
    price_kopeks: int,
) -> None:
    """Экран разбивки частичной оплаты: тариф, скидка, зачёт баланса, доплата."""
    texts = get_texts(db_user.language)
    shortfall = price_kopeks - db_user.balance_kopeks

    lines = [
        texts.t("TARIFF_PARTIAL_TITLE", "🧾 <b>Оформление подписки</b>"),
        "",
        texts.t(
            "TARIFF_PARTIAL_PLAN_LINE", "Тариф: <b>{name}, {period}</b> — {price}"
        ).format(
            name=plan.display_name,
            period=_period_label(period_days, texts),
            price=_format_rub_short(base_price_kopeks),
        ),
    ]
    if price_kopeks < base_price_kopeks:
        lines.append(
            texts.t(
                "TARIFF_PARTIAL_DISCOUNT_LINE", "🔥 Скидка: −{discount} → <b>{price}</b>"
            ).format(
                discount=_format_rub_short(base_price_kopeks - price_kopeks),
                price=_format_rub_short(price_kopeks),
            )
        )
    lines += [
        "",
        texts.t(
            "TARIFF_PARTIAL_BALANCE_LINE", "💰 С баланса спишется: <b>{amount}</b>"
        ).format(amount=_format_rub_short(db_user.balance_kopeks)),
        texts.t(
            "TARIFF_PARTIAL_SHORTFALL_LINE", "💳 К доплате: <b>{amount}</b>"
        ).format(amount=_format_rub_short(shortfall)),
        "",
        texts.t(
            "TARIFF_PARTIAL_HINT",
            "Баланс спишется только после успешной доплаты. "
            "Если доплату не завершить — баланс останется нетронутым.",
        ),
    ]

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        "TARIFF_PARTIAL_PAY_BUTTON", "💳 Оплатить {amount}"
                    ).format(amount=_format_rub_short(shortfall)),
                    callback_data=f"tariff_pay_partial:{plan.code}:{period_days}",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f"tariff_select:{plan.code}",
                )
            ],
        ]
    )

    message_text = "\n".join(lines)
    await edit_or_answer_photo(
        callback,
        message_text,
        keyboard,
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


async def show_tariff_partial_payment_methods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Кнопка «Оплатить X₽» на экране разбивки: выбор способа доплаты.

    Пересчитывает цену и баланс на момент нажатия (скидка могла истечь, баланс —
    измениться) и пересохраняет intent-корзину, чтобы счёт зафиксировал
    актуальную разбивку.
    """
    texts = get_texts(db_user.language)
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("bad_callback", show_alert=False)
        return
    plan_code, period_str = parts[1], parts[2]
    try:
        period_days = int(period_str)
    except ValueError:
        await callback.answer("bad_callback", show_alert=False)
        return

    plan = await get_plan_by_code(db, plan_code)
    if not plan or not plan.is_active:
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return

    resolved = await _resolve_tariff_purchase_price(db, db_user, plan, period_days)
    if resolved is None:
        await callback.answer(
            texts.t("TARIFFS_PRICES_MISSING", "Цены для этого тарифа не настроены."),
            show_alert=True,
        )
        return
    base_price_kopeks, price_kopeks, offer_price, offer, offer_type = resolved

    # Баланса уже хватает или он обнулился — маршрутизируем через обычный
    # покупочный обработчик (полная оплата с баланса / старый экран пополнения).
    if db_user.balance_kopeks <= 0 or db_user.balance_kopeks >= price_kopeks:
        callback.data = f"tariff_buy:{plan.code}:{period_days}"
        await start_tariff_purchase(callback, db_user, db)
        return

    await _save_tariff_intent_cart(
        db_user,
        tariff_op="purchase",
        plan=plan,
        period_days=period_days,
        total_price=price_kopeks,
        offer_id=getattr(offer, "id", None),
        price_override_kopeks=offer_price,
        offer_type=offer_type,
        base_price_kopeks=base_price_kopeks,
    )

    from app.keyboards.inline import get_partial_payment_methods_keyboard
    from app.services.tariff_partial_payment_service import get_provider_min_kopeks

    shortfall = price_kopeks - db_user.balance_kopeks
    keyboard = get_partial_payment_methods_keyboard(
        shortfall,
        db_user.language,
        back_callback=f"tariff_buy:{plan.code}:{period_days}",
    )

    message_text = texts.t(
        "TARIFF_PARTIAL_METHODS_MESSAGE",
        "💳 <b>Доплата {amount}</b>\n\n"
        "Выберите способ оплаты. После успешной оплаты подписка активируется "
        "автоматически: {balance} спишется с баланса, {amount} — со счёта.",
    ).format(
        amount=_format_rub_short(shortfall),
        balance=_format_rub_short(db_user.balance_kopeks),
    )
    if any(
        get_provider_min_kopeks(m) > shortfall
        for m in ("yookassa", "mulenpay", "pal24", "platega", "wata", "cloudpayments", "cryptobot", "heleket")
    ):
        message_text += "\n\n" + texts.t(
            "TARIFF_PARTIAL_MIN_NOTE",
            "⚠️ У некоторых способов минимальная сумма счёта больше доплаты — "
            "в этом случае счёт выставляется на минимум, а излишек останется на балансе.",
        )

    await edit_or_answer_photo(
        callback,
        message_text,
        keyboard,
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


async def show_tariff_saved_cart(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    cart_data: dict,
):
    """«Вернуться к оформлению» для тарифной intent-корзины: показываем сохранённый
    тариф и отправляем обратно в start_tariff_purchase — там разбивка пересчитается
    от актуального баланса."""
    texts = get_texts(db_user.language)
    plan_code = cart_data.get("plan_code")
    period_days = int(cart_data.get("period_days") or 0)

    plan = await get_plan_by_code(db, plan_code) if plan_code else None
    if not plan or not plan.is_active or period_days <= 0:
        from app.services.user_cart_service import user_cart_service

        await user_cart_service.delete_user_cart(db_user.id)
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return

    message_text = texts.t(
        "TARIFF_SAVED_CART_MESSAGE",
        "🛒 <b>Восстановленная корзина</b>\n\n"
        "Тариф: <b>{name}, {period}</b>\n"
        "На балансе: {balance}\n\n"
        "Продолжите оформление — сумма к оплате пересчитается от текущего баланса.",
    ).format(
        name=plan.display_name,
        period=_period_label(period_days, texts),
        balance=_format_rub_short(db_user.balance_kopeks),
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t("TARIFF_SAVED_CART_RESUME", "▶️ Продолжить оформление"),
                    callback_data=f"tariff_buy:{plan.code}:{period_days}",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("TARIFF_SAVED_CART_CLEAR", "🗑️ Очистить корзину"),
                    callback_data="clear_saved_cart",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data="subscription_tariffs",
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.answer(message_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def claim_hot_invoice_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    parts = (callback.data or "").split(":")
    try:
        offer_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        offer_id = 0
    plan_code = parts[3] if len(parts) > 3 else ""

    offer = await hot_invoice_offer_service.get_available_offer(db, db_user.id)
    if not offer or not offer_id or offer.id != offer_id:
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    callback.data = f"tariff_select:{plan_code}"
    await show_tariff_periods(callback, db_user, db)


async def claim_expired_subscription_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    parts = (callback.data or "").split(":")
    try:
        offer_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        offer_id = 0
    plan_code = parts[3] if len(parts) > 3 else ""

    offer = await expired_subscription_offer_service.get_available_offer(db, db_user.id)
    expected_plan_code = (
        str((offer.extra_data or {}).get("plan_code") or "").lower()
        if offer
        else ""
    )
    if (
        not offer
        or not offer_id
        or offer.id != offer_id
        or expected_plan_code != (plan_code or "").lower()
    ):
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    callback.data = f"tariff_select:{plan_code}"
    await show_tariff_periods(callback, db_user, db)


async def claim_cold_solo_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    parts = (callback.data or "").split(":")
    try:
        offer_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        offer_id = 0

    offer = await cold_solo_offer_service.get_active_offer(db, db_user.id)
    if not offer or (offer_id and offer.id != offer_id):
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    if await cold_solo_offer_service.has_successful_subscription_purchase(db, db_user):
        await callback.answer("У вас уже есть оплаченная подписка", show_alert=True)
        return

    plan = await get_plan_by_code(db, cold_solo_offer_service.PLAN_CODE)
    if not plan or not plan.is_active:
        await callback.answer("Тариф Solo временно недоступен", show_alert=True)
        return

    if not cold_solo_offer_service.validate_offer_for_plan(
        offer,
        plan_code=plan.code,
        period_days=cold_solo_offer_service.PERIOD_DAYS,
    ):
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    price_kopeks = cold_solo_offer_service.PRICE_KOPEKS
    missing = max(0, price_kopeks - db_user.balance_kopeks)

    await _save_tariff_intent_cart(
        db_user,
        tariff_op="purchase",
        plan=plan,
        period_days=cold_solo_offer_service.PERIOD_DAYS,
        total_price=price_kopeks,
        offer_id=offer.id,
        price_override_kopeks=price_kopeks,
        offer_type=cold_solo_offer_service.NOTIFICATION_TYPE,
    )

    if missing == 0:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="Оплатить с баланса 990₽",
                        callback_data=f"cold_solo_offer:balance:{offer.id}",
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data="subscription_tariffs")],
            ]
        )
    else:
        keyboard = get_payment_methods_keyboard(missing, db_user.language)

    message = (
        "🔥 <b>Solo на 1 год за 990₽</b>\n\n"
        "Обычная цена: <s>1560₽</s>\n"
        "Цена предложения: <b>990₽</b>\n"
        "Действует до: <b>22:00 MSK</b>\n\n"
    )
    if missing:
        message += (
            f"На балансе: {texts.format_price(db_user.balance_kopeks)}\n"
            f"К оплате сейчас: {texts.format_price(missing)}"
        )
    else:
        message += "Средств на балансе достаточно для активации."

    await edit_or_answer_photo(
        callback,
        message,
        keyboard,
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


async def pay_cold_solo_offer_from_balance(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    parts = (callback.data or "").split(":")
    try:
        offer_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        offer_id = 0

    offer = await cold_solo_offer_service.get_active_offer(db, db_user.id)
    if not offer or not offer_id or offer.id != offer_id:
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    if await cold_solo_offer_service.has_successful_subscription_purchase(db, db_user):
        await callback.answer("У вас уже есть оплаченная подписка", show_alert=True)
        return

    plan = await get_plan_by_code(db, cold_solo_offer_service.PLAN_CODE)
    if not plan or not cold_solo_offer_service.validate_offer_for_plan(
        offer,
        plan_code=getattr(plan, "code", None),
        period_days=cold_solo_offer_service.PERIOD_DAYS,
    ):
        await callback.answer("Предложение недоступно", show_alert=True)
        return

    if db_user.balance_kopeks < cold_solo_offer_service.PRICE_KOPEKS:
        await callback.answer("Недостаточно средств на балансе", show_alert=True)
        return

    result = await finalize_tariff_purchase(
        db,
        db_user,
        plan,
        cold_solo_offer_service.PERIOD_DAYS,
        cold_solo_offer_service.PRICE_KOPEKS,
        bot=callback.bot,
    )
    if result is None:
        await callback.answer("Не удалось списать средства", show_alert=True)
        return
    new_sub, _, _ = result

    await cold_solo_offer_service.mark_claimed_after_purchase(db, offer)
    await callback.answer()
    await callback.message.edit_text(
        format_subscription_purchase_success(
            plan=subscription_plan_name(new_sub, plan),
            period=cold_solo_offer_service.PERIOD_DAYS,
            end_date=new_sub.end_date,
        ),
        reply_markup=build_success_management_keyboard(),
        parse_mode="HTML",
    )


async def claim_legacy_pro_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    parts = (callback.data or "").split(":")
    try:
        offer_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        offer_id = 0

    offer = await legacy_pro_offer_service.get_active_offer(db, db_user.id)
    if not offer or (offer_id and offer.id != offer_id):
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    offer_user = getattr(offer, "user", None) or db_user
    if legacy_pro_offer_service.has_converted_to_tariff(offer_user):
        await callback.answer("У вас уже подключён новый тариф", show_alert=True)
        return

    plan = await get_plan_by_code(db, legacy_pro_offer_service.PLAN_CODE)
    if not plan or not plan.is_active:
        await callback.answer("Тариф Pro временно недоступен", show_alert=True)
        return

    if not legacy_pro_offer_service.validate_offer_for_plan(
        offer,
        plan_code=plan.code,
        period_days=legacy_pro_offer_service.PERIOD_DAYS,
    ):
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    price_kopeks, offer = await legacy_pro_offer_service.get_price_override(
        db,
        db_user.id,
        plan_code=plan.code,
        period_days=legacy_pro_offer_service.PERIOD_DAYS,
    )
    if price_kopeks is None or offer is None:
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    missing = max(0, price_kopeks - db_user.balance_kopeks)
    await _save_tariff_intent_cart(
        db_user,
        tariff_op="purchase",
        plan=plan,
        period_days=legacy_pro_offer_service.PERIOD_DAYS,
        total_price=price_kopeks,
        offer_id=offer.id,
        price_override_kopeks=price_kopeks,
        offer_type=getattr(offer, "notification_type", None),
    )

    price_label = _format_rub_short(price_kopeks)
    if missing == 0:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=f"Оплатить с баланса {price_label}",
                        callback_data=f"legacy_pro_offer:balance:{offer.id}",
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data="subscription_tariffs")],
            ]
        )
    else:
        keyboard = get_payment_methods_keyboard(missing, db_user.language)

    message = (
        f"💎 <b>Pro на 1 год за {price_label}</b>\n\n"
        "Обычная цена: <s>2280₽</s>\n"
        f"Цена предложения: <b>{price_label}</b>\n"
        "Действует до: <b>1 августа 23:59 MSK</b>\n\n"
    )
    if missing:
        message += (
            f"На балансе: {texts.format_price(db_user.balance_kopeks)}\n"
            f"К оплате сейчас: {texts.format_price(missing)}"
        )
    else:
        message += "Средств на балансе достаточно для активации."

    await edit_or_answer_photo(
        callback,
        message,
        keyboard,
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


async def pay_legacy_pro_offer_from_balance(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    parts = (callback.data or "").split(":")
    try:
        offer_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        offer_id = 0

    offer = await legacy_pro_offer_service.get_active_offer(db, db_user.id)
    if not offer or not offer_id or offer.id != offer_id:
        await callback.answer("Предложение недоступно или истекло", show_alert=True)
        return

    offer_user = getattr(offer, "user", None) or db_user
    if legacy_pro_offer_service.has_converted_to_tariff(offer_user):
        await callback.answer("У вас уже подключён новый тариф", show_alert=True)
        return

    plan = await get_plan_by_code(db, legacy_pro_offer_service.PLAN_CODE)
    if not plan or not legacy_pro_offer_service.validate_offer_for_plan(
        offer,
        plan_code=getattr(plan, "code", None),
        period_days=legacy_pro_offer_service.PERIOD_DAYS,
    ):
        await callback.answer("Предложение недоступно", show_alert=True)
        return

    price_kopeks, offer = await legacy_pro_offer_service.get_price_override(
        db,
        db_user.id,
        plan_code=plan.code,
        period_days=legacy_pro_offer_service.PERIOD_DAYS,
    )
    if price_kopeks is None or offer is None:
        await callback.answer("Предложение недоступно", show_alert=True)
        return

    if db_user.balance_kopeks < price_kopeks:
        await callback.answer("Недостаточно средств на балансе", show_alert=True)
        return

    result = await finalize_tariff_purchase(
        db,
        db_user,
        plan,
        legacy_pro_offer_service.PERIOD_DAYS,
        price_kopeks,
        bot=callback.bot,
    )
    if result is None:
        await callback.answer("Не удалось списать средства", show_alert=True)
        return
    new_sub, _, _ = result

    await legacy_pro_offer_service.mark_claimed_after_purchase(db, offer)
    await callback.answer()
    await callback.message.edit_text(
        format_subscription_purchase_success(
            plan=subscription_plan_name(new_sub, plan),
            period=legacy_pro_offer_service.PERIOD_DAYS,
            end_date=new_sub.end_date,
        ),
        reply_markup=build_success_management_keyboard(),
        parse_mode="HTML",
    )


async def show_renew_current(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Show 5 period buttons priced at the user's current tier."""
    texts = get_texts(db_user.language)
    active_sub = await _resolve_renewable_subscription(db_user)
    if not active_sub:
        await callback.answer(
            texts.t("SUBSCRIPTION_NOT_ACTIVE", "Подписка не активна."),
            show_alert=True,
        )
        return
    # Always reload via get_plan_by_id so prices are eagerly loaded — Subscription.plan
    # is lazy="joined" for the plan itself but NOT for plan.prices.
    plan = await get_plan_by_id(db, active_sub.plan_id) if active_sub.plan_id else None
    if plan is None:
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return

    period_prices = select_prices_for_cohort(plan, resolve_pricing_cohort(db_user))
    hot_offer = await hot_invoice_offer_service.get_available_offer(db, db_user.id)
    expired_offer = None
    if hot_offer is None:
        expired_offer = await expired_subscription_offer_service.get_available_offer(db, db_user.id)
    if hot_offer is not None:
        await hot_invoice_offer_service.activate_offer(db, hot_offer)
        for period_days, base_price in list(period_prices.items()):
            hot_price, _, _ = await _get_hot_invoice_tariff_offer(
                db,
                db_user,
                plan_code=plan.code,
                period_days=period_days,
                base_price_kopeks=base_price,
                activate=True,
            )
            if hot_price is not None:
                period_prices[period_days] = hot_price
    elif expired_offer is not None:
        await expired_subscription_offer_service.activate_offer(db, expired_offer)
        for period_days, base_price in list(period_prices.items()):
            expired_price, _, _ = await _get_expired_subscription_tariff_offer(
                db,
                db_user,
                plan_code=plan.code,
                period_days=period_days,
                base_price_kopeks=base_price,
                activate=True,
            )
            if expired_price is not None:
                period_prices[period_days] = expired_price

    description = texts.t(
        f"TARIFF_CARD_{plan.code.upper()}",
        plan.description_md or plan.display_name,
    )
    message_text = texts.t(
        "RENEW_TITLE",
        "💎 <b>Продление подписки</b>\n\nТариф: {name}\n{description}\n\nВыберите период продления:",
    ).format(name=plan.display_name, description=description)

    keyboard = get_renew_periods_keyboard(plan.id, period_prices, language=db_user.language)

    try:
        await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.answer(message_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def apply_renewal(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Renew at current tier: pay full period price and extend end_date by period_days."""
    texts = get_texts(db_user.language)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("bad_callback", show_alert=False)
        return
    try:
        plan_id = int(parts[1])
        period_days = int(parts[2])
    except ValueError:
        await callback.answer("bad_callback", show_alert=False)
        return
    if period_days not in SUPPORTED_PERIOD_DAYS:
        await callback.answer("bad_period", show_alert=False)
        return

    active_sub = await _resolve_renewable_subscription(db_user)
    if not active_sub or active_sub.plan_id != plan_id:
        await callback.answer(
            texts.t("SUBSCRIPTION_NOT_ACTIVE", "Подписка не активна."),
            show_alert=True,
        )
        return
    plan = active_sub.plan or await get_plan_by_id(db, plan_id)
    if not plan:
        await callback.answer(
            texts.t("TARIFFS_PLAN_NOT_FOUND", "Тариф не найден."),
            show_alert=True,
        )
        return
    await _execute_renewal(callback, db_user, db, active_sub, plan, period_days)


async def _execute_renewal(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    subscription: Subscription,
    plan: SubscriptionPlan,
    period_days: int,
):
    texts = get_texts(db_user.language)
    base_price_kopeks = await get_plan_price(
        db, plan.id, period_days, cohort=resolve_pricing_cohort(db_user)
    )
    if base_price_kopeks is None:
        await callback.answer(
            texts.t("TARIFFS_PRICES_MISSING", "Цены для этого тарифа не настроены."),
            show_alert=True,
        )
        return

    price_kopeks = base_price_kopeks
    hot_price, hot_offer, hot_offer_type = await _get_hot_invoice_tariff_offer(
        db,
        db_user,
        plan_code=plan.code,
        period_days=period_days,
        base_price_kopeks=base_price_kopeks,
        activate=True,
    )
    if hot_price is not None:
        price_kopeks = hot_price
    else:
        hot_price, hot_offer, hot_offer_type = await _get_expired_subscription_tariff_offer(
            db,
            db_user,
            plan_code=plan.code,
            period_days=period_days,
            base_price_kopeks=base_price_kopeks,
            activate=True,
        )
        if hot_price is not None:
            price_kopeks = hot_price

    if db_user.balance_kopeks < price_kopeks:
        missing = price_kopeks - db_user.balance_kopeks
        await _save_tariff_intent_cart(
            db_user,
            tariff_op="renew",
            plan=plan,
            period_days=period_days,
            total_price=price_kopeks,
            offer_id=getattr(hot_offer, "id", None),
            price_override_kopeks=hot_price,
            offer_type=hot_offer_type,
        )
        await edit_or_answer_photo(
            callback,
            texts.t(
                "ADDON_INSUFFICIENT_FUNDS_MESSAGE",
                "⚠️ <b>Недостаточно средств</b>\n\nСтоимость услуги: {required}\nНа балансе: {balance}\nНе хватает: {missing}\n\nВыберите способ пополнения. Сумма подставится автоматически.",
            ).format(
                required=_format_rub_short(price_kopeks),
                balance=_format_rub_short(db_user.balance_kopeks),
                missing=_format_rub_short(missing),
            ),
            get_insufficient_balance_keyboard(
                language=db_user.language,
                amount_kopeks=missing,
                has_saved_cart=True,
            ),
            photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
        )
        await callback.answer()
        return

    result = await finalize_tariff_renewal(db, db_user, subscription, plan, period_days, price_kopeks)
    if result is None:
        await callback.answer(
            texts.t("BALANCE_DEDUCTION_FAILED", "Не удалось списать средства."),
            show_alert=True,
        )
        return
    subscription, transaction, old_end_date = result
    await _mark_tariff_offer_claimed(
        db,
        hot_offer,
        plan_code=plan.code,
        period_days=period_days,
        base_price_kopeks=base_price_kopeks,
        price_kopeks=price_kopeks,
    )

    try:
        await send_extension_notification(
            callback, db, db_user, subscription, transaction.id, period_days, old_end_date,
        )
    except Exception as e:
        logger.debug(f"Уведомление о продлении не отправлено: {e}")

    await callback.answer()
    await callback.message.edit_text(
        format_subscription_renewal_success(
            plan=subscription_plan_name(subscription, plan),
            days=period_days,
            end_date=subscription.end_date,
        ),
        reply_markup=build_success_management_keyboard(),
        parse_mode="HTML",
    )


async def _all_active_server_uuids(db: AsyncSession) -> list:
    squads = await get_active_server_squads(db)
    return [s.squad_uuid for s in squads if getattr(s, "squad_uuid", None)]


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(
        claim_cold_solo_offer,
        F.data.startswith("cold_solo_offer:claim:"),
    )
    dp.callback_query.register(
        pay_cold_solo_offer_from_balance,
        F.data.startswith("cold_solo_offer:balance:"),
    )
    dp.callback_query.register(
        claim_legacy_pro_offer,
        F.data.startswith("legacy_pro_offer:claim:"),
    )
    dp.callback_query.register(
        pay_legacy_pro_offer_from_balance,
        F.data.startswith("legacy_pro_offer:balance:"),
    )
    dp.callback_query.register(
        claim_hot_invoice_offer,
        F.data.startswith("hot_invoice_offer:claim:"),
    )
    dp.callback_query.register(
        claim_expired_subscription_offer,
        F.data.startswith("expired_subscription_offer:claim:"),
    )
    dp.callback_query.register(
        show_tariffs_page,
        F.data == "subscription_tariffs",
    )
    dp.callback_query.register(
        show_tariff_periods,
        F.data.startswith("tariff_select:"),
    )
    dp.callback_query.register(
        start_tariff_purchase,
        F.data.startswith("tariff_buy:"),
    )
    dp.callback_query.register(
        show_tariff_partial_payment_methods,
        F.data.startswith("tariff_pay_partial:"),
    )
    dp.callback_query.register(
        confirm_tier_upgrade,
        F.data.startswith("tariff_upgrade_confirm:"),
    )
    dp.callback_query.register(
        show_renew_current,
        F.data == "subscription_renew_current",
    )
    dp.callback_query.register(
        apply_renewal,
        F.data.startswith("tariff_renew:"),
    )
