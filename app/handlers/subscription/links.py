import base64
import json
import logging
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

async def handle_connect_subscription(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    subscription_link = get_display_subscription_link(subscription)
    hide_subscription_link = settings.should_hide_subscription_link()

    if not subscription_link:
        await callback.answer(
            texts.t(
                "SUBSCRIPTION_NO_ACTIVE_LINK",
                "⚠ У вас нет активной подписки или ссылка еще генерируется",
            ),
            show_alert=True,
        )
        return

    connect_mode = settings.CONNECT_BUTTON_MODE

    if connect_mode == "miniapp_subscription":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    web_app=types.WebAppInfo(url=subscription_link)
                )
            ],
            [
                InlineKeyboardButton(text=texts.BACK, callback_data="subscription")
            ]
        ])

        await callback.message.edit_text(
            texts.t(
                "SUBSCRIPTION_CONNECT_MINIAPP_MESSAGE",
                """📱 <b>Подключить подписку</b>

🚀 Нажмите кнопку ниже, чтобы открыть подписку в мини-приложении Telegram:""",
            ),
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    elif connect_mode == "miniapp_custom":
        if not settings.MINIAPP_CUSTOM_URL:
            await callback.answer(
                texts.t(
                    "CUSTOM_MINIAPP_URL_NOT_SET",
                    "⚠ Кастомная ссылка для мини-приложения не настроена",
                ),
                show_alert=True,
            )
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL)
                )
            ],
            [
                InlineKeyboardButton(text=texts.BACK, callback_data="subscription")
            ]
        ])

        await callback.message.edit_text(
            texts.t(
                "SUBSCRIPTION_CONNECT_CUSTOM_MESSAGE",
                """🚀 <b>Подключить подписку</b>

📱 Нажмите кнопку ниже, чтобы открыть приложение:""",
            ),
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    elif connect_mode == "link":
        rows = [
            [
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    url=subscription_link
                )
            ]
        ]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append([
            InlineKeyboardButton(text=texts.BACK, callback_data="subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

        await callback.message.edit_text(
            texts.t(
                "SUBSCRIPTION_CONNECT_LINK_MESSAGE",
                """🚀 <b>Подключить подписку</b>",

🔗 Нажмите кнопку ниже, чтобы открыть ссылку подписки:""",
            ),
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    elif connect_mode == "happ_cryptolink":
        rows = [
            [
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    callback_data="open_subscription_link",
                )
            ]
        ]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append([
            InlineKeyboardButton(text=texts.BACK, callback_data="subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

        await callback.message.edit_text(
            texts.t(
                "SUBSCRIPTION_CONNECT_LINK_MESSAGE",
                """🚀 <b>Подключить подписку</b>",

🔗 Нажмите кнопку ниже, чтобы открыть ссылку подписки:""",
            ),
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    else:
        from app.utils.subscription_utils import (
            get_happ_cryptolink_redirect_link,
            convert_subscription_link_to_happ_scheme,
        )

        redirect_link = get_happ_cryptolink_redirect_link(subscription_link)
        happ_scheme_link = convert_subscription_link_to_happ_scheme(subscription_link)
        connect_url = redirect_link or happ_scheme_link or subscription_link

        happ_message = texts.t(
            "SUBSCRIPTION_HAPP_CONNECT_MESSAGE",
            """🚀 <b>Подключить подписку</b>

🔗 Нажмите кнопку ниже, чтобы открыть Happ и добавить подписку автоматически.""",
        )

        if not hide_subscription_link:
            happ_message += "\n\n" + texts.t(
                "SUBSCRIPTION_HAPP_MANUAL_HINT",
                "💡 Если не получается, скопируйте ссылку вручную:",
            )
            happ_message += f"\n\n<code>{subscription_link}</code>"

        rows = [
            [
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    url=connect_url,
                )
            ]
        ]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append([
            InlineKeyboardButton(text=texts.BACK, callback_data="subscription")
        ])

        await callback.message.edit_text(
            happ_message,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML"
        )

    await callback.answer()

async def handle_open_subscription_link(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    subscription_link = get_display_subscription_link(subscription)

    if not subscription_link:
        await callback.answer(
            texts.t("SUBSCRIPTION_LINK_UNAVAILABLE", "❌ Ссылка подписки недоступна"),
            show_alert=True,
        )
        return

    if settings.is_happ_cryptolink_mode():
        redirect_link = get_happ_cryptolink_redirect_link(subscription_link)
        happ_scheme_link = convert_subscription_link_to_happ_scheme(subscription_link)
        happ_message = (
                texts.t(
                    "SUBSCRIPTION_HAPP_OPEN_TITLE",
                    "🔗 <b>Подключение через Happ</b>",
                )
                + "\n\n"
                + texts.t(
            "SUBSCRIPTION_HAPP_OPEN_LINK",
            "<a href=\"{subscription_link}\">🔓 Открыть ссылку в Happ</a>",
        ).format(subscription_link=happ_scheme_link)
                + "\n\n"
                + texts.t(
            "SUBSCRIPTION_HAPP_OPEN_HINT",
            "💡 Если ссылка не открывается автоматически, скопируйте её вручную:",
        )
        )

        if redirect_link:
            happ_message += "\n\n" + texts.t(
                "SUBSCRIPTION_HAPP_OPEN_BUTTON_HINT",
                "▶️ Нажмите кнопку \"Подключиться\" ниже, чтобы открыть Happ и добавить подписку автоматически.",
            )

        happ_message += "\n\n" + texts.t(
            "SUBSCRIPTION_HAPP_CRYPTOLINK_BLOCK",
            "<blockquote expandable><code>{crypto_link}</code></blockquote>",
        ).format(crypto_link=subscription_link)

        keyboard = get_happ_cryptolink_keyboard(
            subscription_link,
            db_user.language,
            redirect_link=redirect_link,
        )

        await callback.message.answer(
            happ_message,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    link_text = (
            texts.t("SUBSCRIPTION_DEVICE_LINK_TITLE", "🔗 <b>Ссылка подписки:</b>")
            + "\n\n"
            + f"<code>{subscription_link}</code>\n\n"
            + texts.t("SUBSCRIPTION_LINK_USAGE_TITLE", "📱 <b>Как использовать:</b>")
            + "\n"
            + "\n".join(
        [
            texts.t(
                "SUBSCRIPTION_LINK_STEP1",
                "1. Нажмите на ссылку выше чтобы её скопировать",
            ),
            texts.t(
                "SUBSCRIPTION_LINK_STEP2",
                "2. Откройте ваше VPN приложение",
            ),
            texts.t(
                "SUBSCRIPTION_LINK_STEP3",
                "3. Найдите функцию \"Добавить подписку\" или \"Import\"",
            ),
            texts.t(
                "SUBSCRIPTION_LINK_STEP4",
                "4. Вставьте скопированную ссылку",
            ),
        ]
    )
            + "\n\n"
            + texts.t(
        "SUBSCRIPTION_LINK_HINT",
        "💡 Если ссылка не скопировалась, выделите её вручную и скопируйте.",
    )
    )

    await callback.message.edit_text(
        link_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                                     callback_data="subscription_connect")
            ],
            [
                InlineKeyboardButton(text=texts.BACK, callback_data="subscription")
            ]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()
