"""Automatic subscription purchase from a saved cart after balance top-up."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import Subscription, TransactionType, User
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.subscription_checkout_service import clear_subscription_checkout_draft
from app.services.subscription_purchase_service import (
    PurchaseOptionsContext,
    PurchasePricingResult,
    PurchaseSelection,
    PurchaseValidationError,
    PurchaseBalanceError,
    MiniAppSubscriptionPurchaseService,
)
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.utils.pricing_utils import format_period_description
from app.utils.timezone import format_local_datetime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AutoPurchaseContext:
    """Aggregated data prepared for automatic checkout processing."""

    context: PurchaseOptionsContext
    pricing: PurchasePricingResult
    selection: PurchaseSelection
    service: MiniAppSubscriptionPurchaseService


@dataclass(slots=True)
class AutoExtendContext:
    """Data required to automatically extend an existing subscription."""

    subscription: Subscription
    period_days: int
    price_kopeks: int
    description: str
    device_limit: Optional[int] = None
    traffic_limit_gb: Optional[int] = None
    squad_uuid: Optional[str] = None
    consume_promo_offer: bool = False


async def _prepare_auto_purchase(
    db: AsyncSession,
    user: User,
    cart_data: dict,
) -> Optional[AutoPurchaseContext]:
    """Builds purchase context and pricing for a saved cart."""

    period_days = int(cart_data.get("period_days") or 0)
    if period_days <= 0:
        logger.info(
            "🔁 Автопокупка: у пользователя %s нет корректного периода в сохранённой корзине",
            user.telegram_id,
        )
        return None

    miniapp_service = MiniAppSubscriptionPurchaseService()
    context = await miniapp_service.build_options(db, user)

    period_config = context.period_map.get(f"days:{period_days}")
    if not period_config:
        logger.warning(
            "🔁 Автопокупка: период %s дней недоступен для пользователя %s",
            period_days,
            user.telegram_id,
        )
        return None

    traffic_value = cart_data.get("traffic_gb")
    if traffic_value is None:
        traffic_value = (
            period_config.traffic.current_value
            if period_config.traffic.current_value is not None
            else period_config.traffic.default_value or 0
        )
    else:
        traffic_value = int(traffic_value)

    devices = int(cart_data.get("devices") or period_config.devices.current or 1)
    servers = list(cart_data.get("countries") or [])
    if not servers:
        servers = list(period_config.servers.default_selection)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_value,
        servers=servers,
        devices=devices,
    )

    pricing = await miniapp_service.calculate_pricing(db, context, selection)
    return AutoPurchaseContext(
        context=context,
        pricing=pricing,
        selection=selection,
        service=miniapp_service,
    )


def _safe_int(value: Optional[object], default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


async def _prepare_auto_extend_context(
    db: AsyncSession,
    user: User,
    cart_data: dict,
) -> Optional[AutoExtendContext]:
    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user.id)
    if subscription is None:
        logger.info(
            "🔁 Автопокупка: у пользователя %s нет активной подписки для продления",
            user.telegram_id,
        )
        return None

    saved_subscription_id = cart_data.get("subscription_id")
    if saved_subscription_id is not None:
        saved_subscription_id = _safe_int(saved_subscription_id, subscription.id)
        if saved_subscription_id != subscription.id:
            logger.warning(
                "🔁 Автопокупка: сохранённая подписка %s не совпадает с текущей %s у пользователя %s",
                saved_subscription_id,
                subscription.id,
                user.telegram_id,
            )
            return None

    period_days = _safe_int(cart_data.get("period_days"))
    price_kopeks = _safe_int(
        cart_data.get("total_price")
        or cart_data.get("price")
        or cart_data.get("final_price"),
    )

    if period_days <= 0:
        logger.warning(
            "🔁 Автопокупка: некорректное количество дней продления (%s) у пользователя %s",
            period_days,
            user.telegram_id,
        )
        return None

    if price_kopeks <= 0:
        logger.warning(
            "🔁 Автопокупка: некорректная цена продления (%s) у пользователя %s",
            price_kopeks,
            user.telegram_id,
        )
        return None

    description = cart_data.get("description") or f"Продление подписки на {period_days} дней"

    device_limit = cart_data.get("device_limit")
    if device_limit is not None:
        device_limit = _safe_int(device_limit, subscription.device_limit)

    traffic_limit_gb = cart_data.get("traffic_limit_gb")
    if traffic_limit_gb is not None:
        traffic_limit_gb = _safe_int(traffic_limit_gb, subscription.traffic_limit_gb or 0)

    squad_uuid = cart_data.get("squad_uuid")
    consume_promo_offer = bool(cart_data.get("consume_promo_offer"))

    return AutoExtendContext(
        subscription=subscription,
        period_days=period_days,
        price_kopeks=price_kopeks,
        description=description,
        device_limit=device_limit,
        traffic_limit_gb=traffic_limit_gb,
        squad_uuid=squad_uuid,
        consume_promo_offer=consume_promo_offer,
    )


def _apply_extension_updates(context: AutoExtendContext) -> None:
    """
    Применяет обновления лимитов подписки (трафик, устройства, серверы).
    НЕ изменяет is_trial - это делается позже после успешного коммита продления.
    """
    subscription = context.subscription

    # Обновляем лимиты для триальной подписки
    if subscription.is_trial:
        # НЕ удаляем триал здесь! Это будет сделано после успешного extend_subscription()
        # subscription.is_trial = False  # УДАЛЕНО: преждевременное удаление триала
        if context.traffic_limit_gb is not None:
            subscription.traffic_limit_gb = context.traffic_limit_gb
        if context.device_limit is not None:
            subscription.device_limit = max(subscription.device_limit, context.device_limit)
        if context.squad_uuid and context.squad_uuid not in (subscription.connected_squads or []):
            subscription.connected_squads = (subscription.connected_squads or []) + [context.squad_uuid]
    else:
        # Обновляем лимиты для платной подписки
        if context.traffic_limit_gb not in (None, 0):
            subscription.traffic_limit_gb = context.traffic_limit_gb
        if (
            context.device_limit is not None
            and context.device_limit > subscription.device_limit
        ):
            subscription.device_limit = context.device_limit
        if context.squad_uuid and context.squad_uuid not in (subscription.connected_squads or []):
            subscription.connected_squads = (subscription.connected_squads or []) + [context.squad_uuid]


async def _auto_extend_subscription(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Optional[Bot] = None,
) -> bool:
    try:
        prepared = await _prepare_auto_extend_context(db, user, cart_data)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "❌ Автопокупка: ошибка подготовки данных продления для пользователя %s: %s",
            user.telegram_id,
            error,
            exc_info=True,
        )
        return False

    if prepared is None:
        return False

    if user.balance_kopeks < prepared.price_kopeks:
        logger.info(
            "🔁 Автопокупка: у пользователя %s недостаточно средств для продления (%s < %s)",
            user.telegram_id,
            user.balance_kopeks,
            prepared.price_kopeks,
        )
        return False

    try:
        deducted = await subtract_user_balance(
            db,
            user,
            prepared.price_kopeks,
            prepared.description,
            consume_promo_offer=prepared.consume_promo_offer,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "❌ Автопокупка: ошибка списания средств при продлении пользователя %s: %s",
            user.telegram_id,
            error,
            exc_info=True,
        )
        return False

    if not deducted:
        logger.warning(
            "❌ Автопокупка: списание средств для продления подписки пользователя %s не выполнено",
            user.telegram_id,
        )
        return False

    subscription = prepared.subscription
    old_end_date = subscription.end_date
    was_trial = subscription.is_trial  # Запоминаем, была ли подписка триальной

    _apply_extension_updates(prepared)

    try:
        updated_subscription = await extend_subscription(
            db,
            subscription,
            prepared.period_days,
        )

        # НОВОЕ: Конвертируем триал в платную подписку ТОЛЬКО после успешного продления
        if was_trial and subscription.is_trial:
            subscription.is_trial = False
            subscription.status = "active"
            user.has_had_paid_subscription = True
            await db.commit()
            logger.info(
                "✅ Триал конвертирован в платную подписку %s для пользователя %s",
                subscription.id,
                user.telegram_id,
            )

    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "❌ Автопокупка: не удалось продлить подписку пользователя %s: %s",
            user.telegram_id,
            error,
            exc_info=True,
        )
        # НОВОЕ: Откатываем изменения при ошибке
        await db.rollback()
        return False

    transaction = None
    try:
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=prepared.price_kopeks,
            description=prepared.description,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "⚠️ Автопокупка: не удалось зафиксировать транзакцию продления для пользователя %s: %s",
            user.telegram_id,
            error,
            exc_info=True,
        )

    subscription_service = SubscriptionService()
    try:
        await subscription_service.update_remnawave_user(
            db,
            updated_subscription,
            reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
            reset_reason="продление подписки",
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "⚠️ Автопокупка: не удалось обновить RemnaWave пользователя %s после продления: %s",
            user.telegram_id,
            error,
        )

    await user_cart_service.delete_user_cart(user.id)
    await clear_subscription_checkout_draft(user.id)

    texts = get_texts(getattr(user, "language", "ru"))
    period_label = format_period_description(
        prepared.period_days,
        getattr(user, "language", "ru"),
    )
    new_end_date = updated_subscription.end_date
    end_date_label = format_local_datetime(new_end_date, "%d.%m.%Y %H:%M")

    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_extension_notification(
                db,
                user,
                updated_subscription,
                transaction,
                prepared.period_days,
                old_end_date,
                new_end_date=new_end_date,
                balance_after=user.balance_kopeks,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                "⚠️ Автопокупка: не удалось уведомить администраторов о продлении пользователя %s: %s",
                user.telegram_id,
                error,
            )

        try:
            auto_message = texts.t(
                "AUTO_PURCHASE_SUBSCRIPTION_EXTENDED",
                "✅ Subscription automatically extended for {period}.",
            ).format(period=period_label)
            details_message = texts.t(
                "AUTO_PURCHASE_SUBSCRIPTION_EXTENDED_DETAILS",
                "New expiration date: {date}.",
            ).format(date=end_date_label)
            hint_message = texts.t(
                "AUTO_PURCHASE_SUBSCRIPTION_HINT",
                "Open the ‘My subscription’ section to access your link.",
            )

            full_message = "\n\n".join(
                part.strip()
                for part in [auto_message, details_message, hint_message]
                if part and part.strip()
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t("MY_SUBSCRIPTION_BUTTON", "📱 My subscription"),
                            callback_data="menu_subscription",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t("BACK_TO_MAIN_MENU_BUTTON", "🏠 Main menu"),
                            callback_data="back_to_menu",
                        )
                    ],
                ]
            )

            await bot.send_message(
                chat_id=user.telegram_id,
                text=full_message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                "⚠️ Автопокупка: не удалось уведомить пользователя %s о продлении: %s",
                user.telegram_id,
                error,
            )

    logger.info(
        "✅ Автопокупка: подписка продлена на %s дней для пользователя %s",
        prepared.period_days,
        user.telegram_id,
    )

    return True


async def _auto_add_devices(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Optional[Bot] = None,
) -> bool:
    """Автоматическое добавление устройств после пополнения баланса."""
    from app.database.crud.subscription import get_subscription_by_user_id

    device_count = _safe_int(cart_data.get("device_count"))
    current_devices = _safe_int(cart_data.get("current_devices"))
    price_kopeks = _safe_int(cart_data.get("total_price"))
    saved_subscription_id = cart_data.get("subscription_id")

    if device_count <= 0 or price_kopeks <= 0:
        logger.warning(
            "🔁 Автопокупка устройств: некорректные параметры (devices=%s, price=%s) у пользователя %s",
            device_count, price_kopeks, user.telegram_id,
        )
        return False

    subscription = await get_subscription_by_user_id(db, user.id)
    if subscription is None:
        logger.info(
            "🔁 Автопокупка устройств: у пользователя %s нет активной подписки",
            user.telegram_id,
        )
        return False

    if saved_subscription_id is not None:
        if _safe_int(saved_subscription_id, subscription.id) != subscription.id:
            logger.warning(
                "🔁 Автопокупка устройств: подписка %s не совпадает с текущей %s у пользователя %s",
                saved_subscription_id, subscription.id, user.telegram_id,
            )
            return False

    if user.balance_kopeks < price_kopeks:
        logger.info(
            "🔁 Автопокупка устройств: у пользователя %s недостаточно средств (%s < %s)",
            user.telegram_id, user.balance_kopeks, price_kopeks,
        )
        return False

    try:
        deducted = await subtract_user_balance(
            db, user, price_kopeks,
            f"Изменение количества устройств с {current_devices} до {device_count}",
        )
    except Exception as error:
        logger.error(
            "❌ Автопокупка устройств: ошибка списания средств у пользователя %s: %s",
            user.telegram_id, error, exc_info=True,
        )
        return False

    if not deducted:
        return False

    old_device_limit = subscription.device_limit
    subscription.device_limit = device_count

    try:
        from datetime import datetime
        subscription.updated_at = datetime.utcnow()
        await db.commit()
    except Exception as error:
        logger.error(
            "❌ Автопокупка устройств: ошибка коммита для пользователя %s: %s",
            user.telegram_id, error, exc_info=True,
        )
        await db.rollback()
        return False

    try:
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=f"Изменение устройств с {current_devices} до {device_count} (автопокупка)",
        )
    except Exception as error:
        logger.error(
            "⚠️ Автопокупка устройств: не удалось создать транзакцию для пользователя %s: %s",
            user.telegram_id, error, exc_info=True,
        )

    subscription_service = SubscriptionService()
    try:
        await subscription_service.update_remnawave_user(db, subscription)
    except Exception as error:
        logger.error(
            "⚠️ Автопокупка устройств: ошибка обновления RemnaWave для пользователя %s: %s",
            user.telegram_id, error,
        )

    await user_cart_service.delete_user_cart(user.id)

    texts = get_texts(getattr(user, "language", "ru"))

    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_update_notification(
                db, user, subscription, "devices", old_device_limit, device_count, price_kopeks
            )
        except Exception as error:
            logger.error(
                "⚠️ Автопокупка устройств: ошибка уведомления админов для пользователя %s: %s",
                user.telegram_id, error,
            )

        try:
            success_text = (
                "✅ Количество устройств увеличено!\n\n"
                f"📱 Было: {old_device_limit} → Стало: {device_count}\n"
                f"💰 Списано: {texts.format_price(price_kopeks)}"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text=texts.t("MY_SUBSCRIPTION_BUTTON", "📱 My subscription"),
                        callback_data="menu_subscription",
                    )],
                    [InlineKeyboardButton(
                        text=texts.t("BACK_TO_MAIN_MENU_BUTTON", "🏠 Main menu"),
                        callback_data="back_to_menu",
                    )],
                ]
            )
            await bot.send_message(
                chat_id=user.telegram_id,
                text=success_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error(
                "⚠️ Автопокупка устройств: ошибка уведомления пользователя %s: %s",
                user.telegram_id, error,
            )

    logger.info(
        "✅ Автопокупка устройств: %s → %s для пользователя %s",
        old_device_limit, device_count, user.telegram_id,
    )
    return True


async def auto_purchase_saved_cart_after_topup(
    db: AsyncSession,
    user: User,
    *,
    bot: Optional[Bot] = None,
) -> bool:
    """Attempts to automatically purchase a subscription from a saved cart."""

    if not user or not getattr(user, "id", None):
        return False

    cart_data = await user_cart_service.get_user_cart(user.id)
    if not cart_data:
        if not settings.is_auto_purchase_after_topup_enabled():
            return False
        return False

    # Явные intent-корзины (пользователь нажал "оплатить") обрабатываем всегда,
    # даже если AUTO_PURCHASE_AFTER_TOPUP_ENABLED выключен
    is_intent = cart_data.get("intent", False)
    if not is_intent and not settings.is_auto_purchase_after_topup_enabled():
        return False

    logger.info(
        "🔁 Автопокупка: обнаружена сохранённая корзина у пользователя %s (intent=%s)",
        user.telegram_id,
        is_intent,
    )

    cart_mode = cart_data.get("cart_mode") or cart_data.get("mode")
    if cart_mode == "extend":
        return await _auto_extend_subscription(db, user, cart_data, bot=bot)
    elif cart_mode == "add_devices":
        return await _auto_add_devices(db, user, cart_data, bot=bot)

    try:
        prepared = await _prepare_auto_purchase(db, user, cart_data)
    except PurchaseValidationError as error:
        logger.error(
            "❌ Автопокупка: ошибка валидации корзины пользователя %s: %s",
            user.telegram_id,
            error,
        )
        return False
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "❌ Автопокупка: непредвиденная ошибка при подготовке корзины %s: %s",
            user.telegram_id,
            error,
            exc_info=True,
        )
        return False

    if prepared is None:
        return False

    pricing = prepared.pricing
    selection = prepared.selection

    if pricing.final_total <= 0:
        logger.warning(
            "❌ Автопокупка: итоговая сумма для пользователя %s некорректна (%s)",
            user.telegram_id,
            pricing.final_total,
        )
        return False

    if user.balance_kopeks < pricing.final_total:
        logger.info(
            "🔁 Автопокупка: у пользователя %s недостаточно средств (%s < %s)",
            user.telegram_id,
            user.balance_kopeks,
            pricing.final_total,
        )
        return False

    purchase_service = prepared.service

    try:
        purchase_result = await purchase_service.submit_purchase(
            db,
            prepared.context,
            pricing,
        )
    except PurchaseBalanceError:
        logger.info(
            "🔁 Автопокупка: баланс пользователя %s изменился и стал недостаточным",
            user.telegram_id,
        )
        return False
    except PurchaseValidationError as error:
        logger.error(
            "❌ Автопокупка: не удалось подтвердить корзину пользователя %s: %s",
            user.telegram_id,
            error,
        )
        return False
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            "❌ Автопокупка: ошибка оформления подписки для пользователя %s: %s",
            user.telegram_id,
            error,
            exc_info=True,
        )
        return False

    await user_cart_service.delete_user_cart(user.id)
    await clear_subscription_checkout_draft(user.id)

    subscription = purchase_result.get("subscription")
    transaction = purchase_result.get("transaction")
    was_trial_conversion = purchase_result.get("was_trial_conversion", False)
    texts = get_texts(getattr(user, "language", "ru"))

    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_purchase_notification(
                db,
                user,
                subscription,
                transaction,
                selection.period.days,
                was_trial_conversion,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                "⚠️ Автопокупка: не удалось отправить уведомление админам (%s): %s",
                user.telegram_id,
                error,
            )

        try:
            period_label = format_period_description(
                selection.period.days,
                getattr(user, "language", "ru"),
            )
            auto_message = texts.t(
                "AUTO_PURCHASE_SUBSCRIPTION_SUCCESS",
                "✅ Subscription purchased automatically after balance top-up ({period}).",
            ).format(period=period_label)

            hint_message = texts.t(
                "AUTO_PURCHASE_SUBSCRIPTION_HINT",
                "Open the ‘My subscription’ section to access your link.",
            )

            purchase_message = purchase_result.get("message", "")
            full_message = "\n\n".join(
                part.strip()
                for part in [auto_message, purchase_message, hint_message]
                if part and part.strip()
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t("MY_SUBSCRIPTION_BUTTON", "📱 My subscription"),
                            callback_data="menu_subscription",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t("BACK_TO_MAIN_MENU_BUTTON", "🏠 Main menu"),
                            callback_data="back_to_menu",
                        )
                    ],
                ]
            )

            await bot.send_message(
                chat_id=user.telegram_id,
                text=full_message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                "⚠️ Автопокупка: не удалось уведомить пользователя %s: %s",
                user.telegram_id,
                error,
            )

    logger.info(
        "✅ Автопокупка: подписка на %s дней оформлена для пользователя %s",
        selection.period.days,
        user.telegram_id,
    )

    return True


__all__ = ["auto_purchase_saved_cart_after_topup"]
