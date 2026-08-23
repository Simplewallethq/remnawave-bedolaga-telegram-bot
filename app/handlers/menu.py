import html
import logging
import re
from decimal import Decimal
from typing import Dict, List
from aiogram import Dispatcher, types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import os

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id, update_user
from app.database.crud.promo_group import (
    get_auto_assign_promo_groups,
    has_auto_assign_promo_groups,
)
from app.database.crud.transaction import get_user_total_spent_kopeks
from app.keyboards.inline import (
    get_main_menu_keyboard,
    get_main_menu_keyboard_async,
    get_language_selection_keyboard,
    get_info_menu_keyboard,
    get_new_main_menu_keyboard,
    get_connection_keyboard,
    get_profile_keyboard,
    get_referral_keyboard,
    get_balance_keyboard,
    get_support_keyboard,
    get_onboarding_device_selection_keyboard,
    get_onboarding_connection_keyboard,
    get_onboarding_connected_keyboard,
    get_connection_platform_keyboard,
    get_connect_android_keyboard,
    get_connect_apple_keyboard,
    get_connect_windows_keyboard,
)
from app.utils.subscription_utils import (
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
    get_incy_button_url,
    get_raw_subscription_link,
    is_ios_device_type,
)
from app.utils.access_keys import (
    build_access_key_section,
    format_copyable_code,
)
from app.utils.bot_registry import is_primary_bot
from app.database.crud.referral import get_user_referral_stats
from app.database.crud.transaction import get_user_transactions
from app.database.models import TransactionType
from app.localization.texts import get_texts, get_rules, format_support_placeholders
from app.database.models import PromoGroup, User
from app.database.crud.user_message import get_random_active_message
from app.services.subscription_checkout_service import (
    has_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.utils.photo_message import edit_or_answer_photo
from app.services.support_settings_service import SupportSettingsService
from app.services.main_menu_button_service import MainMenuButtonService
from app.services.user_cart_service import user_cart_service
from app.utils.promo_offer import (
    build_promo_offer_hint,
    build_test_access_hint,
)
from app.services.privacy_policy_service import PrivacyPolicyService
from app.services.public_offer_service import PublicOfferService
from app.services.faq_service import FaqService
from app.utils.timezone import format_local_datetime
from app.utils.pricing_utils import format_period_description
from app.utils.user_utils import get_effective_referral_commission_percent
from app.handlers.subscription.traffic import handle_add_traffic, add_traffic

logger = logging.getLogger(__name__)


MAIN_MENU_TEXT_CUSTOM_EMOJI_IDS = {
    "active": "5416081784641168838",
    "inactive": "5411225014148014586",
    "channel": "5282843764451195532",
}


def _main_menu_custom_emoji(kind: str, fallback: str) -> str:
    return (
        f'<tg-emoji emoji-id="{MAIN_MENU_TEXT_CUSTOM_EMOJI_IDS[kind]}">'
        f"{fallback}</tg-emoji>"
    )


def _decorate_main_menu_text(
    text: str,
    subscription_state: str,
    *,
    use_premium_emoji: bool,
) -> str:
    """Apply the primary bot's premium emoji and status emphasis."""
    for label in ("Подписка:", "Subscription:"):
        if label in text and f"<b>{label}</b>" not in text:
            text = text.replace(label, f"<b>{label}</b>", 1)

    if use_premium_emoji:
        if subscription_state in {"trial", "active"}:
            text = text.replace("🟢", _main_menu_custom_emoji("active", "🟢"), 1)
        elif subscription_state == "inactive":
            text = text.replace("🔴", _main_menu_custom_emoji("inactive", "🔴"), 1)

        channel_emoji = _main_menu_custom_emoji("channel", "🖥")
        text = re.sub(
            r'<a\b[^>]*>\s*➡️?\s*</a>',
            channel_emoji,
            text,
            count=1,
        )
    return text


def _format_rubles(amount_kopeks: int) -> str:
    rubles = Decimal(amount_kopeks) / Decimal(100)

    if rubles == rubles.to_integral_value():
        formatted = f"{rubles:,.0f}"
    else:
        formatted = f"{rubles:,.2f}"

    return f"{formatted.replace(',', ' ')} ₽"


def _collect_period_discounts(group: PromoGroup) -> Dict[int, int]:
    discounts: Dict[int, int] = {}
    raw_discounts = getattr(group, "period_discounts", None)

    if isinstance(raw_discounts, dict):
        for key, value in raw_discounts.items():
            try:
                period = int(key)
                percent = int(value)
            except (TypeError, ValueError):
                continue

            normalized_percent = max(0, min(100, percent))
            if normalized_percent > 0:
                discounts[period] = normalized_percent

    if group.is_default and settings.is_base_promo_group_period_discount_enabled():
        try:
            base_discounts = settings.get_base_promo_group_period_discounts() or {}
        except Exception:
            base_discounts = {}

        for key, value in base_discounts.items():
            try:
                period = int(key)
                percent = int(value)
            except (TypeError, ValueError):
                continue

            if period in discounts:
                continue

            normalized_percent = max(0, min(100, percent))
            if normalized_percent > 0:
                discounts[period] = normalized_percent

    return dict(sorted(discounts.items()))


def _build_group_discount_lines(group: PromoGroup, texts, language: str) -> list[str]:
    lines: list[str] = []

    if getattr(group, "server_discount_percent", 0) > 0:
        lines.append(
            texts.t("PROMO_GROUP_DISCOUNT_SERVERS", "🌍 Серверы: {percent}%").format(
                percent=group.server_discount_percent
            )
        )

    if getattr(group, "traffic_discount_percent", 0) > 0:
        lines.append(
            texts.t("PROMO_GROUP_DISCOUNT_TRAFFIC", "📊 Трафик: {percent}%").format(
                percent=group.traffic_discount_percent
            )
        )

    if getattr(group, "device_discount_percent", 0) > 0:
        lines.append(
            texts.t("PROMO_GROUP_DISCOUNT_DEVICES", "📱 Доп. устройства: {percent}%").format(
                percent=group.device_discount_percent
            )
        )

    period_discounts = _collect_period_discounts(group)

    if period_discounts:
        lines.append(
            texts.t(
                "PROMO_GROUP_PERIOD_DISCOUNTS_HEADER",
                "⏳ Скидки за длительный период:",
            )
        )

        for period_days, percent in period_discounts.items():
            lines.append(
                texts.t(
                    "PROMO_GROUP_PERIOD_DISCOUNT_ITEM",
                    "{period} — {percent}%",
                ).format(
                    period=format_period_description(period_days, language),
                    percent=percent,
                )
            )

    return lines


async def show_main_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    *,
    skip_callback_answer: bool = False,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    db_user.last_activity = datetime.utcnow()
    await db.commit()

    menu_text = await get_main_menu_text(
        db_user,
        texts,
        db,
        use_premium_emoji=is_primary_bot(callback.bot.id if callback.bot else None),
    )

    # Determine status for keyboard
    subscription = db_user.subscription
    is_active = subscription and subscription.is_active
    is_trial = subscription and getattr(subscription, "is_trial", False)
    
    trial_active = bool(is_active and is_trial)
    has_active_subscription = bool(is_active and not is_trial)
    
    # Logic: if subscription is not None, trial is considered "used" or currently handled.
    # We might want a more robust check for "has used trial" in the past if sub is currently None.
    # But for now:
    trial_used = (subscription is not None or db_user.has_had_paid_subscription)

    # Проверяем, является ли пользователь администратором
    is_admin = settings.is_admin(db_user.telegram_id)

    keyboard = get_new_main_menu_keyboard(
        balance_rub=db_user.balance_kopeks / 100,
        trial_used=trial_used,
        trial_active=trial_active,
        has_active_subscription=has_active_subscription,
        is_admin=is_admin,
        language=db_user.language,
        use_premium_emoji=is_primary_bot(callback.bot.id if callback.bot else None),
    )

    image_path = os.path.join("images", "main_menu.webp")
    if not os.path.exists(image_path):
        image_path = None

    await edit_or_answer_photo(
        callback=callback,
        caption=menu_text,
        keyboard=keyboard,
        parse_mode="HTML",
        force_text=settings.is_text_main_menu_mode(),
        photo_path=image_path,
        disable_web_page_preview=True,
    )
    if not skip_callback_answer:
        await callback.answer()


async def handle_profile_unavailable(callback: types.CallbackQuery) -> None:
    language = getattr(callback.from_user, "language_code", None) or settings.DEFAULT_LANGUAGE
    try:
        texts = get_texts(language)
    except Exception:
        texts = get_texts()

    await callback.answer(
        texts.t(
            "MENU_PROFILE_UNAVAILABLE",
            "❗️ Личный кабинет пока недоступен. Попробуйте позже.",
        ),
        show_alert=True,
    )


async def show_service_rules(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    from app.database.crud.rules import get_current_rules_content

    texts = get_texts(db_user.language)
    rules_text = await get_current_rules_content(db, db_user.language)

    if not rules_text:
        rules_text = await get_rules(db_user.language)

    await callback.message.edit_text(
        f"{texts.t('RULES_HEADER', '📋 <b>Правила сервиса</b>')}\n\n{rules_text}",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")]
        ])
    )
    await callback.answer()


async def show_info_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    header = texts.t("MENU_INFO_HEADER", "ℹ️ <b>Инфо</b>")
    prompt = texts.t("MENU_INFO_PROMPT", "Выберите раздел:")
    caption = f"{header}\n\n{prompt}" if prompt else header

    privacy_enabled = await PrivacyPolicyService.is_policy_enabled(db, db_user.language)
    public_offer_enabled = await PublicOfferService.is_offer_enabled(db, db_user.language)
    faq_enabled = await FaqService.is_enabled(db, db_user.language)
    promo_groups_available = await has_auto_assign_promo_groups(db)

    await edit_or_answer_photo(
        callback=callback,
        caption=caption,
        keyboard=get_info_menu_keyboard(
            language=db_user.language,
            show_privacy_policy=privacy_enabled,
            show_public_offer=public_offer_enabled,
            show_faq=faq_enabled,
            show_promo_groups=promo_groups_available,
        ),
        parse_mode="HTML",
        photo_path=(
            os.path.join("images", "info.webp")
            if os.path.exists(os.path.join("images", "info.webp"))
            else None
        ),
    )
    await callback.answer()


async def show_promo_groups_info(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    promo_groups = await get_auto_assign_promo_groups(db)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_info")]]
    )

    if not promo_groups:
        empty_text = texts.t(
            "PROMO_GROUPS_INFO_EMPTY",
            "Промогруппы с автовыдачей ещё не настроены.",
        )
        header = texts.t("PROMO_GROUPS_INFO_HEADER", "🎯 <b>Промогруппы</b>")
        message = f"{header}\n\n{empty_text}" if empty_text else header

        await callback.message.edit_text(
            message,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await callback.answer()
        return

    total_spent_kopeks = await get_user_total_spent_kopeks(db, db_user.id)
    total_spent_text = _format_rubles(total_spent_kopeks)

    sorted_groups = sorted(
        promo_groups,
        key=lambda group: (group.auto_assign_total_spent_kopeks or 0, group.id),
    )

    achieved_groups: List[PromoGroup] = [
        group
        for group in sorted_groups
        if (group.auto_assign_total_spent_kopeks or 0) > 0
        and total_spent_kopeks >= (group.auto_assign_total_spent_kopeks or 0)
    ]

    current_group = next(
        (group for group in sorted_groups if group.id == db_user.promo_group_id),
        None,
    )

    if not current_group and achieved_groups:
        current_group = achieved_groups[-1]

    next_group = next(
        (
            group
            for group in sorted_groups
            if (group.auto_assign_total_spent_kopeks or 0) > total_spent_kopeks
        ),
        None,
    )

    header = texts.t("PROMO_GROUPS_INFO_HEADER", "🎯 <b>Промогруппы</b>")
    lines: List[str] = [header, ""]

    spent_line = texts.t(
        "PROMO_GROUPS_INFO_TOTAL_SPENT",
        "💰 Потрачено в боте: {amount}",
    ).format(amount=total_spent_text)
    lines.append(spent_line)

    if current_group:
        lines.append(
            texts.t(
                "PROMO_GROUPS_INFO_CURRENT_LEVEL",
                "🏆 Текущий уровень: {name}",
            ).format(name=html.escape(current_group.name)),
        )
    else:
        lines.append(
            texts.t(
                "PROMO_GROUPS_INFO_NO_LEVEL",
                "🏆 Текущий уровень: пока не получен",
            )
        )

    if next_group:
        remaining_kopeks = (next_group.auto_assign_total_spent_kopeks or 0) - total_spent_kopeks
        lines.append(
            texts.t(
                "PROMO_GROUPS_INFO_NEXT_LEVEL",
                "📈 До уровня «{name}»: осталось {amount}",
            ).format(
                name=html.escape(next_group.name),
                amount=_format_rubles(max(remaining_kopeks, 0)),
            )
        )
    else:
        lines.append(
            texts.t(
                "PROMO_GROUPS_INFO_MAX_LEVEL",
                "🏆 Вы уже получили максимальный уровень скидок!",
            )
        )

    lines.extend(["", texts.t("PROMO_GROUPS_INFO_LEVELS_HEADER", "📋 Уровни с автовыдачей:")])

    for group in sorted_groups:
        threshold = group.auto_assign_total_spent_kopeks or 0
        status_icon = "✅" if total_spent_kopeks >= threshold else "🔒"
        lines.append(
            texts.t(
                "PROMO_GROUPS_INFO_LEVEL_LINE",
                "{status} <b>{name}</b> — от {amount}",
            ).format(
                status=status_icon,
                name=html.escape(group.name),
                amount=_format_rubles(threshold),
            )
        )

        discount_lines = _build_group_discount_lines(group, texts, db_user.language)
        for discount_line in discount_lines:
            if discount_line:
                lines.append(f"   {discount_line}")

        lines.append("")

    while lines and not lines[-1]:
        lines.pop()

    message_text = "\n".join(lines)

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


async def show_faq_pages(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    pages = await FaqService.get_pages(db, db_user.language)
    if not pages:
        await callback.answer(
            texts.t("FAQ_NOT_AVAILABLE", "FAQ временно недоступен."),
            show_alert=True,
        )
        return

    header = texts.t("FAQ_HEADER", "❓ <b>FAQ</b>")
    prompt = texts.t("FAQ_PAGES_PROMPT", "Выберите вопрос:" )
    caption = f"{header}\n\n{prompt}" if prompt else header

    buttons: list[list[types.InlineKeyboardButton]] = []
    for index, page in enumerate(pages, start=1):
        raw_title = (page.title or "").strip()
        if not raw_title:
            raw_title = texts.t("FAQ_PAGE_UNTITLED", "Без названия")
        if len(raw_title) > 60:
            raw_title = f"{raw_title[:57]}..."
        buttons.append([
            types.InlineKeyboardButton(
                text=f"{index}. {raw_title}",
                callback_data=f"menu_faq_page:{page.id}:1",
            )
        ])

    buttons.append([
        types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_info")
    ])

    await callback.message.edit_text(
        caption,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=settings.DISABLE_WEB_PAGE_PREVIEW,
    )
    await callback.answer()


async def show_faq_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    raw_data = callback.data or ""
    parts = raw_data.split(":")

    page_id = None
    requested_page = 1

    if len(parts) >= 2:
        try:
            page_id = int(parts[1])
        except ValueError:
            page_id = None

    if len(parts) >= 3:
        try:
            requested_page = int(parts[2])
        except ValueError:
            requested_page = 1

    if not page_id:
        await callback.answer()
        return

    page = await FaqService.get_page(db, page_id, db_user.language)

    if not page or not page.is_active:
        await callback.answer(
            texts.t("FAQ_PAGE_NOT_AVAILABLE", "Эта страница FAQ недоступна."),
            show_alert=True,
        )
        return

    content_pages = FaqService.split_content_into_pages(page.content)

    if not content_pages:
        await callback.answer(
            texts.t("FAQ_PAGE_EMPTY", "Текст для этой страницы ещё не добавлен."),
            show_alert=True,
        )
        return

    total_pages = len(content_pages)
    current_page = max(1, min(requested_page, total_pages))

    header = texts.t("FAQ_HEADER", "❓ <b>FAQ</b>")
    title_template = texts.t("FAQ_PAGE_TITLE", "<b>{title}</b>")
    page_title = (page.title or "").strip()
    if not page_title:
        page_title = texts.t("FAQ_PAGE_UNTITLED", "Без названия")
    title_block = title_template.format(title=html.escape(page_title))

    body = content_pages[current_page - 1]

    footer_template = texts.t(
        "FAQ_PAGE_FOOTER",
        "Страница {current} из {total}",
    )
    footer = ""
    if total_pages > 1 and footer_template:
        try:
            footer = footer_template.format(current=current_page, total=total_pages)
        except Exception:
            footer = f"{current_page}/{total_pages}"

    parts_to_join = [header, title_block]
    if body:
        parts_to_join.append(body)
    if footer:
        parts_to_join.append(f"<code>{footer}</code>")

    message_text = "\n\n".join(segment for segment in parts_to_join if segment)

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    if total_pages > 1:
        nav_row: list[types.InlineKeyboardButton] = []
        if current_page > 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text=texts.t("PAGINATION_PREV", "⬅️"),
                    callback_data=f"menu_faq_page:{page.id}:{current_page - 1}",
                )
            )

        nav_row.append(
            types.InlineKeyboardButton(
                text=f"{current_page}/{total_pages}",
                callback_data="noop",
            )
        )

        if current_page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(
                    text=texts.t("PAGINATION_NEXT", "➡️"),
                    callback_data=f"menu_faq_page:{page.id}:{current_page + 1}",
                )
            )

        keyboard_rows.append(nav_row)

    keyboard_rows.append([
        types.InlineKeyboardButton(
            text=texts.t("FAQ_BACK_TO_LIST", "⬅️ К списку FAQ"),
            callback_data="menu_faq",
        )
    ])
    keyboard_rows.append([
        types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_info")
    ])

    await callback.message.edit_text(
        message_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        disable_web_page_preview=settings.DISABLE_WEB_PAGE_PREVIEW,
    )
    await callback.answer()

async def show_privacy_policy(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    raw_page = 1
    if callback.data and ":" in callback.data:
        try:
            raw_page = int(callback.data.split(":", 1)[1])
        except ValueError:
            raw_page = 1

    if raw_page < 1:
        raw_page = 1

    policy = await PrivacyPolicyService.get_active_policy(db, db_user.language)

    if not policy:
        await callback.answer(
            texts.t(
                "PRIVACY_POLICY_NOT_AVAILABLE",
                "Политика конфиденциальности временно недоступна.",
            ),
            show_alert=True,
        )
        return

    pages = PrivacyPolicyService.split_content_into_pages(policy.content)

    if not pages:
        await callback.answer(
            texts.t(
                "PRIVACY_POLICY_EMPTY_ALERT",
                "Политика конфиденциальности ещё не заполнена.",
            ),
            show_alert=True,
        )
        return

    total_pages = len(pages)
    current_page = raw_page if raw_page <= total_pages else total_pages

    header = texts.t(
        "PRIVACY_POLICY_HEADER",
        "🛡️ <b>Политика конфиденциальности</b>",
    )
    body = pages[current_page - 1]

    footer_template = texts.t(
        "PRIVACY_POLICY_PAGE_INFO",
        "Страница {current} из {total}",
    )
    footer = ""
    if total_pages > 1 and footer_template:
        try:
            footer = footer_template.format(current=current_page, total=total_pages)
        except Exception:
            footer = f"{current_page}/{total_pages}"

    message_text = header
    if body:
        message_text += f"\n\n{body}"
    if footer:
        message_text += f"\n\n<code>{footer}</code>"

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    if total_pages > 1:
        nav_row: list[types.InlineKeyboardButton] = []
        if current_page > 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text=texts.t("PAGINATION_PREV", "⬅️"),
                    callback_data=f"menu_privacy_policy:{current_page - 1}",
                )
            )

        nav_row.append(
            types.InlineKeyboardButton(
                text=f"{current_page}/{total_pages}",
                callback_data="noop",
            )
        )

        if current_page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(
                    text=texts.t("PAGINATION_NEXT", "➡️"),
                    callback_data=f"menu_privacy_policy:{current_page + 1}",
                )
            )

        keyboard_rows.append(nav_row)

    keyboard_rows.append(
        [types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_info")]
    )

    await callback.message.edit_text(
        message_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        disable_web_page_preview=settings.DISABLE_WEB_PAGE_PREVIEW,
    )
    await callback.answer()


async def show_public_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    raw_page = 1
    if callback.data and ":" in callback.data:
        try:
            raw_page = int(callback.data.split(":", 1)[1])
        except ValueError:
            raw_page = 1

    if raw_page < 1:
        raw_page = 1

    offer = await PublicOfferService.get_active_offer(db, db_user.language)

    if not offer:
        await callback.answer(
            texts.t(
                "PUBLIC_OFFER_NOT_AVAILABLE",
                "Публичная оферта временно недоступна.",
            ),
            show_alert=True,
        )
        return

    pages = PublicOfferService.split_content_into_pages(offer.content)

    if not pages:
        await callback.answer(
            texts.t(
                "PUBLIC_OFFER_EMPTY_ALERT",
                "Публичная оферта ещё не заполнена.",
            ),
            show_alert=True,
        )
        return

    total_pages = len(pages)
    current_page = raw_page if raw_page <= total_pages else total_pages

    header = texts.t(
        "PUBLIC_OFFER_HEADER",
        "📄 <b>Публичная оферта</b>",
    )
    body = pages[current_page - 1]

    footer_template = texts.t(
        "PUBLIC_OFFER_PAGE_INFO",
        "Страница {current} из {total}",
    )
    footer = ""
    if total_pages > 1 and footer_template:
        try:
            footer = footer_template.format(current=current_page, total=total_pages)
        except Exception:
            footer = f"{current_page}/{total_pages}"

    message_text = header
    if body:
        message_text += f"\n\n{body}"
    if footer:
        message_text += f"\n\n<code>{footer}</code>"

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    if total_pages > 1:
        nav_row: list[types.InlineKeyboardButton] = []
        if current_page > 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text=texts.t("PAGINATION_PREV", "⬅️"),
                    callback_data=f"menu_public_offer:{current_page - 1}",
                )
            )

        nav_row.append(
            types.InlineKeyboardButton(
                text=f"{current_page}/{total_pages}",
                callback_data="noop",
            )
        )

        if current_page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(
                    text=texts.t("PAGINATION_NEXT", "➡️"),
                    callback_data=f"menu_public_offer:{current_page + 1}",
                )
            )

        keyboard_rows.append(nav_row)

    keyboard_rows.append(
        [types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_info")]
    )

    await callback.message.edit_text(
        message_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        disable_web_page_preview=settings.DISABLE_WEB_PAGE_PREVIEW,
    )
    await callback.answer()


async def show_language_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    if not settings.is_language_selection_enabled():
        await callback.answer(
            texts.t(
                "LANGUAGE_SELECTION_DISABLED",
                "⚙️ Выбор языка временно недоступен.",
            ),
            show_alert=True,
        )
        return

    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t("LANGUAGE_PROMPT", "🌐 Выберите язык интерфейса:"),
        keyboard=get_language_selection_keyboard(
            current_language=db_user.language,
            include_back=True,
            language=db_user.language,
        ),
        parse_mode="HTML",
        photo_path=(
            os.path.join("images", "profile.webp")
            if os.path.exists(os.path.join("images", "profile.webp"))
            else None
        ),
    )
    await callback.answer()


async def process_language_change(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if db_user is None:
        # Пользователь не найден, используем язык по умолчанию
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t(
                "USER_NOT_FOUND_ERROR",
                "Ошибка: пользователь не найден.",
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    if not settings.is_language_selection_enabled():
        await callback.answer(
            texts.t(
                "LANGUAGE_SELECTION_DISABLED",
                "⚙️ Выбор языка временно недоступен.",
            ),
            show_alert=True,
        )
        return

    selected_raw = (callback.data or "").split(":", 1)[-1]
    normalized_selected = selected_raw.strip().lower()

    available_map = {
        lang.strip().lower(): lang.strip()
        for lang in settings.get_available_languages()
        if isinstance(lang, str) and lang.strip()
    }

    if normalized_selected not in available_map:
        await callback.answer("❌ Unsupported language", show_alert=True)
        return

    resolved_language = available_map[normalized_selected].lower()

    if db_user.language.lower() == normalized_selected:
        await show_main_menu(
            callback,
            db_user,
            db,
            skip_callback_answer=True,
        )
        await callback.answer(texts.t("LANGUAGE_SELECTED", "🌐 Язык интерфейса обновлен."))
        return

    updated_user = await update_user(db, db_user, language=resolved_language)
    texts = get_texts(updated_user.language)

    await show_main_menu(
        callback,
        updated_user,
        db,
        skip_callback_answer=True,
    )
    await callback.answer(texts.t("LANGUAGE_SELECTED", "🌐 Язык интерфейса обновлен."))


async def handle_back_to_menu(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession
):
    if db_user is None:
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
             texts.t("USER_NOT_FOUND_ERROR", "Ошибка: пользователь не найден."),
             show_alert=True
        )
        return

    await state.clear()
    await show_main_menu(callback, db_user, db)

def _get_subscription_status(user: User, texts) -> str:
    subscription = getattr(user, "subscription", None)
    if not subscription:
        return texts.t("SUB_STATUS_NONE", "❌ Отсутствует")

    current_time = datetime.utcnow()
    actual_status = (subscription.actual_status or "").lower()
    end_date = getattr(subscription, "end_date", None)
    end_date_text = format_local_datetime(end_date, "%d.%m.%Y") if end_date else None
    days_left = 0

    if subscription.end_date > current_time:
        days_left = (subscription.end_date - current_time).days

    if actual_status == "pending":
        return texts.t("SUBSCRIPTION_NONE", "❌ Нет активной подписки")

    if actual_status == "disabled":
        return texts.t("SUB_STATUS_DISABLED", "⚫ Отключена")

    if actual_status == "expired":
        return texts.t(
            "SUB_STATUS_EXPIRED",
            "🔴 Истекла\n📅 {end_date}",
        ).format(end_date=end_date_text or "—")

    is_trial_subscription = getattr(subscription, "is_trial", False)

    is_trial_like_status = actual_status == "trial" or (
        is_trial_subscription and actual_status in {"active", "trial"}
    )

    if is_trial_like_status:
        return texts.t(
            "SUB_STATUS_TRIAL_ACTIVE",
            "🎁 Тестовая подписка\n📅 до {end_date} ({days} дн.)",
        ).format(
            end_date=end_date_text or "—",
            days=days_left,
        )

    if actual_status == "active":
        return texts.t(
            "SUB_STATUS_ACTIVE_LONG",
            "💎 Активна\n📅 до {end_date} ({days} дн.)",
        ).format(
            end_date=end_date_text or "—",
            days=days_left,
        )

    return texts.t("SUB_STATUS_UNKNOWN", "❓ Неизвестно")


def _insert_random_message(base_text: str, random_message: str, action_prompt: str) -> str:
    if not random_message:
        return base_text

    prompt = action_prompt or ""
    if prompt and prompt in base_text:
        parts = base_text.split(prompt, 1)
        if len(parts) == 2:
            return f"{parts[0]}\n{random_message}\n\n{prompt}{parts[1]}"
        return base_text.replace(prompt, f"\n{random_message}\n\n{prompt}", 1)

    return f"{base_text}\n\n{random_message}"


async def get_main_menu_text(
    user,
    texts,
    db: AsyncSession,
    *,
    use_premium_emoji: bool = False,
):
    subscription = user.subscription
    is_active = subscription and subscription.is_active
    is_trial = subscription and getattr(subscription, "is_trial", False)

    trial_active = is_active and is_trial
    has_active_subscription = is_active and not is_trial
    trial_used = (subscription is not None)

    base_text = ""
    subscription_state = "available"

    date_fmt = "%d.%m.%Y"

    if trial_active and subscription.end_date:
        subscription_state = "trial"
        end_str = format_local_datetime(subscription.end_date, date_fmt)
        days_left = max(0, (subscription.end_date - datetime.utcnow()).days)
        device_limit = getattr(subscription, "device_limit", 1) or 1
        base_text += texts.t(
            "MAIN_MENU_TRIAL_ACTIVE",
            "Подписка: 🟢Активна (пробная)\nдо {end_date} ({days} дн.)\n\nУстройств: {devices} шт."
        ).format(end_date=end_str, days=days_left, devices=device_limit)
    elif has_active_subscription and subscription.end_date:
        subscription_state = "active"
        end_str = format_local_datetime(subscription.end_date, date_fmt)
        days_left = max(0, (subscription.end_date - datetime.utcnow()).days)
        device_limit = getattr(subscription, "device_limit", 1) or 1
        base_text += texts.t(
            "MAIN_MENU_SUBSCRIPTION_ACTIVE",
            "Подписка: 🟢Активна\nдо {end_date} ({days} дн.)\n\nУстройств: {devices} шт."
        ).format(end_date=end_str, days=days_left, devices=device_limit)
    elif not trial_used:
        base_text += texts.t("MAIN_MENU_TRIAL_AVAILABLE", "Вам доступно 3 дня бесплатно 🎁")
    else:
        subscription_state = "inactive"
        base_text += texts.t("MAIN_MENU_NO_SUBSCRIPTION", "Подписка: 🔴Истекла")

    base_text += texts.t(
        "MAIN_MENU_CHANNEL_HINT",
        "\n\n<a href=\"https://t.me/vpnleto\">➡️</a> "
        "<a href=\"https://t.me/vpnleto\">Подпишись на наш канал</a> — там много интересного",
    )
    base_text += texts.t(
        "MAIN_MENU_LEGAL_LINKS",
        "\n\n<a href=\"https://telegra.ph/Politika-konfidencialnosti-07-20-101\">Политика конфиденциальности</a>"
        " | "
        "<a href=\"https://telegra.ph/Polzovatelskoe-soglashenie-07-20-32\">Пользовательское соглашение</a>",
    )

    return _decorate_main_menu_text(
        base_text,
        subscription_state,
        use_premium_emoji=use_premium_emoji,
    )


async def handle_activate_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    texts = get_texts(db_user.language)
    
    # Получить подписку пользователя
    from app.database.crud.subscription import get_subscription_by_user_id
    subscription = await get_subscription_by_user_id(db, db_user.id)
    
    if subscription and subscription.status == "ACTIVE" and subscription.end_date > datetime.utcnow():
        await callback.answer(
            texts.t("SUBSCRIPTION_ALREADY_ACTIVE", "✅ Подписка уже активна!"),
            show_alert=True,
        )
        return
    
    # Параметры из подписки или дефолтные
    device_limit = subscription.device_limit if subscription else settings.DEFAULT_DEVICE_LIMIT
    traffic_limit_gb = subscription.traffic_limit_gb if subscription else 0
    connected_squads = subscription.connected_squads if subscription else []
    
    # Получить IDs серверов из UUIDs
    from app.database.crud.server_squad import get_server_ids_by_uuids
    server_ids = await get_server_ids_by_uuids(db, connected_squads) if connected_squads else []
    
    balance = db_user.balance_kopeks
    available_periods = [int(p) for p in settings.AVAILABLE_SUBSCRIPTION_PERIODS]
    
    best_period = None
    best_price = 0
    
    from app.services.subscription_service import SubscriptionService
    subscription_service = SubscriptionService()
    
    # Найти максимальный период, цена которого <= баланса
    for period in sorted(available_periods, reverse=True):
        price, _ = await subscription_service.calculate_subscription_price_with_months(
            period,
            traffic_limit_gb,
            server_ids,
            device_limit,
            db,
            user=db_user
        )
        if price <= balance:
            best_period = period
            best_price = price
            break
    
    if best_period:
        # Создать новую подписку
        from app.database.crud.subscription import create_paid_subscription
        new_subscription = await create_paid_subscription(
            db,
            db_user.id,
            best_period,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit,
            connected_squads=connected_squads,
            update_server_counters=True
        )
        
        # Списать деньги
        db_user.balance_kopeks -= best_price
        await db.commit()
        from app.database.crud.subscription_event import record_subscription_purchase_event

        await record_subscription_purchase_event(
            db,
            user_id=db_user.id,
            subscription_id=new_subscription.id,
            amount_kopeks=best_price,
            period_days=best_period,
            was_trial_conversion=False,
            source="menu_activation",
            starts_at=new_subscription.start_date,
            ends_at=new_subscription.end_date,
        )

        await callback.answer(
            texts.t("ACTIVATION_SUCCESS", f"✅ Подписка активирована на {best_period} дней за {best_price//100} руб!"),
            show_alert=True,
        )
    else:
        await callback.answer(
            texts.t("INSUFFICIENT_FUNDS", "❌ Недостаточно средств для активации подписки"),
            show_alert=True,
        )



def _build_onboarding_device_selection_view(user) -> str:
    """Caption для экрана выбора платформы.

    Ключ подписки здесь не показываем — он доступен внутри платформенных
    инструкций (ручное подключение).
    """
    texts = get_texts(user.language)
    return texts.t(
        "ONBOARDING_DEVICE_SELECTION_TEXT",
        "Для подключения основного или дополнительного устройства выбери платформу:",
    )


def _get_connection_key(user: User) -> str | None:
    """Return the raw Remnawave key that works in Leto, Happ, and Incy."""
    return get_raw_subscription_link(getattr(user, "subscription", None))


def _format_connection_key(link: str) -> str:
    return format_copyable_code(link)


def _get_happ_transfer_url(user: User) -> str | None:
    return get_happ_cryptolink_redirect_link(
        get_display_subscription_link(getattr(user, "subscription", None))
    )


async def _detect_vpn_connection(db: AsyncSession, user: User) -> None:
    """Подтянуть из панели факт первого подключения к VPN.

    Признак подключения живёт только в RemnaWave, а забирает его единственный
    вызов `sync_subscription_usage`. Меню «Подключиться» работает на локальном
    ключе и панель не трогает, поэтому дёргаем синк здесь — иначе
    `has_connected_to_vpn` не выставится ни у кого, а на нём завязаны бонус за
    первое подключение и отсев «неподключившихся» в триальных напоминаниях.
    Только пока флаг не выставлен: дальше сигнал уже не нужен.
    """
    if getattr(user, "has_connected_to_vpn", False):
        return

    subscription = getattr(user, "subscription", None)
    if not subscription:
        return

    try:
        from app.services.subscription_service import SubscriptionService

        service = SubscriptionService()
        if not service.is_configured:
            return
        await service.sync_subscription_usage(db, subscription)
    except Exception as error:
        logger.warning(
            "Не удалось определить подключение к VPN для пользователя %s: %s",
            getattr(user, "telegram_id", None),
            error,
        )


async def _get_connect_menu_user(
    callback: types.CallbackQuery,
    db: AsyncSession,
) -> User | None:
    user = await get_user_by_telegram_id(db, callback.from_user.id)
    if user is None:
        return None

    if not _get_connection_key(user):
        texts = get_texts(user.language)
        await callback.answer(
            texts.t("SUBSCRIPTION_LINK_UNAVAILABLE", "❌ Ссылка подписки недоступна"),
            show_alert=True,
        )
        return None

    await _detect_vpn_connection(db, user)

    return user


async def _build_connect_platform_selection_text(db: AsyncSession, user: User) -> str:
    texts = get_texts(user.language)
    return (
        await build_access_key_section(
            db,
            user,
            texts,
            texts.t(
                "CONNECT_ACCESS_KEY_LABEL",
                "<b>Твой ключ доступа</b> (для приложений Leto, Happ, Incy)",
            ),
        )
        + "\n\n"
        + texts.t(
            "CONNECT_PLATFORM_SELECTION_TEXT",
            "Выбери платформу для подключения:",
        )
    )


async def handle_howto(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    from_toggle: bool = False
):
    """Show the Connect menu with a universal subscription key."""
    user = await _get_connect_menu_user(callback, db)
    if user is None:
        return

    await edit_or_answer_photo(
        callback,
        await _build_connect_platform_selection_text(db, user),
        get_connection_platform_keyboard(user.language),
        parse_mode="HTML",
        photo_path=os.path.join("images", "connection.webp"),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def handle_connect_platform_android(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    user = await _get_connect_menu_user(callback, db)
    if user is None:
        return

    texts = get_texts(user.language)
    text = (
        await build_access_key_section(
            db,
            user,
            texts,
            texts.t(
                "CONNECT_ACCESS_KEY_LABEL",
                "<b>Твой ключ доступа</b> (для приложений Leto, Happ, Incy)",
            ),
        )
        + "\n\n"
        + texts.t(
            "CONNECT_ANDROID_TEXT",
            "Скачай Leto VPN по кнопке ниже и авторизуйся через Telegram или с помощью ключа доступа.",
        )
        + "\n\n"
        + texts.t(
            "CONNECT_ANDROID_HAPP_HINT",
            "Если у тебя есть Happ, можешь передать ключ доступа в него по кнопке ниже.",
        )
    )
    await edit_or_answer_photo(
        callback,
        text,
        get_connect_android_keyboard(
            user.language,
            user.telegram_id,
            happ_transfer_url=_get_happ_transfer_url(user),
        ),
        parse_mode="HTML",
        photo_path=os.path.join("images", "connection.webp"),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def handle_connect_platform_apple(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    user = await _get_connect_menu_user(callback, db)
    if user is None:
        return

    texts = get_texts(user.language)
    text = (
        await build_access_key_section(
            db,
            user,
            texts,
            texts.t(
                "CONNECT_ACCESS_KEY_LABEL",
                "<b>Твой ключ доступа</b> (для приложений Leto, Happ, Incy)",
            ),
        )
        + "\n\n"
        + texts.t(
            "CONNECT_APPLE_TEXT",
            "<b>Для iOS/macOS доступны два приложения:</b>\n"
            "<b>Incy</b> — RU App Store\n"
            "<b>Happ</b> — международный App Store\n\n"
            "В любое из приложений (после скачивания) можно передать ключ доступа по кнопке ниже.",
        )
    )
    await edit_or_answer_photo(
        callback,
        text,
        get_connect_apple_keyboard(
            user.language,
            incy_transfer_url=get_incy_button_url(_get_connection_key(user)),
            happ_transfer_url=_get_happ_transfer_url(user),
        ),
        parse_mode="HTML",
        photo_path=os.path.join("images", "connection.webp"),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def handle_connect_platform_windows(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    user = await _get_connect_menu_user(callback, db)
    if user is None:
        return

    texts = get_texts(user.language)
    link = _get_connection_key(user)
    text = (
        texts.t(
            "CONNECT_WINDOWS_TEXT",
            "Для Windows Leto работает через Happ. Скачай приложение и вставь туда ссылку-ключ ниже:",
        )
        + "\n\n"
        + _format_connection_key(link)
    )
    await edit_or_answer_photo(
        callback,
        text,
        get_connect_windows_keyboard(
            user.language,
            happ_transfer_url=_get_happ_transfer_url(user),
        ),
        parse_mode="HTML",
        photo_path=os.path.join("images", "connection.webp"),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def handle_onboarding_connect_free(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    """Screen 1 -> Screen 2: User clicks 'Подключиться бесплатно', show device selection."""
    user = await get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        return

    caption = _build_onboarding_device_selection_view(user)

    image_path = os.path.join("images", "connection.webp")
    if not os.path.exists(image_path):
        image_path = None

    await edit_or_answer_photo(
        callback,
        caption,
        get_onboarding_device_selection_keyboard(user.language),
        parse_mode="HTML",
        photo_path=image_path,
    )
    await callback.answer()


async def handle_onboarding_device_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    """Screen 2 -> Screen 3: Show device-specific connection instructions."""
    user = await get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        return

    device_map = {
        "onboarding_device_iphone": "iphone",
        "onboarding_device_android": "android",
        "onboarding_device_windows": "windows",
        "onboarding_device_macos": "macos",
    }
    device_type = device_map.get(callback.data, "iphone")
    await state.update_data(onboarding_device_type=device_type, onboarding_link_sent=False)

    texts = get_texts(user.language)
    if device_type == "android":
        connection_text = texts.t(
            "ONBOARDING_CONNECTION_TEXT_ANDROID",
            "Установи приложение Leto по кнопке ниже.\n\nПосле авторизуйся в приложении через Telegram → все настроится в один клик.",
        )
    elif is_ios_device_type(device_type):
        connection_text = texts.t(
            "ONBOARDING_CONNECTION_TEXT_IOS",
            "Установи приложение Incy по кнопке ниже.\n\nПосле установки нажми кнопку \"Подключиться\"  ниже → все настроится автоматически.",
        )
    else:
        connection_text = texts.t(
            "ONBOARDING_CONNECTION_TEXT",
            "Установи приложение Happ по кнопке ниже.\n\nПосле установки нажми кнопку \"Подключиться\"  ниже → все настроится автоматически.",
        )

    subscription_link = None
    raw_subscription_link = None
    if user.subscription:
        subscription_link = get_display_subscription_link(user.subscription)
        raw_subscription_link = get_raw_subscription_link(user.subscription)

    keyboard = get_onboarding_connection_keyboard(
        device_type,
        user.language,
        subscription_link=subscription_link,
        telegram_id=user.telegram_id,
        raw_subscription_link=raw_subscription_link,
    )

    image_path = os.path.join("images", "connection.webp")
    if not os.path.exists(image_path):
        image_path = None

    await edit_or_answer_photo(
        callback,
        connection_text,
        keyboard,
        parse_mode="HTML",
        photo_path=image_path,
    )
    await callback.answer()


async def handle_onboarding_connect(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    """Handle 'Подключиться' button on Screen 3: send deep link, prevent duplicates, update message."""
    logger.info(f"🔗 ONBOARDING_CONNECT: Нажатие кнопки Подключиться от {callback.from_user.id}")

    # Answer callback immediately to remove loader
    await callback.answer()

    user = await get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        return

    # Duplicate protection via FSM state
    data = await state.get_data() or {}
    if data.get("onboarding_link_sent"):
        logger.info(f"⚠️ ONBOARDING_CONNECT: Повторное нажатие от {callback.from_user.id}, игнорируем")
        return

    # Mark link as sent to prevent duplicates
    await state.update_data(onboarding_link_sent=True)

    device_type = data.get("onboarding_device_type") or "iphone"

    # Build combined post-connect keyboard with deep link
    redirect_link = None
    subscription = user.subscription
    if subscription:
        subscription_link = get_display_subscription_link(subscription)
        if subscription_link:
            if is_ios_device_type(device_type):
                # Incy принимает обычную ссылку подписки, а не криптоссылку.
                redirect_link = get_incy_button_url(
                    get_raw_subscription_link(subscription) or subscription_link
                )
            else:
                from app.utils.subscription_utils import get_happ_cryptolink_redirect_link
                redirect_link = get_happ_cryptolink_redirect_link(subscription_link)
            if redirect_link:
                logger.info(f"✅ ONBOARDING_CONNECT: Deep link сгенерирован для {callback.from_user.id}")
            else:
                logger.warning(f"⚠️ ONBOARDING_CONNECT: Не удалось сгенерировать redirect link для {callback.from_user.id}")
        else:
            logger.warning(f"⚠️ ONBOARDING_CONNECT: Нет subscription_link для {callback.from_user.id}")
    else:
        logger.warning(f"⚠️ ONBOARDING_CONNECT: Нет подписки у {callback.from_user.id}")

    # Build single message with all buttons
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    texts = get_texts(user.language)
    support_url = settings.get_support_contact_url()
    buttons = []
    if redirect_link:
        buttons.append([InlineKeyboardButton(
            text=texts.t("ONBOARDING_OPEN_HAPP_BUTTON", "🚀 Подключиться"),
            url=redirect_link,
        )])
    buttons.append([InlineKeyboardButton(
        text=texts.t("ONBOARDING_CONNECTED_BUTTON", "✅ Я подключился"),
        callback_data="main_menu",
    )])
    buttons.append([InlineKeyboardButton(
        text=texts.t("ONBOARDING_MANUAL_LINK_BUTTON", "🔗 Ручное подключение"),
        callback_data="onboarding_manual_link",
    )])
    if support_url:
        buttons.append([InlineKeyboardButton(
            text=texts.t("ONBOARDING_SUPPORT_BUTTON", "💬 Поддержка"),
            url=support_url,
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    post_connect_text = texts.t(
        "ONBOARDING_POST_CONNECT_TEXT",
        "Нажми 🚀 Подключиться → в приложении выбери Подключить (займет 5–10 сек).\n\n"
        "Вернись сюда:\n\n"
        "Если всё ок — нажми Я подключился. "
        "Если не получилось — выбери Ручное подключение или Поддержка.",
    )

    await edit_or_answer_photo(
        callback,
        post_connect_text,
        keyboard,
        parse_mode="HTML",
    )


async def handle_onboarding_manual_link(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    """Show subscription link as copyable code for manual connection."""
    if db_user is None:
        texts = get_texts(settings.DEFAULT_LANGUAGE_CODE)
        await callback.answer(
            texts.t("USER_NOT_FOUND_ERROR", "Ошибка: пользователь не найден."),
            show_alert=True,
        )
        return

    subscription = db_user.subscription
    if not subscription:
        await callback.answer("❌ У вас нет активной подписки", show_alert=True)
        return

    device_type = "iphone"
    if state is not None:
        state_data = await state.get_data() or {}
        device_type = state_data.get("onboarding_device_type") or device_type

    is_ios = is_ios_device_type(device_type)

    # На iOS Incy добавляет подписку по обычной ссылке, а не по криптоссылке.
    link = (
        get_raw_subscription_link(subscription) if is_ios else None
    ) or get_display_subscription_link(subscription)
    if not link:
        await callback.answer("❌ Ссылка подписки недоступна", show_alert=True)
        return

    texts = get_texts(db_user.language)
    if is_ios:
        manual_prompt = texts.t(
            "ONBOARDING_MANUAL_LINK_TEXT_IOS",
            "Для ручного подключения скопируй ключ и добавь его в Incy\n\n",
        )
    else:
        manual_prompt = texts.t(
            "ONBOARDING_MANUAL_LINK_TEXT",
            "Для ручного подключения скопируй ключ и добавь его в Happ\n\n",
        )

    manual_text = manual_prompt + f"<blockquote expandable><code>{link}</code></blockquote>"

    support_url = settings.get_support_contact_url()

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    manual_rows = []
    if support_url:
        manual_rows.append([InlineKeyboardButton(
            text=texts.t("ONBOARDING_SUPPORT_BUTTON", "💬 Поддержка"),
            url=support_url,
        )])
    manual_rows.append([InlineKeyboardButton(
        text=texts.t("BACK", "⬅️ Назад"),
        callback_data="onboarding_connect_free",
    )])
    keyboard = InlineKeyboardMarkup(inline_keyboard=manual_rows)

    await edit_or_answer_photo(
        callback,
        manual_text,
        keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


async def handle_connection_link_toggle(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession
):
    action = callback.data # hide_link_howto or show_link_howto
    show = (action == "show_link_howto")
    await state.update_data(howto_show_link=show)
    await handle_howto(callback, state, db, from_toggle=True)


async def handle_profile(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    reg_date = format_local_datetime(db_user.created_at, "%Y-%m-%d")

    text = (
        texts.t("PROFILE_TITLE", "👤 Профиль\n\n")
        + texts.t("PROFILE_USER_ID", "ID пользователя: {user_id}").format(user_id=db_user.telegram_id) + "\n"
        + texts.t("PROFILE_REG_DATE", "Дата регистрации: {reg_date}").format(reg_date=reg_date)
    )

    image_path = os.path.join("images", "profile.webp")
    if not os.path.exists(image_path):
         image_path = None

    has_subscription = db_user.subscription is not None

    await edit_or_answer_photo(
        callback,
        text,
        get_profile_keyboard(
            language=db_user.language,
            balance_kopeks=db_user.balance_kopeks,
            has_subscription=has_subscription,
        ),
        parse_mode="HTML",
        photo_path=image_path
    )
    await callback.answer()


async def handle_referral(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Экран «Пригласить друзей»."""
    texts = get_texts(db_user.language)
    stats = await get_user_referral_stats(db, db_user.id)

    count = stats.get("invited_count", 0)
    amount = int(stats.get("total_earned_kopeks", 0) / 100)
    rays = db_user.rays_balance or 0
    percent = get_effective_referral_commission_percent(db_user)
    rays_enabled = settings.is_rays_program_enabled()
    shop_enabled = settings.is_rays_shop_enabled()

    bot = await callback.bot.get_me()
    referral_link = f"https://t.me/{bot.username}?start={db_user.referral_code}"

    if rays_enabled:
        header = texts.t(
            "REFERRAL_HEADER",
            "💸 <b>Получай {percent}% с платежей друзей + ☀️ Лучи на призы</b>",
        ).format(percent=percent)
    else:
        header = texts.t(
            "REFERRAL_HEADER_NO_RAYS",
            "💸 <b>Получай {percent}% с платежей друзей</b>",
        ).format(percent=percent)

    lines = [
        header,
        "",
        texts.t("REFERRAL_INVITED_LINE", "👥 <b>Приглашено друзей:</b> {count}").format(count=count),
        texts.t("REFERRAL_EARNED_LINE", "💰 <b>Заработано:</b> {amount} ₽").format(amount=amount),
    ]
    if rays_enabled:
        lines.append(texts.t("REFERRAL_RAYS_LINE", "☀️ <b>Лучей:</b> {rays}").format(rays=rays))
    lines += [
        "",
        texts.t("REFERRAL_LINK_QUOTE", "🔗 Твоя ссылка\n<code>{link}</code>").format(link=referral_link),
        "",
        texts.t("REFERRAL_HOW_HEADING", "❓ <b>Как это работает</b>"),
        texts.t(
            "REFERRAL_HOW_COMMISSION",
            "• Получай {percent}% со <b>всех</b> платежей друзей",
        ).format(percent=percent),
        texts.t(
            "REFERRAL_HOW_BALANCE",
            "• Деньги идут на баланс (вывод на карту от {min_withdrawal}₽)",
        ).format(min_withdrawal=settings.REFERRAL_WITHDRAWAL_MIN_RUBLES),
    ]
    if rays_enabled:
        lines.append(
            texts.t("REFERRAL_HOW_RAYS", "• За друзей с подпиской от 3 мес. начисляются ☀️ Лучи")
        )
        lines.append(
            texts.t("REFERRAL_HOW_SHOP", "• Лучи можно обменять на призы в Магазине наград")
        )
    if settings.REFERRAL_TERMS_URL:
        lines += [
            "",
            f'<a href="{settings.REFERRAL_TERMS_URL}">'
            + texts.t("REFERRAL_TERMS_LINK", "📖 Полные условия программы →")
            + "</a>",
        ]

    invite_text = texts.t("REFERRAL_INVITE_TEXT", "🔥 Лови 3 дня бесплатного VPN!\n{link}").format(link=referral_link)

    await edit_or_answer_photo(
        callback,
        "\n".join(lines),
        get_referral_keyboard(
            referral_link,
            invite_text,
            language=db_user.language,
            show_rewards_shop=shop_enabled,
        ),
        photo_path=(
            os.path.join("images", "ref.webp")
            if os.path.exists(os.path.join("images", "ref.webp"))
            else None
        ),
    )
    await callback.answer()

async def handle_copy_referral_link(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    bot = await callback.bot.get_me()
    referral_link = f"https://t.me/{bot.username}?start={db_user.referral_code}"
    copy_text = texts.t("REFERRAL_COPY_LABEL", "Ваша ссылка:\n<code>{link}</code>").format(link=referral_link)
    await callback.message.answer(copy_text, parse_mode="HTML")
    await callback.answer()

async def handle_balance(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    balance = db_user.balance_kopeks / 100
    
    # Estimate days (approx 10 rub/day)
    estimated_days = int(balance / 10)
    
    # Last topup
    transactions = await get_user_transactions(db, db_user.id, limit=20)
    last_deposit = None
    for t in transactions:
        if t.type == TransactionType.DEPOSIT.value and t.is_completed:
            last_deposit = t
            break
    
    last_topup_text = ""
    if last_deposit:
         date_str = format_local_datetime(last_deposit.created_at, "%d.%m.%Y")
         amount_dep = last_deposit.amount_kopeks / 100
         last_topup_text = texts.t("BALANCE_LAST_TOPUP", "\n\nПоследнее пополнение: {date} на {amount}₽").format(date=date_str, amount=int(amount_dep))
    
    text = (
        texts.t("BALANCE_TITLE", "💳 Ваш баланс\n\n")
        + texts.t("BALANCE_CURRENT", "Текущий баланс: {amount}₽").format(amount=int(balance)) + "\n"
        + texts.t("BALANCE_ESTIMATED_DAYS", "Это ~{days} дней подписки").format(days=estimated_days)
        + last_topup_text
    )
    
    image_path = os.path.join("images", "balance_screen.png")
    if not os.path.exists(image_path):
         image_path = None

    await edit_or_answer_photo(callback, text, get_balance_keyboard(language=db_user.language), parse_mode="HTML", photo_path=image_path)
    await callback.answer()

async def handle_purchases_history(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # Retrieve history
    transactions = await get_user_transactions(db, db_user.id, limit=10)
    lines = []
    for t in transactions:
        if t.is_completed:
            date_str = format_local_datetime(t.created_at, "%d.%m")
            amt = t.amount_kopeks / 100
            type_icon = "➕" if t.type == TransactionType.DEPOSIT.value else "➖"
            lines.append(f"{date_str} {type_icon} {amt:.0f}₽")
    
    if not lines:
        lines.append(texts.t("HISTORY_EMPTY", "История пуста"))
        
    history_text = texts.t("HISTORY_TITLE", "📜 Последние операции:\n\n") + "\n".join(lines)
    await callback.answer(history_text, show_alert=True)

async def handle_support(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = (
        texts.t("SUPPORT_TITLE", "🛠 Поддержка\n\n")
        + format_support_placeholders(
            texts.t(
                "SUPPORT_TEXT",
                "Возникли вопросы или проблемы?\nСвяжитесь с нашей службой поддержки:\n"
                "\n📧 Email: {support_email}\n💬 Telegram: {support_contact}\n\n"
                "Мы отвечаем в течение 24 часов.",
            )
        )
    )
    
    image_path = os.path.join("images", "support.webp")
    if not os.path.exists(image_path):
         image_path = None

    await edit_or_answer_photo(callback, text, get_support_keyboard(language=db_user.language), parse_mode="HTML", photo_path=image_path)
    await callback.answer()


def register_handlers(dp: Dispatcher):

    dp.callback_query.register(
        handle_howto,
        F.data == "howto"
    )

    dp.callback_query.register(
        handle_connect_platform_android,
        F.data == "connect_platform_android",
    )

    dp.callback_query.register(
        handle_connect_platform_apple,
        F.data == "connect_platform_apple",
    )

    dp.callback_query.register(
        handle_connect_platform_windows,
        F.data == "connect_platform_windows",
    )

    dp.callback_query.register(
        handle_onboarding_connect_free,
        F.data == "onboarding_connect_free"
    )

    dp.callback_query.register(
        handle_onboarding_device_selection,
        F.data.in_({"onboarding_device_iphone", "onboarding_device_android", "onboarding_device_windows", "onboarding_device_macos"})
    )

    dp.callback_query.register(
        handle_onboarding_connect,
        F.data == "onboarding_connect"
    )

    dp.callback_query.register(
        handle_onboarding_manual_link,
        F.data == "onboarding_manual_link"
    )

    dp.callback_query.register(
        handle_connection_link_toggle,
        F.data.in_({"show_link_howto", "hide_link_howto"})
    )

    dp.callback_query.register(
        handle_back_to_menu,
        F.data.in_({"back_to_menu", "main_menu"})
    )
    
    dp.callback_query.register(
        handle_profile,
        F.data == "profile"
    )

    dp.callback_query.register(
        handle_referral,
        F.data == "referral"
    )

    dp.callback_query.register(
        handle_copy_referral_link,
        F.data == "copy_referral_link"
    )

    dp.callback_query.register(
        handle_balance,
        F.data == "balance"
    )
    
    dp.callback_query.register(
         handle_purchases_history,
         F.data == "purchases_history"
    )

    dp.callback_query.register(
        handle_support,
        F.data == "support"
    )

    dp.callback_query.register(
        handle_profile_unavailable,
        F.data == "menu_profile_unavailable",
    )

    dp.callback_query.register(
        show_service_rules,
        F.data == "menu_rules"
    )

    dp.callback_query.register(
        show_info_menu,
        F.data == "menu_info",
    )

    dp.callback_query.register(
        show_promo_groups_info,
        F.data == "menu_info_promo_groups",
    )

    dp.callback_query.register(
        show_faq_pages,
        F.data == "menu_faq",
    )

    dp.callback_query.register(
        show_faq_page,
        F.data.startswith("menu_faq_page:"),
    )

    dp.callback_query.register(
        show_privacy_policy,
        F.data.in_({"menu_privacy_policy", "profile_privacy"}),
    )

    dp.callback_query.register(
        show_privacy_policy,
        F.data.startswith("menu_privacy_policy:"),
    )

    dp.callback_query.register(
        show_public_offer,
        F.data.in_({"menu_public_offer", "profile_terms"}),
    )

    dp.callback_query.register(
        show_public_offer,
        F.data.startswith("menu_public_offer:"),
    )

    dp.callback_query.register(
        show_language_menu,
        F.data.in_({"menu_language", "profile_language"})
    )

    dp.callback_query.register(
        process_language_change,
        F.data.startswith("language_select:"),
        StateFilter(None)
    )

    dp.callback_query.register(
        handle_add_traffic,
        F.data == "buy_traffic"
    )

    dp.callback_query.register(
        add_traffic,
        F.data.startswith("add_traffic_")
    )

    dp.callback_query.register(
        handle_activate_button,
        F.data == "activate_button"
    )
