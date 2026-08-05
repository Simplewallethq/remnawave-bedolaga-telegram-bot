import base64
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple, Optional
from urllib.parse import quote
from aiogram import Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings, PERIOD_PRICES, get_traffic_prices
from app.database.crud.discount_offer import (
    get_offer_by_id,
    mark_offer_claimed,
)
from app.database.crud.promo_offer_template import get_promo_offer_template_by_id
from app.database.crud.subscription import (
    create_trial_subscription,
    create_paid_subscription, add_subscription_traffic, add_subscription_devices,
    update_subscription_autopay
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import (
    User, TransactionType, SubscriptionStatus,
    Subscription
)
from app.keyboards.inline import (
    get_subscription_keyboard, get_trial_keyboard,
    get_subscription_period_keyboard, get_traffic_packages_keyboard,
    get_countries_keyboard, get_devices_keyboard,
    get_subscription_confirm_keyboard, get_autopay_keyboard,
    get_autopay_days_keyboard, get_back_keyboard,
    get_add_traffic_keyboard,
    get_change_devices_keyboard, get_reset_traffic_confirm_keyboard,
    get_manage_countries_keyboard,
    get_device_selection_keyboard, get_connection_guide_keyboard,
    get_app_selection_keyboard, get_specific_app_keyboard,
    get_updated_subscription_settings_keyboard, get_insufficient_balance_keyboard,
    get_extend_subscription_keyboard_with_prices, get_confirm_change_devices_keyboard,
    get_devices_management_keyboard, get_device_management_help_keyboard,
    get_happ_cryptolink_keyboard,
    get_happ_download_platform_keyboard, get_happ_download_link_keyboard,
    get_happ_download_button_row,
    get_payment_methods_keyboard_with_cart,
    get_subscription_confirm_keyboard_with_cart,
    get_insufficient_balance_keyboard_with_cart
)
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_checkout_service import (
    clear_subscription_checkout_draft,
    get_subscription_checkout_draft,
    save_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.services.promo_offer_service import promo_offer_service
from app.states import SubscriptionStates
from app.utils.pagination import paginate_list
from app.utils.pricing_utils import (
    calculate_months_from_days,
    get_remaining_months,
    calculate_prorated_price,
    validate_pricing_calculation,
    format_period_description,
    apply_percentage_discount,
)
from app.utils.subscription_utils import (
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
    convert_subscription_link_to_happ_scheme,
)
from app.utils.promo_offer import (
    build_promo_offer_hint,
    get_user_active_promo_discount_percent,
)
from app.utils.photo_message import edit_or_answer_photo

from .countries import _get_available_countries, _should_show_countries_management
from .pricing import _build_subscription_period_prompt

PAY_IMAGE_PATH = os.path.join("images", "pay.jpg")
PLANS_IMAGE_PATH = os.path.join("images", "plans.jpg")

async def handle_autopay_menu(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    if not subscription:
        await callback.answer(
            texts.t("SUBSCRIPTION_ACTIVE_REQUIRED", "⚠️ У вас нет активной подписки!"),
            show_alert=True,
        )
        return

    status = (
        texts.t("AUTOPAY_STATUS_ENABLED", "включен")
        if subscription.autopay_enabled
        else texts.t("AUTOPAY_STATUS_DISABLED", "выключен")
    )
    days = subscription.autopay_days_before

    text = texts.t(
        "AUTOPAY_MENU_TEXT",
        (
            "💳 <b>Автоплатеж</b>\n\n"
            "📊 <b>Статус:</b> {status}\n"
            "⏰ <b>Списание за:</b> {days} дн. до окончания\n\n"
            "Выберите действие:"
        ),
    ).format(status=status, days=days)

    await edit_or_answer_photo(
        callback,
        text,
        get_autopay_keyboard(db_user.language),
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()

async def toggle_autopay(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    subscription = db_user.subscription
    enable = callback.data == "autopay_enable"

    await update_subscription_autopay(db, subscription, enable)

    texts = get_texts(db_user.language)
    status = (
        texts.t("AUTOPAY_STATUS_ENABLED", "включен")
        if enable
        else texts.t("AUTOPAY_STATUS_DISABLED", "выключен")
    )
    await callback.answer(
        texts.t("AUTOPAY_TOGGLE_SUCCESS", "✅ Автоплатеж {status}!").format(status=status)
    )

    await handle_autopay_menu(callback, db_user, db)

async def show_autopay_days(
        callback: types.CallbackQuery,
        db_user: User
):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            "AUTOPAY_SELECT_DAYS_PROMPT",
            "⏰ Выберите за сколько дней до окончания списывать средства:",
        ),
        reply_markup=get_autopay_days_keyboard(db_user.language)
    )
    await callback.answer()

async def set_autopay_days(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    days = int(callback.data.split('_')[2])
    subscription = db_user.subscription

    await update_subscription_autopay(
        db, subscription, subscription.autopay_enabled, days
    )

    texts = get_texts(db_user.language)
    await callback.answer(
        texts.t("AUTOPAY_DAYS_SET", "✅ Установлено {days} дней!").format(days=days)
    )

    await handle_autopay_menu(callback, db_user, db)

async def handle_subscription_config_back(
        callback: types.CallbackQuery,
        state: FSMContext,
        db_user: User,
        db: AsyncSession
):
    current_state = await state.get_state()
    texts = get_texts(db_user.language)

    if current_state == SubscriptionStates.selecting_traffic.state:
        await edit_or_answer_photo(
            callback,
            await _build_subscription_period_prompt(db_user, texts, db),
            get_subscription_period_keyboard(db_user.language, db_user),
            photo_path=PLANS_IMAGE_PATH if os.path.exists(PLANS_IMAGE_PATH) else None,
        )
        await state.set_state(SubscriptionStates.selecting_period)

    elif current_state == SubscriptionStates.selecting_countries.state:
        if settings.is_traffic_selectable():
            await callback.message.edit_text(
                texts.SELECT_TRAFFIC,
                reply_markup=get_traffic_packages_keyboard(db_user.language)
            )
            await state.set_state(SubscriptionStates.selecting_traffic)
        else:
            await edit_or_answer_photo(
                callback,
                await _build_subscription_period_prompt(db_user, texts, db),
                get_subscription_period_keyboard(db_user.language, db_user),
                photo_path=PLANS_IMAGE_PATH if os.path.exists(PLANS_IMAGE_PATH) else None,
            )
            await state.set_state(SubscriptionStates.selecting_period)

    elif current_state == SubscriptionStates.selecting_devices.state:
        await _show_previous_configuration_step(callback, state, db_user, texts, db)

    elif current_state == SubscriptionStates.confirming_purchase.state:
        if settings.is_devices_selection_enabled():
            data = await state.get_data()
            selected_devices = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)

            await callback.message.edit_text(
                texts.SELECT_DEVICES,
                reply_markup=get_devices_keyboard(selected_devices, db_user.language)
            )
            await state.set_state(SubscriptionStates.selecting_devices)
        else:
            await _show_previous_configuration_step(callback, state, db_user, texts, db)

    else:
        from app.handlers.menu import show_main_menu
        await show_main_menu(callback, db_user, db)
        await state.clear()

    await callback.answer()

async def handle_subscription_cancel(
        callback: types.CallbackQuery,
        state: FSMContext,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)

    await state.clear()
    await clear_subscription_checkout_draft(db_user.id)

    # Удаляем сохраненную корзину, чтобы не показывать кнопку возврата
    await user_cart_service.delete_user_cart(db_user.id)

    from app.handlers.menu import show_main_menu
    await show_main_menu(callback, db_user, db)

    await callback.answer("❌ Покупка отменена")
async def _show_previous_configuration_step(
        callback: types.CallbackQuery,
        state: FSMContext,
        db_user: User,
        texts,
        db: AsyncSession,
):
    if await _should_show_countries_management(db_user):
        countries = await _get_available_countries(db_user.promo_group_id)
        data = await state.get_data()
        selected_countries = data.get('countries', [])

        await callback.message.edit_text(
            texts.SELECT_COUNTRIES,
            reply_markup=get_countries_keyboard(countries, selected_countries, db_user.language)
        )
        await state.set_state(SubscriptionStates.selecting_countries)
        return

    if settings.is_traffic_selectable():
        await callback.message.edit_text(
            texts.SELECT_TRAFFIC,
            reply_markup=get_traffic_packages_keyboard(db_user.language)
        )
        await state.set_state(SubscriptionStates.selecting_traffic)
        return

    await edit_or_answer_photo(
        callback,
        await _build_subscription_period_prompt(db_user, texts, db),
        get_subscription_period_keyboard(db_user.language, db_user),
        photo_path=PLANS_IMAGE_PATH if os.path.exists(PLANS_IMAGE_PATH) else None,
    )
    await state.set_state(SubscriptionStates.selecting_period)
