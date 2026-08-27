import logging
import re
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from aiogram import Dispatcher, types, F
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.states import AdminStates
from app.database.models import User, UserStatus, Subscription, SubscriptionStatus, TransactionType 
from app.database.crud.user import (
    get_user_by_id,
    get_user_by_telegram_id,
    get_user_by_username,
    get_referrals,
)
from app.database.crud.subscription_event import (
    record_subscription_purchase_event,
    record_subscription_renewal_event,
)
from app.database.crud.campaign import (
    get_campaign_registration_by_user,
    get_campaign_statistics,
)
from app.keyboards.admin import (
    get_admin_users_keyboard, get_user_management_keyboard,
    get_admin_pagination_keyboard, get_confirmation_keyboard,
    get_admin_users_filters_keyboard, get_user_promo_group_keyboard
)
from app.localization.texts import get_texts
from app.services.user_service import UserService
from app.services.admin_notification_service import AdminNotificationService
from app.database.crud.device_link import revoke_all_device_links
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_time_ago
from app.utils.user_utils import get_effective_referral_commission_percent
from app.services.remnawave_service import RemnaWaveService
from app.external.remnawave_api import TrafficLimitStrategy
from app.database.crud.server_squad import (
    get_all_server_squads,
    get_active_server_squads,
    get_server_squad_by_uuid,
    get_server_squad_by_id,
    get_server_ids_by_uuids,
)
from app.services.plan_pricing_service import (
    SUPPORTED_PERIOD_DAYS,
    get_plan_by_id,
    get_plan_price,
    list_active_plans,
    resolve_pricing_cohort,
)
from app.services.subscription_service import SubscriptionService
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)

logger = logging.getLogger(__name__)

_TARIFF_PERIOD_LABELS = {30: "1 мес", 90: "3 мес", 180: "6 мес", 360: "1 год", 720: "2 года"}


def _tariff_period_label(period_days: Optional[int]) -> str:
    if not period_days:
        return "—"
    return _TARIFF_PERIOD_LABELS.get(period_days, f"{period_days} дн.")


@admin_required
@error_handler
async def show_users_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)
    
    text = f"""
👥 <b>Управление пользователями</b>

📊 <b>Статистика:</b>
• Всего: {stats['total_users']}
• Активных: {stats['active_users']}
• Заблокированных: {stats['blocked_users']}

📈 <b>Новые пользователи:</b>
• Сегодня: {stats['new_today']}
• За неделю: {stats['new_week']}
• За месяц: {stats['new_month']}

Выберите действие:
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_users_keyboard(db_user.language)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_filters(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    
    text = ("⚙️ <b>Фильтры пользователей</b>\n\nВыберите фильтр для отображения пользователей:\n")
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_users_filters_keyboard(db_user.language)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    # Сбрасываем состояние, так как мы в обычном списке
    await state.set_state(None)
    
    user_service = UserService()
    users_data = await user_service.get_users_page(db, page=page, limit=10)
    
    if not users_data["users"]:
        await callback.message.edit_text(
            "👥 Пользователи не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return
    
    text = f"👥 <b>Список пользователей</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"
    
    keyboard = []
    
    for user in users_data["users"]:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = "✅"
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = "🚫"
        else:
            status_emoji = "🗑️"
        
        subscription_emoji = ""
        if user.subscription:
            if user.subscription.is_trial:
                subscription_emoji = "🎁"
            elif user.subscription.is_active:
                subscription_emoji = "💎"
            else:
                subscription_emoji = "⏰"
        else:
            subscription_emoji = "❌"
        
        button_text = f"{status_emoji} {subscription_emoji} {user.full_name}"
        
        if user.balance_kopeks > 0:
            button_text += f" | 💰 {settings.format_price(user.balance_kopeks)}"
        
        button_text += f" | 📅 {format_time_ago(user.created_at, db_user.language)}"
        
        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + "..."
            
            button_text = f"{status_emoji} {subscription_emoji} {short_name}"
            if user.balance_kopeks > 0:
                button_text += f" | 💰 {settings.format_price(user.balance_kopeks)}"
        
        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])
    
    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)
    
    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_balance(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    # Устанавливаем состояние, чтобы отслеживать, откуда пришел пользователь
    await state.set_state(AdminStates.viewing_user_from_balance_list)
    
    user_service = UserService()
    users_data = await user_service.get_users_page(db, page=page, limit=10, order_by_balance=True)
    
    if not users_data["users"]:
        await callback.message.edit_text(
            "👥 Пользователи не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return
    
    text = f"👥 <b>Список пользователей по балансу</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"
    
    keyboard = []
    
    for user in users_data["users"]:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = "✅"
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = "🚫"
        else:
            status_emoji = "🗑️"
        
        subscription_emoji = ""
        if user.subscription:
            if user.subscription.is_trial:
                subscription_emoji = "🎁"
            elif user.subscription.is_active:
                subscription_emoji = "💎"
            else:
                subscription_emoji = "⏰"
        else:
            subscription_emoji = "❌"
        
        button_text = f"{status_emoji} {subscription_emoji} {user.full_name}"
        
        if user.balance_kopeks > 0:
            button_text += f" | 💰 {settings.format_price(user.balance_kopeks)}"
        
        # Добавляем дату окончания подписки, если есть подписка
        if user.subscription and user.subscription.end_date:
            days_left = (user.subscription.end_date - datetime.utcnow()).days
            button_text += f" | 📅 {days_left}д"
        
        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + "..."
            
            button_text = f"{status_emoji} {subscription_emoji} {short_name}"
            if user.balance_kopeks > 0:
                button_text += f" | 💰 {settings.format_price(user.balance_kopeks)}"
        
        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])
    
    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_balance_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)
    
    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_ready_to_renew(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    """Показывает пользователей с истекшей подпиской и балансом >= порога."""
    await state.set_state(AdminStates.viewing_user_from_ready_to_renew_list)

    texts = get_texts(db_user.language)
    threshold = getattr(
        settings,
        "SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS",
        20000,
    )

    user_service = UserService()
    users_data = await user_service.get_users_ready_to_renew(
        db,
        min_balance_kopeks=threshold,
        page=page,
        limit=10,
    )

    amount_text = settings.format_price(threshold)
    header = texts.t(
        "ADMIN_USERS_FILTER_RENEW_READY_TITLE",
        "♻️ Пользователи готовы к продлению",
    )
    description = texts.t(
        "ADMIN_USERS_FILTER_RENEW_READY_DESC",
        "Подписка истекла, а на балансе осталось {amount} или больше.",
    ).format(amount=amount_text)

    if not users_data["users"]:
        empty_text = texts.t(
            "ADMIN_USERS_FILTER_RENEW_READY_EMPTY",
            "Сейчас нет пользователей, которые подходят под этот фильтр.",
        )
        await callback.message.edit_text(
            f"{header}\n\n{description}\n\n{empty_text}",
            reply_markup=get_admin_users_keyboard(db_user.language),
        )
        await callback.answer()
        return

    text = f"{header}\n\n{description}\n\n"
    text += "Нажмите на пользователя для управления:"

    keyboard = []
    current_time = datetime.utcnow()

    for user in users_data["users"]:
        subscription = user.subscription
        status_emoji = "✅" if user.status == UserStatus.ACTIVE.value else "🚫"
        subscription_emoji = "❌"
        expired_days = "?"

        if subscription:
            if subscription.is_trial:
                subscription_emoji = "🎁"
            elif subscription.is_active:
                subscription_emoji = "💎"
            else:
                subscription_emoji = "⏰"

            if subscription.end_date:
                delta = current_time - subscription.end_date
                expired_days = delta.days

        button_text = (
            f"{status_emoji} {subscription_emoji} {user.full_name}"
            f" | 💰 {settings.format_price(user.balance_kopeks)}"
            f" | ⏰ {expired_days}д ист."
        )

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + "..."
            button_text = (
                f"{status_emoji} {subscription_emoji} {short_name}"
                f" | 💰 {settings.format_price(user.balance_kopeks)}"
            )

        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}",
            )
        ])

    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_ready_to_renew_list",
            "admin_users_ready_to_renew_filter",
            db_user.language,
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend([
        [
            types.InlineKeyboardButton(
                text="🔍 Поиск",
                callback_data="admin_users_search",
            ),
            types.InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="admin_users_stats",
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="admin_users",
            )
        ],
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    await state.set_state(AdminStates.viewing_user_from_traffic_list)

    user_service = UserService()
    users_data = await user_service.get_users_page(
        db, page=page, limit=10, order_by_traffic=True
    )

    if not users_data["users"]:
        await callback.message.edit_text(
            "📶 Пользователи с трафиком не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    text = f"👥 <b>Список пользователей по использованному трафику</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"

    keyboard = []

    for user in users_data["users"]:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = "✅"
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = "🚫"
        else:
            status_emoji = "🗑️"

        if user.subscription:
            sub = user.subscription
            if sub.is_trial:
                subscription_emoji = "🎁"
            elif sub.is_active:
                subscription_emoji = "💎"
            else:
                subscription_emoji = "⏰"
            used = sub.traffic_used_gb or 0.0
            if sub.traffic_limit_gb and sub.traffic_limit_gb > 0:
                limit_display = f"{sub.traffic_limit_gb}"
            else:
                limit_display = "♾️"
            traffic_display = f"{used:.1f}/{limit_display} ГБ"
        else:
            subscription_emoji = "❌"
            traffic_display = "нет подписки"

        button_text = f"{status_emoji} {subscription_emoji} {user.full_name}"
        button_text += f" | 📶 {traffic_display}"

        if user.balance_kopeks > 0:
            button_text += f" | 💰 {settings.format_price(user.balance_kopeks)}"

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + "..."
            button_text = f"{status_emoji} {subscription_emoji} {short_name}"
            button_text += f" | 📶 {traffic_display}"

        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])

    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_traffic_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_last_activity(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    await state.set_state(AdminStates.viewing_user_from_last_activity_list)

    user_service = UserService()
    users_data = await user_service.get_users_page(
        db,
        page=page,
        limit=10,
        order_by_last_activity=True,
    )

    if not users_data["users"]:
        await callback.message.edit_text(
            "🕒 Пользователи с активностью не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    text = f"👥 <b>Пользователи по активности</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"

    keyboard = []

    for user in users_data["users"]:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = "✅"
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = "🚫"
        else:
            status_emoji = "🗑️"

        activity_display = (
            format_time_ago(user.last_activity, db_user.language)
            if user.last_activity
            else "неизвестно"
        )

        subscription_emoji = "❌"
        if user.subscription:
            if user.subscription.is_trial:
                subscription_emoji = "🎁"
            elif user.subscription.is_active:
                subscription_emoji = "💎"
            else:
                subscription_emoji = "⏰"

        button_text = f"{status_emoji} {subscription_emoji} {user.full_name}"
        button_text += f" | 🕒 {activity_display}"

        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])

    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_activity_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_spending(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    await state.set_state(AdminStates.viewing_user_from_spending_list)

    user_service = UserService()
    users_data = await user_service.get_users_page(
        db,
        page=page,
        limit=10,
        order_by_total_spent=True,
    )

    users = users_data["users"]
    if not users:
        await callback.message.edit_text(
            "💳 Пользователи с тратами не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    spending_map = await user_service.get_user_spending_stats_map(
        db,
        [user.id for user in users],
    )

    text = f"👥 <b>Пользователи по сумме трат</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"

    keyboard = []

    for user in users:
        stats = spending_map.get(
            user.id,
            {"total_spent": 0, "purchase_count": 0},
        )
        total_spent = stats.get("total_spent", 0)
        purchases = stats.get("purchase_count", 0)

        status_emoji = "✅" if user.status == UserStatus.ACTIVE.value else "🚫" if user.status == UserStatus.BLOCKED.value else "🗑️"

        button_text = (
            f"{status_emoji} {user.full_name}"
            f" | 💳 {settings.format_price(total_spent)}"
            f" | 🛒 {purchases}"
        )

        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])

    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_spending_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_purchases(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    await state.set_state(AdminStates.viewing_user_from_purchases_list)

    user_service = UserService()
    users_data = await user_service.get_users_page(
        db,
        page=page,
        limit=10,
        order_by_purchase_count=True,
    )

    users = users_data["users"]
    if not users:
        await callback.message.edit_text(
            "🛒 Пользователи с покупками не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    spending_map = await user_service.get_user_spending_stats_map(
        db,
        [user.id for user in users],
    )

    text = f"👥 <b>Пользователи по количеству покупок</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"

    keyboard = []

    for user in users:
        stats = spending_map.get(
            user.id,
            {"total_spent": 0, "purchase_count": 0},
        )
        total_spent = stats.get("total_spent", 0)
        purchases = stats.get("purchase_count", 0)

        status_emoji = "✅" if user.status == UserStatus.ACTIVE.value else "🚫" if user.status == UserStatus.BLOCKED.value else "🗑️"

        button_text = (
            f"{status_emoji} {user.full_name}"
            f" | 🛒 {purchases}"
            f" | 💳 {settings.format_price(total_spent)}"
        )

        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])

    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_purchases_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_campaign(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    page: int = 1
):
    
    await state.set_state(AdminStates.viewing_user_from_campaign_list)

    user_service = UserService()
    users_data = await user_service.get_users_by_campaign_page(
        db,
        page=page,
        limit=10,
    )

    users = users_data.get("users", [])
    campaign_map = users_data.get("campaigns", {})

    if not users:
        await callback.message.edit_text(
            "📢 Пользователи с кампанией не найдены",
            reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    text = f"👥 <b>Пользователи по кампании регистрации</b> (стр. {page}/{users_data['total_pages']})\n\n"
    text += "Нажмите на пользователя для управления:"

    keyboard = []

    for user in users:
        info = campaign_map.get(user.id, {})
        campaign_name = info.get("campaign_name") or "Без кампании"
        registered_at = info.get("registered_at")
        registered_display = format_datetime(registered_at) if registered_at else "неизвестно"

        status_emoji = "✅" if user.status == UserStatus.ACTIVE.value else "🚫" if user.status == UserStatus.BLOCKED.value else "🗑️"

        button_text = (
            f"{status_emoji} {user.full_name}"
            f" | 📢 {campaign_name}"
            f" | 📅 {registered_display}"
        )

        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])

    if users_data["total_pages"] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data["current_page"],
            users_data["total_pages"],
            "admin_users_campaign_list",
            "admin_users",
            db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend([
        [
            types.InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"),
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_users_stats")
        ],
        [
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
        ]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()



@admin_required
@error_handler
async def handle_users_list_pagination_fixed(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_balance_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list_by_balance(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list_by_balance(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_traffic_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list_by_traffic(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list_by_traffic(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_activity_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list_by_last_activity(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list_by_last_activity(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_spending_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list_by_spending(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list_by_spending(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_purchases_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list_by_purchases(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list_by_purchases(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_ready_to_renew_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        page = int(callback.data.split('_')[-1])
        await show_users_ready_to_renew(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_ready_to_renew(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_campaign_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1]) 
        await show_users_list_by_campaign(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга номера страницы: {e}")
        await show_users_list_by_campaign(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def start_user_search(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    texts = get_texts(db_user.language)

    # «Назад» ведёт туда, откуда открыли поиск: из корня админки — в корень,
    # из списка юзеров (кнопка 🔍 Поиск) — обратно в список.
    back_callback = (
        "admin_panel"
        if callback.data == "admin_users_search_root"
        else "admin_users"
    )

    await callback.message.edit_text(
        "🔍 <b>Поиск пользователя</b>\n\n"
        "Введите для поиска:\n"
        "• Telegram ID\n"
        "• Username (без @)\n"
        "• Имя или фамилию\n"
        "• Email\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)]
        ])
    )

    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()

@admin_required
@error_handler
async def show_users_statistics(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)
    
    from sqlalchemy import select, func, or_

    current_time = datetime.utcnow()

    active_subscription_query = (
        select(func.count(Subscription.id))
        .join(User, Subscription.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            Subscription.status.in_(
                [
                    SubscriptionStatus.ACTIVE.value,
                    SubscriptionStatus.TRIAL.value,
                ]
            ),
            Subscription.end_date > current_time,
        )
    )
    users_with_subscription = (
        await db.execute(active_subscription_query)
    ).scalar() or 0

    trial_subscription_query = (
        select(func.count(Subscription.id))
        .join(User, Subscription.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            Subscription.end_date > current_time,
            or_(
                Subscription.status == SubscriptionStatus.TRIAL.value,
                Subscription.is_trial.is_(True),
            ),
        )
    )
    trial_users = (await db.execute(trial_subscription_query)).scalar() or 0

    users_without_subscription = max(
        stats["active_users"] - users_with_subscription,
        0,
    )
    
    avg_balance_result = await db.execute(
        select(func.avg(User.balance_kopeks))
        .where(User.status == UserStatus.ACTIVE.value)
    )
    avg_balance = avg_balance_result.scalar() or 0
    
    text = f"""
📊 <b>Детальная статистика пользователей</b>

👥 <b>Общие показатели:</b>
• Всего: {stats['total_users']}
• Активных: {stats['active_users']}
• Заблокированных: {stats['blocked_users']}

📱 <b>Подписки:</b>
• С активной подпиской: {users_with_subscription}
• На триале: {trial_users}
• Без подписки: {users_without_subscription}

💰 <b>Финансы:</b>
• Средний баланс: {settings.format_price(int(avg_balance))}

📈 <b>Регистрации:</b>
• Сегодня: {stats['new_today']}
• За неделю: {stats['new_week']}
• За месяц: {stats['new_month']}

📊 <b>Активность:</b>
• Конверсия в подписку: {(users_with_subscription / max(stats['active_users'], 1) * 100):.1f}%
• Доля триальных: {(trial_users / max(users_with_subscription, 1) * 100):.1f}%
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_users_stats")],
            [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")]
        ])
    )
    await callback.answer()


async def _render_user_subscription_overview(
    callback: types.CallbackQuery,
    db: AsyncSession,
    user_id: int
) -> bool:
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return False

    user = profile["user"]
    subscription = profile["subscription"]

    text = "📱 <b>Подписка и настройки пользователя</b>\n\n"
    user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
    text += f"👤 {user_link} (ID: <code>{user.telegram_id}</code>)\n\n"

    keyboard = []

    if subscription:
        status_emoji = "✅" if subscription.is_active else "❌"
        type_emoji = "🎁" if subscription.is_trial else "💎"

        traffic_display = f"{subscription.traffic_used_gb:.1f}/"
        if subscription.traffic_limit_gb == 0:
            traffic_display += "♾️ ГБ"
        else:
            traffic_display += f"{subscription.traffic_limit_gb} ГБ"

        text += f"<b>Статус:</b> {status_emoji} {'Активна' if subscription.is_active else 'Неактивна'}\n"
        text += f"<b>Тип:</b> {type_emoji} {'Триал' if subscription.is_trial else 'Платная'}\n"
        if subscription.plan_id and subscription.plan:
            text += f"<b>Тариф:</b> 🧾 {subscription.plan.display_name} · {_tariff_period_label(subscription.plan_period_days)}\n"
        else:
            text += "<b>Тариф:</b> — (legacy / ручные параметры)\n"
        text += f"<b>Начало:</b> {format_datetime(subscription.start_date)}\n"
        text += f"<b>Окончание:</b> {format_datetime(subscription.end_date)}\n"
        text += f"<b>Трафик:</b> {traffic_display}\n"
        text += f"<b>Устройства:</b> {subscription.device_limit}\n"

        if subscription.is_active:
            days_left = (subscription.end_date - datetime.utcnow()).days
            text += f"<b>Осталось дней:</b> {days_left}\n"

        current_squads = subscription.connected_squads or []
        if current_squads:
            text += "\n<b>Подключенные серверы:</b>\n"
            for squad_uuid in current_squads:
                try:
                    server = await get_server_squad_by_uuid(db, squad_uuid)
                    if server:
                        text += f"• {server.display_name}\n"
                    else:
                        text += f"• {squad_uuid[:8]}... (неизвестный)\n"
                except Exception as e:
                    logger.error(f"Ошибка получения сервера {squad_uuid}: {e}")
                    text += f"• {squad_uuid[:8]}... (ошибка загрузки)\n"
        else:
            text += "\n<b>Подключенные серверы:</b> отсутствуют\n"

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text="⏰ Продлить",
                    callback_data=f"admin_sub_extend_{user_id}"
                ),
                types.InlineKeyboardButton(
                    text="💳 Купить подписку",
                    callback_data=f"admin_sub_buy_{user_id}"
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🔄 Тип подписки",
                    callback_data=f"admin_sub_change_type_{user_id}"
                ),
                types.InlineKeyboardButton(
                    text="📊 Добавить трафик",
                    callback_data=f"admin_sub_traffic_{user_id}"
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🌍 Сменить сервер",
                    callback_data=f"admin_user_change_server_{user_id}"
                ),
                types.InlineKeyboardButton(
                    text="📱 Устройства",
                    callback_data=f"admin_user_devices_{user_id}"
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🛠️ Лимит трафика",
                    callback_data=f"admin_user_traffic_{user_id}"
                ),
                types.InlineKeyboardButton(
                    text="🔄 Сбросить устройства",
                    callback_data=f"admin_user_reset_devices_{user_id}"
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🧾 Тариф",
                    callback_data=f"admin_sub_tariff_menu_{user_id}"
                )
            ]
        ]

        if subscription.is_active:
            keyboard.append([
                types.InlineKeyboardButton(
                    text="🚫 Деактивировать",
                    callback_data=f"admin_sub_deactivate_{user_id}"
                )
            ])
        else:
            keyboard.append([
                types.InlineKeyboardButton(
                    text="✅ Активировать",
                    callback_data=f"admin_sub_activate_{user_id}"
                )
            ])
    else:
        text += "❌ <b>Подписка отсутствует</b>\n\n"
        text += "Пользователь еще не активировал подписку."

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text="🎁 Выдать триал",
                    callback_data=f"admin_sub_grant_trial_{user_id}"
                ),
                types.InlineKeyboardButton(
                    text="💎 Выдать подписку",
                    callback_data=f"admin_sub_grant_{user_id}"
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🧾 Выдать тариф",
                    callback_data=f"admin_sub_tariff_assign_{user_id}"
                )
            ]
        ]

    keyboard.append([
        types.InlineKeyboardButton(text="⬅️ К пользователю", callback_data=f"admin_user_manage_{user_id}")
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    return True


@admin_required
@error_handler
async def show_user_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):

    user_id = int(callback.data.split('_')[-1])

    if await _render_user_subscription_overview(callback, db, user_id):
        await callback.answer()


@admin_required
@error_handler
async def show_user_transactions(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_id = int(callback.data.split('_')[-1])
    
    from app.database.crud.transaction import get_user_transactions
    
    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    transactions = await get_user_transactions(db, user_id, limit=10)
    
    text = f"💳 <b>Транзакции пользователя</b>\n\n"
    user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
    text += f"👤 {user_link} (ID: <code>{user.telegram_id}</code>)\n"
    text += f"💰 Текущий баланс: {settings.format_price(user.balance_kopeks)}\n\n"
    
    if transactions:
        text += "<b>Последние транзакции:</b>\n\n"
        
        for transaction in transactions:
            type_emoji = "📈" if transaction.amount_kopeks > 0 else "📉"
            text += f"{type_emoji} {settings.format_price(abs(transaction.amount_kopeks))}\n"
            text += f"📋 {transaction.description}\n"
            text += f"📅 {format_datetime(transaction.created_at)}\n\n"
    else:
        text += "📭 <b>Транзакции отсутствуют</b>"
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⬅️ К пользователю", callback_data=f"admin_user_manage_{user_id}")]
        ])
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_user_delete(
    callback: types.CallbackQuery,
    db_user: User
):
    
    user_id = int(callback.data.split('_')[-1])
    
    await callback.message.edit_text(
        "🗑️ <b>Удаление пользователя</b>\n\n"
        "⚠️ <b>ВНИМАНИЕ!</b>\n"
        "Вы уверены, что хотите удалить этого пользователя?\n\n"
        "Это действие:\n"
        "• Пометит пользователя как удаленного\n"
        "• Деактивирует его подписку\n"
        "• Заблокирует доступ к боту\n\n"
        "Данное действие необратимо!",
        reply_markup=get_confirmation_keyboard(
            f"admin_user_delete_confirm_{user_id}",
            f"admin_user_manage_{user_id}",
            db_user.language
        )
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_user_account(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_id = int(callback.data.split('_')[-1])
    
    user_service = UserService()
    success = await user_service.delete_user_account(db, user_id, db_user.id)
    
    if success:
        await callback.message.edit_text(
            "✅ Пользователь успешно удален",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="👥 К списку пользователей", callback_data="admin_users_list")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка удаления пользователя",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def process_user_search(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    
    query = message.text.strip()
    
    if not query:
        await message.answer("❌ Введите корректный запрос для поиска")
        return
    
    user_service = UserService()
    search_results = await user_service.search_users(db, query, page=1, limit=10)
    
    if not search_results["users"]:
        await message.answer(
            f"🔍 По запросу '<b>{query}</b>' ничего не найдено",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")]
            ])
        )
        await state.clear()
        return
    
    text = f"🔍 <b>Результаты поиска:</b> '{query}'\n\n"
    text += "Выберите пользователя:"
    
    keyboard = []
    
    for user in search_results["users"]:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = "✅"
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = "🚫"
        else:
            status_emoji = "🗑️"
        
        subscription_emoji = ""
        if user.subscription:
            if user.subscription.is_trial:
                subscription_emoji = "🎁"
            elif user.subscription.is_active:
                subscription_emoji = "💎"
            else:
                subscription_emoji = "⏰"
        else:
            subscription_emoji = "❌"
        
        if user.telegram_id:
            user_id_label = f"🆔 {user.telegram_id}"
        else:
            user_id_label = f"📧 {user.email}" if user.email else f"#{user.id}"

        button_text = f"{status_emoji} {subscription_emoji} {user.full_name}"

        button_text += f" | {user_id_label}"

        if user.balance_kopeks > 0:
            button_text += f" | 💰 {settings.format_price(user.balance_kopeks)}"

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 15:
                short_name = short_name[:12] + "..."
            button_text = f"{status_emoji} {subscription_emoji} {short_name} | {user_id_label}"
        
        keyboard.append([
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_manage_{user.id}"
            )
        ])
    
    keyboard.append([
        types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")
    ])
    
    await message.answer(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await state.clear()


@admin_required
@error_handler
async def show_user_management(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    
    # Поддерживаем переход "из тикета": admin_user_manage_{userId}_from_ticket_{ticketId}
    parts = callback.data.split('_')
    try:
        user_id = int(parts[3])  # admin_user_manage_{userId}
    except Exception:
        user_id = int(callback.data.split('_')[-1])
    origin_ticket_id = None
    if "from" in parts and "ticket" in parts:
        try:
            origin_ticket_id = int(parts[-1])
        except Exception:
            origin_ticket_id = None
    # Если пришли из тикета — запомним в состоянии, чтобы сохранять кнопку возврата
    try:
        if origin_ticket_id:
            await state.update_data(origin_ticket_id=origin_ticket_id, origin_ticket_user_id=user_id)
    except Exception:
        pass
    # Если не пришло в колбэке — попробуем достать из состояния
    if origin_ticket_id is None:
        try:
            data_state = await state.get_data()
            if data_state.get("origin_ticket_user_id") == user_id:
                origin_ticket_id = data_state.get("origin_ticket_id")
        except Exception:
            pass
    
    # Проверяем, откуда пришел пользователь
    back_callback = "admin_users_list"
    
    # Если callback_data содержит информацию о том, что мы пришли из списка по балансу
    # В реальности это сложно определить, поэтому будем использовать состояние
    
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    
    if not profile:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user = profile["user"]
    subscription = profile["subscription"]

    texts = get_texts(db_user.language)

    status_map = {
        UserStatus.ACTIVE.value: texts.ADMIN_USER_STATUS_ACTIVE,
        UserStatus.BLOCKED.value: texts.ADMIN_USER_STATUS_BLOCKED,
        UserStatus.DELETED.value: texts.ADMIN_USER_STATUS_DELETED,
    }
    status_text = status_map.get(user.status, texts.ADMIN_USER_STATUS_UNKNOWN)

    username_display = (
        f"@{user.username}" if user.username else texts.ADMIN_USER_USERNAME_NOT_SET
    )
    last_activity = (
        format_time_ago(user.last_activity, db_user.language)
        if user.last_activity
        else texts.ADMIN_USER_LAST_ACTIVITY_UNKNOWN
    )

    sections = [
        texts.ADMIN_USER_MANAGEMENT_PROFILE.format(
            name=user.full_name,
            telegram_id=user.telegram_id,
            username=username_display,
            status=status_text,
            language=user.language,
            balance=settings.format_price(user.balance_kopeks),
            transactions=profile["transactions_count"],
            registration=format_datetime(user.created_at),
            last_activity=last_activity,
            registration_days=profile["registration_days"],
        )
    ]

    if subscription:
        subscription_type = (
            texts.ADMIN_USER_SUBSCRIPTION_TYPE_TRIAL
            if subscription.is_trial
            else texts.ADMIN_USER_SUBSCRIPTION_TYPE_PAID
        )
        subscription_status = (
            texts.ADMIN_USER_SUBSCRIPTION_STATUS_ACTIVE
            if subscription.is_active
            else texts.ADMIN_USER_SUBSCRIPTION_STATUS_INACTIVE
        )
        traffic_usage = texts.ADMIN_USER_TRAFFIC_USAGE.format(
            used=f"{subscription.traffic_used_gb:.1f}",
            limit=subscription.traffic_limit_gb,
        )
        sections.append(
            texts.ADMIN_USER_MANAGEMENT_SUBSCRIPTION.format(
                type=subscription_type,
                status=subscription_status,
                end_date=format_datetime(subscription.end_date),
                traffic=traffic_usage,
                devices=subscription.device_limit,
                countries=len(subscription.connected_squads),
            )
        )
    else:
        sections.append(texts.ADMIN_USER_MANAGEMENT_SUBSCRIPTION_NONE)

    # Display promo groups
    primary_group = user.get_primary_promo_group()
    if primary_group:
        sections.append(
            texts.t(
                "ADMIN_USER_PROMO_GROUPS_PRIMARY",
                "⭐ Основная: {name} (Priority: {priority})",
            ).format(name=primary_group.name, priority=getattr(primary_group, "priority", 0))
        )
        sections.append(
            texts.ADMIN_USER_MANAGEMENT_PROMO_GROUP.format(
                name=primary_group.name,
                server_discount=primary_group.server_discount_percent,
                traffic_discount=primary_group.traffic_discount_percent,
                device_discount=primary_group.device_discount_percent,
            )
        )

        # Show additional groups if any
        if user.user_promo_groups and len(user.user_promo_groups) > 1:
            additional_groups = [
                upg.promo_group for upg in user.user_promo_groups
                if upg.promo_group and upg.promo_group.id != primary_group.id
            ]
            if additional_groups:
                sections.append(
                    texts.t(
                        "ADMIN_USER_PROMO_GROUPS_ADDITIONAL",
                        "Дополнительные группы:",
                    )
                )
                for group in additional_groups:
                    sections.append(
                        f"  • {group.name} (Priority: {getattr(group, 'priority', 0)})"
                    )
    else:
        sections.append(texts.ADMIN_USER_MANAGEMENT_PROMO_GROUP_NONE)

    text = "\n\n".join(sections)

    # Проверяем состояние, чтобы определить, откуда пришел пользователь
    current_state = await state.get_state()
    if current_state == AdminStates.viewing_user_from_balance_list:
        back_callback = "admin_users_balance_filter"
    elif current_state == AdminStates.viewing_user_from_traffic_list:
        back_callback = "admin_users_traffic_filter"
    elif current_state == AdminStates.viewing_user_from_last_activity_list:
        back_callback = "admin_users_activity_filter"
    elif current_state == AdminStates.viewing_user_from_spending_list:
        back_callback = "admin_users_spending_filter"
    elif current_state == AdminStates.viewing_user_from_purchases_list:
        back_callback = "admin_users_purchases_filter"
    elif current_state == AdminStates.viewing_user_from_campaign_list:
        back_callback = "admin_users_campaign_filter"
    elif current_state == AdminStates.viewing_user_from_ready_to_renew_list:
        back_callback = "admin_users_ready_to_renew_filter"
    
    # Базовая клавиатура профиля
    kb = get_user_management_keyboard(
        user.id,
        user.status,
        db_user.language,
        back_callback,
        is_partner=bool(user.is_partner),
    )
    # Если пришли из тикета — добавим в начало кнопку возврата к тикету
    try:
        if origin_ticket_id:
            back_to_ticket_btn = types.InlineKeyboardButton(
                text="🎫 Вернуться к тикету",
                callback_data=f"admin_view_ticket_{origin_ticket_id}"
            )
            kb.inline_keyboard.insert(0, [back_to_ticket_btn])
    except Exception:
        pass

    await callback.message.edit_text(
        text,
        reply_markup=kb
    )
    await callback.answer()


async def _build_user_referrals_view(
    db: AsyncSession,
    language: str,
    user_id: int,
    limit: int = 30,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    texts = get_texts(language)

    user = await get_user_by_id(db, user_id)
    if not user:
        return None

    referrals = await get_referrals(db, user_id)

    effective_percent = get_effective_referral_commission_percent(user)
    default_percent = settings.REFERRAL_COMMISSION_PERCENT

    header = texts.t(
        "ADMIN_USER_REFERRALS_TITLE",
        "🤝 <b>Рефералы пользователя</b>",
    )
    summary = texts.t(
        "ADMIN_USER_REFERRALS_SUMMARY",
        "👤 {name} (ID: <code>{telegram_id}</code>)\n👥 Всего рефералов: {count}",
    ).format(
        name=user.full_name,
        telegram_id=user.telegram_id,
        count=len(referrals),
    )

    lines: List[str] = [header, summary]

    if user.referral_commission_percent is None:
        lines.append(
            texts.t(
                "ADMIN_USER_REFERRAL_COMMISSION_DEFAULT",
                "• Процент комиссии: {percent}% (стандартное значение)",
            ).format(percent=effective_percent)
        )
    else:
        lines.append(
            texts.t(
                "ADMIN_USER_REFERRAL_COMMISSION_CUSTOM",
                "• Индивидуальный процент: {percent}% (стандарт: {default_percent}%)",
            ).format(
                percent=user.referral_commission_percent,
                default_percent=default_percent,
            )
        )

    if referrals:
        lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_LIST_HEADER",
                "<b>Список рефералов:</b>",
            )
        )
        items = []
        for referral in referrals[:limit]:
            username_part = (
                f", @{referral.username}"
                if referral.username
                else ""
            )
            referral_link = f'<a href="tg://user?id={referral.telegram_id}">{referral.full_name}</a>'
            items.append(
                texts.t(
                    "ADMIN_USER_REFERRALS_LIST_ITEM",
                    "• {name} (ID: <code>{telegram_id}</code>{username_part})",
                ).format(
                    name=referral_link,
                    telegram_id=referral.telegram_id,
                    username_part=username_part,
                )
            )

        lines.append("\n".join(items))

        if len(referrals) > limit:
            remaining = len(referrals) - limit
            lines.append(
                texts.t(
                    "ADMIN_USER_REFERRALS_LIST_TRUNCATED",
                    "• … и ещё {count} рефералов",
                ).format(count=remaining)
            )
    else:
        lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_EMPTY",
                "Рефералов пока нет.",
            )
        )

    lines.append(
        texts.t(
            "ADMIN_USER_REFERRALS_EDIT_HINT",
            "✏️ Чтобы изменить список, нажмите «✏️ Редактировать» ниже.",
        )
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        "ADMIN_USER_REFERRAL_COMMISSION_EDIT_BUTTON",
                        "📈 Изменить процент",
                    ),
                    callback_data=f"admin_user_referral_percent_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        "ADMIN_USER_REFERRALS_EDIT_BUTTON",
                        "✏️ Редактировать",
                    ),
                    callback_data=f"admin_user_referrals_edit_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f"admin_user_manage_{user_id}",
                )
            ],
        ]
    )

    return "\n\n".join(lines), keyboard


@admin_required
@error_handler
async def show_user_referrals(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    user_id = int(callback.data.split('_')[-1])

    current_state = await state.get_state()
    if current_state in {AdminStates.editing_user_referrals, AdminStates.editing_user_referral_percent}:
        data = await state.get_data()
        preserved_data = {
            key: value
            for key, value in data.items()
            if key not in {"editing_referrals_user_id", "referrals_message_id", "editing_referral_percent_user_id"}
        }
        await state.clear()
        if preserved_data:
            await state.update_data(**preserved_data)

    view = await _build_user_referrals_view(db, db_user.language, user_id)
    if not view:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    text, keyboard = view

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_referral_percent(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    texts = get_texts(db_user.language)

    effective_percent = get_effective_referral_commission_percent(user)
    default_percent = settings.REFERRAL_COMMISSION_PERCENT

    prompt = texts.t(
        "ADMIN_USER_REFERRAL_COMMISSION_PROMPT",
        (
            "📈 <b>Индивидуальный процент реферальной комиссии</b>\n\n"
            "Текущее значение: {current}%\n"
            "Стандартное значение: {default}%\n\n"
            "Отправьте новое значение от 0 до 100 или слово 'стандарт' для сброса."
        ),
    ).format(current=effective_percent, default=default_percent)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5%",
                    callback_data=f"admin_user_referral_percent_set_{user_id}_5",
                ),
                InlineKeyboardButton(
                    text="10%",
                    callback_data=f"admin_user_referral_percent_set_{user_id}_10",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="15%",
                    callback_data=f"admin_user_referral_percent_set_{user_id}_15",
                ),
                InlineKeyboardButton(
                    text="20%",
                    callback_data=f"admin_user_referral_percent_set_{user_id}_20",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        "ADMIN_USER_REFERRAL_COMMISSION_RESET_BUTTON",
                        "♻️ Сбросить на стандартный",
                    ),
                    callback_data=f"admin_user_referral_percent_reset_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f"admin_user_referrals_{user_id}",
                )
            ],
        ]
    )

    await state.update_data(editing_referral_percent_user_id=user_id)
    await state.set_state(AdminStates.editing_user_referral_percent)

    await callback.message.edit_text(
        prompt,
        reply_markup=keyboard,
    )
    await callback.answer()


async def toggle_user_partner_account(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Включает или снимает партнёрский аккаунт: комиссия 60% вместо 50% и
    полное отключение программы лучей."""
    texts = get_texts(db_user.language)
    user_id = int(callback.data.rsplit("_", 1)[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(
            texts.t("ADMIN_USER_NOT_FOUND", "Пользователь не найден"),
            show_alert=True,
        )
        return

    user.is_partner = not bool(user.is_partner)
    user.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    percent = get_effective_referral_commission_percent(user)
    logger.info(
        "Админ %s %s партнёрский аккаунт пользователю %s (комиссия %s%%)",
        callback.from_user.id,
        "выдал" if user.is_partner else "снял",
        user_id,
        percent,
    )

    if user.is_partner:
        alert = texts.t(
            "ADMIN_USER_PARTNER_ENABLED",
            "🤝 Партнёрский аккаунт включён. Комиссия: {percent}%. Лучи отключены.",
        ).format(percent=percent)
    else:
        alert = texts.t(
            "ADMIN_USER_PARTNER_DISABLED",
            "Партнёрский аккаунт снят. Комиссия: {percent}%.",
        ).format(percent=percent)

    await callback.answer(alert, show_alert=True)

    # Перерисовываем только клавиатуру: подпись кнопки зависит от флага.
    # Кнопку «Назад» берём из текущей разметки, чтобы не увести админа из
    # ветки, из которой он пришёл (например, из карточки тикета).
    back_callback = "admin_users_list"
    markup = callback.message.reply_markup if callback.message else None
    if markup and markup.inline_keyboard and markup.inline_keyboard[-1]:
        last = markup.inline_keyboard[-1][0]
        if last.callback_data:
            back_callback = last.callback_data

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_user_management_keyboard(
                user.id,
                user.status,
                db_user.language,
                back_callback,
                is_partner=bool(user.is_partner),
            )
        )
    except TelegramBadRequest as error:
        logger.debug("Не удалось обновить клавиатуру карточки пользователя: %s", error)


async def _update_referral_commission_percent(
    db: AsyncSession,
    user_id: int,
    percent: Optional[int],
    admin_id: int,
) -> Tuple[bool, Optional[int]]:
    try:
        user = await get_user_by_id(db, user_id)
        if not user:
            return False, None

        user.referral_commission_percent = percent
        user.updated_at = datetime.utcnow()

        await db.commit()

        effective = get_effective_referral_commission_percent(user)

        logger.info(
            "Админ %s обновил реферальный процент пользователя %s: %s",
            admin_id,
            user_id,
            percent,
        )

        return True, effective
    except Exception as e:
        logger.error(
            "Ошибка обновления реферального процента пользователя %s: %s",
            user_id,
            e,
        )
        try:
            await db.rollback()
        except Exception as rollback_error:
            logger.error("Ошибка отката транзакции: %s", rollback_error)
        return False, None


async def _render_referrals_after_update(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User,
    user_id: int,
    success_message: str,
):
    view = await _build_user_referrals_view(db, db_user.language, user_id)
    if view:
        text, keyboard = view
        text = f"{success_message}\n\n" + text
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.edit_text(success_message)


@admin_required
@error_handler
async def set_referral_percent_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    parts = callback.data.split('_')

    if "reset" in parts:
        user_id = int(parts[-1])
        percent_value: Optional[int] = None
    else:
        user_id = int(parts[-2])
        percent_value = int(parts[-1])

    texts = get_texts(db_user.language)

    success, effective_percent = await _update_referral_commission_percent(
        db,
        user_id,
        percent_value,
        db_user.id,
    )

    if not success:
        await callback.answer("❌ Не удалось обновить процент", show_alert=True)
        return

    await state.clear()

    success_message = texts.t(
        "ADMIN_USER_REFERRAL_COMMISSION_UPDATED",
        "✅ Процент обновлён: {percent}%",
    ).format(percent=effective_percent)

    await _render_referrals_after_update(callback, db, db_user, user_id, success_message)
    await callback.answer()


@admin_required
@error_handler
async def process_referral_percent_input(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    user_id = data.get("editing_referral_percent_user_id")

    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return

    raw_text = message.text.strip()
    normalized = raw_text.lower()

    percent_value: Optional[int]

    if normalized in {"стандарт", "standard", "default"}:
        percent_value = None
    else:
        normalized_number = raw_text.replace(',', '.').strip()
        try:
            percent_float = float(normalized_number)
        except (TypeError, ValueError):
            await message.answer(
                get_texts(db_user.language).t(
                    "ADMIN_USER_REFERRAL_COMMISSION_INVALID",
                    "❌ Введите число от 0 до 100 или слово 'стандарт'",
                )
            )
            return

        percent_value = int(round(percent_float))

        if percent_value < 0 or percent_value > 100:
            await message.answer(
                get_texts(db_user.language).t(
                    "ADMIN_USER_REFERRAL_COMMISSION_INVALID",
                    "❌ Введите число от 0 до 100 или слово 'стандарт'",
                )
            )
            return

    texts = get_texts(db_user.language)

    success, effective_percent = await _update_referral_commission_percent(
        db,
        int(user_id),
        percent_value,
        db_user.id,
    )

    if not success:
        await message.answer("❌ Не удалось обновить процент")
        return

    await state.clear()

    success_message = texts.t(
        "ADMIN_USER_REFERRAL_COMMISSION_UPDATED",
        "✅ Процент обновлён: {percent}%",
    ).format(percent=effective_percent)

    view = await _build_user_referrals_view(db, db_user.language, int(user_id))
    if view:
        text, keyboard = view
        await message.answer(f"{success_message}\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer(success_message)


@admin_required
@error_handler
async def start_edit_user_referrals(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    texts = get_texts(db_user.language)

    prompt = texts.t(
        "ADMIN_USER_REFERRALS_EDIT_PROMPT",
        (
            "✏️ <b>Редактирование рефералов</b>\n\n"
            "Отправьте список рефералов для пользователя <b>{name}</b> (ID: <code>{telegram_id}</code>):\n"
            "• Используйте TG ID или @username\n"
            "• Значения можно указывать через запятую, пробел или с новой строки\n"
            "• Чтобы очистить список, отправьте 0 или слово 'нет'\n\n"
            "Или нажмите кнопку ниже, чтобы отменить."
        ),
    ).format(
        name=user.full_name,
        telegram_id=user.telegram_id,
    )

    await state.update_data(
        editing_referrals_user_id=user_id,
        referrals_message_id=callback.message.message_id,
    )

    await callback.message.edit_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.BACK,
                        callback_data=f"admin_user_referrals_{user_id}",
                    )
                ]
            ]
        ),
    )

    await state.set_state(AdminStates.editing_user_referrals)
    await callback.answer()


@admin_required
@error_handler
async def process_edit_user_referrals(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    user_id = data.get("editing_referrals_user_id")
    if not user_id:
        await message.answer(
            texts.t(
                "ADMIN_USER_REFERRALS_STATE_LOST",
                "❌ Не удалось определить пользователя. Попробуйте начать сначала.",
            )
        )
        await state.clear()
        return

    raw_text = message.text.strip()
    lower_text = raw_text.lower()
    clear_keywords = {"0", "нет", "none", "пусто", "clear"}
    clear_requested = lower_text in clear_keywords

    tokens: List[str] = []
    if not clear_requested:
        parts = re.split(r"[,\n]+", raw_text)
        for part in parts:
            for token in part.split():
                cleaned = token.strip()
                if cleaned and cleaned not in tokens:
                    tokens.append(cleaned)

    found_users: List[User] = []
    not_found: List[str] = []
    skipped_self: List[str] = []
    duplicate_tokens: List[str] = []

    seen_ids = set()

    for token in tokens:
        normalized = token.strip()
        if not normalized:
            continue

        if normalized.startswith("@"):
            normalized = normalized[1:]

        user = None
        if normalized.isdigit():
            try:
                user = await get_user_by_telegram_id(db, int(normalized))
            except ValueError:
                user = None
        else:
            user = await get_user_by_username(db, normalized)

        if not user:
            not_found.append(token)
            continue

        if user.id == user_id:
            skipped_self.append(token)
            continue

        if user.id in seen_ids:
            duplicate_tokens.append(token)
            continue

        seen_ids.add(user.id)
        found_users.append(user)

    if not found_users and not clear_requested:
        error_lines = [
            texts.t(
                "ADMIN_USER_REFERRALS_NO_VALID",
                "❌ Не удалось найти ни одного пользователя по введённым данным.",
            )
        ]
        if not_found:
            error_lines.append(
                texts.t(
                    "ADMIN_USER_REFERRALS_INVALID_ENTRIES",
                    "Не найдены: {values}",
                ).format(values=", ".join(not_found))
            )
        if skipped_self:
            error_lines.append(
                texts.t(
                    "ADMIN_USER_REFERRALS_SELF_SKIPPED",
                    "Пропущены значения пользователя: {values}",
                ).format(values=", ".join(skipped_self))
            )
        await message.answer("\n".join(error_lines))
        return

    user_service = UserService()

    new_referral_ids = [user.id for user in found_users] if not clear_requested else []

    success, details = await user_service.update_user_referrals(
        db,
        user_id,
        new_referral_ids,
        db_user.id,
    )

    if not success:
        await message.answer(
            texts.t(
                "ADMIN_USER_REFERRALS_UPDATE_ERROR",
                "❌ Не удалось обновить рефералов. Попробуйте позже.",
            )
        )
        return

    response_lines = [
        texts.t(
            "ADMIN_USER_REFERRALS_UPDATED",
            "✅ Список рефералов обновлён.",
        )
    ]

    total_referrals = details.get("total", len(new_referral_ids))
    added = details.get("added", 0)
    removed = details.get("removed", 0)

    response_lines.append(
        texts.t(
            "ADMIN_USER_REFERRALS_UPDATED_TOTAL",
            "• Текущий список: {total}",
        ).format(total=total_referrals)
    )

    if added > 0:
        response_lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_UPDATED_ADDED",
                "• Добавлено: {count}",
            ).format(count=added)
        )

    if removed > 0:
        response_lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_UPDATED_REMOVED",
                "• Удалено: {count}",
            ).format(count=removed)
        )

    if not_found:
        response_lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_INVALID_ENTRIES",
                "Не найдены: {values}",
            ).format(values=", ".join(not_found))
        )

    if skipped_self:
        response_lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_SELF_SKIPPED",
                "Пропущены значения пользователя: {values}",
            ).format(values=", ".join(skipped_self))
        )

    if duplicate_tokens:
        response_lines.append(
            texts.t(
                "ADMIN_USER_REFERRALS_DUPLICATES",
                "Игнорированы дубли: {values}",
            ).format(values=", ".join(duplicate_tokens))
        )

    view = await _build_user_referrals_view(db, db_user.language, user_id)
    message_id = data.get("referrals_message_id")

    if view and message_id:
        try:
            await message.bot.edit_message_text(
                view[0],
                chat_id=message.chat.id,
                message_id=message_id,
                reply_markup=view[1],
            )
        except TelegramBadRequest:
            await message.answer(view[0], reply_markup=view[1])
    elif view:
        await message.answer(view[0], reply_markup=view[1])

    await message.answer("\n".join(response_lines))
    await state.clear()

async def _render_user_promo_group(
    message: types.Message,
    language: str,
    user: User,
    promo_groups: list
) -> None:
    texts = get_texts(language)

    # Get primary and all user groups
    primary_group = user.get_primary_promo_group()
    user_group_ids = [upg.promo_group_id for upg in user.user_promo_groups] if user.user_promo_groups else []

    # Build current groups section
    if primary_group:
        current_line = texts.t(
            "ADMIN_USER_PROMO_GROUPS_PRIMARY",
            "⭐ Основная: {name} (Priority: {priority})",
        ).format(name=primary_group.name, priority=getattr(primary_group, "priority", 0))

        discount_line = texts.ADMIN_USER_PROMO_GROUP_DISCOUNTS.format(
            servers=primary_group.server_discount_percent,
            traffic=primary_group.traffic_discount_percent,
            devices=primary_group.device_discount_percent,
        )

        # Show additional groups if any
        if len(user_group_ids) > 1:
            additional_groups = [
                upg.promo_group for upg in user.user_promo_groups
                if upg.promo_group and upg.promo_group.id != primary_group.id
            ]
            if additional_groups:
                additional_line = "\n" + texts.t(
                    "ADMIN_USER_PROMO_GROUPS_ADDITIONAL",
                    "Дополнительные группы:",
                ) + "\n"
                for group in additional_groups:
                    additional_line += f"  • {group.name} (Priority: {getattr(group, 'priority', 0)})\n"
                discount_line += additional_line
    else:
        current_line = texts.t(
            "ADMIN_USER_PROMO_GROUPS_NONE",
            "У пользователя нет промогрупп",
        )
        discount_line = ""

    text = (
        f"{texts.ADMIN_USER_PROMO_GROUP_TITLE}\n\n"
        f"{current_line}\n"
        f"{discount_line}\n\n"
        f"{texts.ADMIN_USER_PROMO_GROUP_SELECT}"
    )

    await message.edit_text(
        text,
        reply_markup=get_user_promo_group_keyboard(
            promo_groups,
            user.id,
            user_group_ids,  # Pass list of all group IDs
            language
        )
    )


@admin_required
@error_handler
async def show_user_promo_group(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):

    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    promo_groups = await get_promo_groups_with_counts(db)
    if not promo_groups:
        texts = get_texts(db_user.language)
        await callback.answer(texts.ADMIN_PROMO_GROUPS_EMPTY, show_alert=True)
        return

    await _render_user_promo_group(callback.message, db_user.language, user, promo_groups)
    await callback.answer()


@admin_required
@error_handler
async def set_user_promo_group(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    from app.database.crud.user_promo_group import (
        has_user_promo_group,
        add_user_to_promo_group,
        remove_user_from_promo_group,
        count_user_promo_groups
    )
    from app.database.crud.promo_group import get_promo_group_by_id

    parts = callback.data.split('_')
    user_id = int(parts[-2])
    group_id = int(parts[-1])

    texts = get_texts(db_user.language)

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    # Check if user already has this group
    has_group = await has_user_promo_group(db, user_id, group_id)

    if has_group:
        # Remove group
        # Check if it's the last group
        groups_count = await count_user_promo_groups(db, user_id)
        if groups_count <= 1:
            await callback.answer(
                texts.t(
                    "ADMIN_USER_PROMO_GROUP_CANNOT_REMOVE_LAST",
                    "❌ Нельзя удалить последнюю промогруппу",
                ),
                show_alert=True
            )
            return

        group = await get_promo_group_by_id(db, group_id)
        await remove_user_from_promo_group(db, user_id, group_id)
        await callback.answer(
            texts.t(
                "ADMIN_USER_PROMO_GROUP_REMOVED",
                "🗑 Группа «{name}» удалена",
            ).format(name=group.name if group else ""),
            show_alert=True
        )
    else:
        # Add group
        group = await get_promo_group_by_id(db, group_id)
        if not group:
            await callback.answer(texts.ADMIN_USER_PROMO_GROUP_ERROR, show_alert=True)
            return

        await add_user_to_promo_group(db, user_id, group_id, assigned_by="admin")
        await callback.answer(
            texts.t(
                "ADMIN_USER_PROMO_GROUP_ADDED",
                "✅ Группа «{name}» добавлена",
            ).format(name=group.name),
            show_alert=True
        )

    # Refresh user data and show updated list
    user = await get_user_by_id(db, user_id)
    promo_groups = await get_promo_groups_with_counts(db)
    await _render_user_promo_group(callback.message, db_user.language, user, promo_groups)



@admin_required
@error_handler
async def start_balance_edit(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    
    user_id = int(callback.data.split('_')[-1])
    
    await state.update_data(editing_user_id=user_id)
    
    await callback.message.edit_text(
        "💰 <b>Изменение баланса</b>\n\n"
        "Введите сумму для изменения баланса:\n"
        "• Положительное число для пополнения\n"
        "• Отрицательное число для списания\n"
        "• Примеры: 100, -50, 25.5\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_manage_{user_id}")]
        ])
    )
    
    await state.set_state(AdminStates.editing_user_balance)
    await callback.answer()


@admin_required
@error_handler
async def start_send_user_message(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    user_id = int(callback.data.split('_')[-1])

    target_user = await get_user_by_id(db, user_id)
    if not target_user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    await state.update_data(direct_message_user_id=user_id)

    texts = get_texts(db_user.language)
    prompt = (
        texts.t("ADMIN_USER_SEND_MESSAGE_PROMPT",
                 "✉️ <b>Отправка сообщения пользователю</b>\n\n"
                 "Введите текст, который бот отправит пользователю."
                 "\n\nВы можете отменить действие командой /cancel или кнопкой ниже." )
    )

    await callback.message.edit_text(
        prompt,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_manage_{user_id}")]
            ]
        ),
        parse_mode="HTML",
    )

    await state.set_state(AdminStates.sending_user_message)
    await callback.answer()


@admin_required
@error_handler
async def process_send_user_message(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get("direct_message_user_id")

    if not user_id:
        await message.answer(texts.t("ADMIN_USER_SEND_MESSAGE_ERROR_NOT_FOUND", "❌ Пользователь для отправки сообщения не найден"))
        await state.clear()
        return

    target_user = await get_user_by_id(db, int(user_id))
    if not target_user:
        await message.answer(texts.t("ADMIN_USER_SEND_MESSAGE_ERROR_NOT_FOUND", "❌ Пользователь не найден или был удалён"))
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer(texts.t("ADMIN_USER_SEND_MESSAGE_EMPTY", "❌ Пожалуйста, введите непустое сообщение"))
        return

    confirmation_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]]
    )

    try:
        await message.bot.send_message(target_user.telegram_id, text)
        await message.answer(
            texts.t("ADMIN_USER_SEND_MESSAGE_SUCCESS", "✅ Сообщение отправлено пользователю"),
            reply_markup=confirmation_keyboard,
        )
    except TelegramForbiddenError:
        await message.answer(
            texts.t("ADMIN_USER_SEND_MESSAGE_FORBIDDEN", "⚠️ Пользователь заблокировал бота или не может получить сообщения."),
            reply_markup=confirmation_keyboard,
        )
    except TelegramBadRequest as err:
        logger.error("Ошибка отправки сообщения пользователю %s: %s", target_user.telegram_id, err)
        await message.answer(
            texts.t("ADMIN_USER_SEND_MESSAGE_BAD_REQUEST", "❌ Telegram отклонил сообщение. Проверьте текст и попробуйте ещё раз."),
            reply_markup=confirmation_keyboard,
        )
        return
    except Exception as err:
        logger.error("Неожиданная ошибка отправки сообщения пользователю %s: %s", target_user.telegram_id, err)
        await message.answer(
            texts.t("ADMIN_USER_SEND_MESSAGE_ERROR", "❌ Не удалось отправить сообщение. Попробуйте позже."),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return

    await state.clear()


@admin_required
@error_handler
async def process_balance_edit(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    
    data = await state.get_data()
    user_id = data.get("editing_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: пользователь не найден")
        await state.clear()
        return
    
    try:
        amount_rubles = float(message.text.replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)
        
        if abs(amount_kopeks) > 10000000: 
            await message.answer("❌ Слишком большая сумма (максимум 100,000 ₽)")
            return
        
        user_service = UserService()
        
        description = f"Изменение баланса администратором {db_user.full_name}"
        if amount_kopeks > 0:
            description = f"Пополнение администратором: +{int(amount_rubles)} ₽"
        else:
            description = f"Списание администратором: {int(amount_rubles)} ₽"
        
        success = await user_service.update_user_balance(
            db, user_id, amount_kopeks, description, db_user.id,
            bot=message.bot, admin_name=db_user.full_name
        )
        
        if success:
            action = "пополнен" if amount_kopeks > 0 else "списан"
            await message.answer(
                f"✅ Баланс пользователя {action} на {settings.format_price(abs(amount_kopeks))}",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]
                ])
            )
        else:
            await message.answer("❌ Ошибка изменения баланса (возможно, недостаточно средств для списания)")
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму (например: 100 или -50)")
        return
    
    await state.clear()


@admin_required
@error_handler
async def confirm_user_block(
    callback: types.CallbackQuery,
    db_user: User
):
    
    user_id = int(callback.data.split('_')[-1])
    
    await callback.message.edit_text(
        "🚫 <b>Блокировка пользователя</b>\n\n"
        "Вы уверены, что хотите заблокировать этого пользователя?\n"
        "Пользователь потеряет доступ к боту.",
        reply_markup=get_confirmation_keyboard(
            f"admin_user_block_confirm_{user_id}",
            f"admin_user_manage_{user_id}",
            db_user.language
        )
    )
    await callback.answer()


@admin_required
@error_handler
async def block_user(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_id = int(callback.data.split('_')[-1])
    
    user_service = UserService()
    success = await user_service.block_user(
        db, user_id, db_user.id, "Заблокирован администратором"
    )
    
    if success:
        await callback.message.edit_text(
            "✅ Пользователь заблокирован",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка блокировки пользователя",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def show_inactive_users(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_service = UserService()
    
    from app.database.crud.user import get_inactive_users
    inactive_users = await get_inactive_users(db, settings.INACTIVE_USER_DELETE_MONTHS)
    
    if not inactive_users:
        await callback.message.edit_text(
            f"✅ Неактивных пользователей (более {settings.INACTIVE_USER_DELETE_MONTHS} месяцев) не найдено",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")]
            ])
        )
        await callback.answer()
        return
    
    text = f"🗑️ <b>Неактивные пользователи</b>\n"
    text += f"Без активности более {settings.INACTIVE_USER_DELETE_MONTHS} месяцев: {len(inactive_users)}\n\n"

    for user in inactive_users[:10]:
        user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
        text += f"👤 {user_link}\n"
        text += f"🆔 <code>{user.telegram_id}</code>\n"
        last_activity_display = (
            format_time_ago(user.last_activity, db_user.language)
            if user.last_activity
            else "Никогда"
        )
        text += f"📅 {last_activity_display}\n\n"
    
    if len(inactive_users) > 10:
        text += f"... и еще {len(inactive_users) - 10} пользователей"
    
    keyboard = [
        [types.InlineKeyboardButton(text="🗑️ Очистить всех", callback_data="admin_cleanup_inactive")],
        [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")]
    ]
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@admin_required
@error_handler
async def confirm_user_unblock(
    callback: types.CallbackQuery,
    db_user: User
):
    
    user_id = int(callback.data.split('_')[-1])
    
    await callback.message.edit_text(
        "✅ <b>Разблокировка пользователя</b>\n\n"
        "Вы уверены, что хотите разблокировать этого пользователя?\n"
        "Пользователь снова получит доступ к боту.",
        reply_markup=get_confirmation_keyboard(
            f"admin_user_unblock_confirm_{user_id}",
            f"admin_user_manage_{user_id}",
            db_user.language
        )
    )
    await callback.answer()


@admin_required
@error_handler
async def unblock_user(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_id = int(callback.data.split('_')[-1])
    
    user_service = UserService()
    success = await user_service.unblock_user(db, user_id, db_user.id)
    
    if success:
        await callback.message.edit_text(
            "✅ Пользователь разблокирован",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка разблокировки пользователя",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin_user_manage_{user_id}")]
            ])
        )
    
    await callback.answer()

@admin_required
@error_handler
async def show_user_statistics(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_id = int(callback.data.split('_')[-1])
    
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    
    if not profile:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    user = profile["user"]
    subscription = profile["subscription"]
    
    referral_stats = await get_detailed_referral_stats(db, user.id)
    campaign_registration = await get_campaign_registration_by_user(db, user.id)
    campaign_stats = None
    if campaign_registration:
        campaign_stats = await get_campaign_statistics(db, campaign_registration.campaign_id)
    
    text = f"📊 <b>Статистика пользователя</b>\n\n"
    user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
    text += f"👤 {user_link} (ID: <code>{user.telegram_id}</code>)\n\n"
    
    text += f"<b>Основная информация:</b>\n"
    text += f"• Дней с регистрации: {profile['registration_days']}\n"
    text += f"• Баланс: {settings.format_price(user.balance_kopeks)}\n"
    text += f"• Транзакций: {profile['transactions_count']}\n"
    text += (
        f"• ✨ Лучи: {user.rays_balance or 0} "
        f"(всего заработано: {user.rays_lifetime_earned or 0})\n"
    )
    text += f"• Язык: {user.language}\n\n"
    
    text += f"<b>Подписка:</b>\n"
    if subscription:
        sub_status = "✅ Активна" if subscription.is_active else "❌ Неактивна"
        sub_type = " (пробная)" if subscription.is_trial else " (платная)"
        text += f"• Статус: {sub_status}{sub_type}\n"
        text += f"• Трафик: {subscription.traffic_used_gb:.1f}/{subscription.traffic_limit_gb} ГБ\n"
        text += f"• Устройства: {subscription.device_limit}\n"
        text += f"• Стран: {len(subscription.connected_squads)}\n"
    else:
        text += f"• Отсутствует\n"
    
    text += f"\n<b>Реферальная программа:</b>\n"

    if user.referred_by_id:
        referrer = await get_user_by_id(db, user.referred_by_id)
        if referrer:
            text += f"• Пришел по реферальной ссылке от <b>{referrer.full_name}</b>\n"
        else:
            text += "• Пришел по реферальной ссылке (реферер не найден)\n"
        if campaign_registration and campaign_registration.campaign:
            text += (
                "• Дополнительно зарегистрирован через кампанию "
                f"<b>{campaign_registration.campaign.name}</b>\n"
            )
    elif campaign_registration and campaign_registration.campaign:
        text += (
            "• Регистрация через рекламную кампанию "
            f"<b>{campaign_registration.campaign.name}</b>\n"
        )
        if campaign_registration.created_at:
            text += (
                "• Дата регистрации по кампании: "
                f"{campaign_registration.created_at.strftime('%d.%m.%Y %H:%M')}\n"
            )
    else:
        text += "• Прямая регистрация\n"

    text += f"• Реферальный код: <code>{user.referral_code}</code>\n\n"

    if campaign_registration and campaign_registration.campaign and campaign_stats:
        text += "<b>Рекламная кампания:</b>\n"
        text += (
            "• Название: "
            f"<b>{campaign_registration.campaign.name}</b>"
        )
        if campaign_registration.campaign.start_parameter:
            text += (
                " (параметр: "
                f"<code>{campaign_registration.campaign.start_parameter}</code>)"
            )
        text += "\n"
        text += (
            "• Всего регистраций: "
            f"{campaign_stats['registrations']}\n"
        )
        text += (
            "• Суммарный доход: "
            f"{settings.format_price(campaign_stats['total_revenue_kopeks'])}\n"
        )
        text += (
            "• Получили триал: "
            f"{campaign_stats['trial_users_count']}"
            f" (активно: {campaign_stats['active_trials_count']})\n"
        )
        text += (
            "• Конверсий в оплату: "
            f"{campaign_stats['conversion_count']}"
            f" (оплативших пользователей: {campaign_stats['paid_users_count']})\n"
        )
        text += (
            "• Конверсия в оплату: "
            f"{campaign_stats['conversion_rate']:.1f}%\n"
        )
        text += (
            "• Конверсия триала: "
            f"{campaign_stats['trial_conversion_rate']:.1f}%\n"
        )
        text += (
            "• Средний доход на пользователя: "
            f"{settings.format_price(campaign_stats['avg_revenue_per_user_kopeks'])}\n"
        )
        text += (
            "• Средний первый платеж: "
            f"{settings.format_price(campaign_stats['avg_first_payment_kopeks'])}\n"
        )
        text += "\n"
    
    if referral_stats['invited_count'] > 0:
        text += f"<b>Доходы от рефералов:</b>\n"
        text += f"• Всего приглашено: {referral_stats['invited_count']}\n"
        text += f"• Активных рефералов: {referral_stats['active_referrals']}\n"
        text += f"• Общий доход: {settings.format_price(referral_stats['total_earned_kopeks'])}\n"
        text += f"• Доход за месяц: {settings.format_price(referral_stats['month_earned_kopeks'])}\n"
        
        if referral_stats['referrals_detail']:
            text += f"\n<b>Детали по рефералам:</b>\n"
            for detail in referral_stats['referrals_detail'][:5]: 
                referral_name = detail['referral_name']
                earned = settings.format_price(detail['total_earned_kopeks'])
                status = "🟢" if detail['is_active'] else "🔴"
                text += f"• {status} {referral_name}: {earned}\n"
            
            if len(referral_stats['referrals_detail']) > 5:
                text += f"• ... и еще {len(referral_stats['referrals_detail']) - 5} рефералов\n"
    else:
        text += f"<b>Реферальная программа:</b>\n"
        text += f"• Рефералов нет\n"
        text += f"• Доходов нет\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⬅️ К пользователю", callback_data=f"admin_user_manage_{user_id}")]
        ])
    )
    await callback.answer()


async def get_detailed_referral_stats(db: AsyncSession, user_id: int) -> dict:
    from app.database.crud.referral import get_user_referral_stats, get_referral_earnings_by_user
    from sqlalchemy import select, func
    from sqlalchemy.orm import selectinload
    
    base_stats = await get_user_referral_stats(db, user_id)
    
    referrals_query = select(User).options(
        selectinload(User.subscription)
    ).where(User.referred_by_id == user_id)
    
    referrals_result = await db.execute(referrals_query)
    referrals = referrals_result.scalars().all()
    
    earnings_by_referral = {}
    all_earnings = await get_referral_earnings_by_user(db, user_id)
    
    for earning in all_earnings:
        referral_id = earning.referral_id
        if referral_id not in earnings_by_referral:
            earnings_by_referral[referral_id] = 0
        earnings_by_referral[referral_id] += earning.amount_kopeks
    
    referrals_detail = []
    current_time = datetime.utcnow()
    
    for referral in referrals:
        earned = earnings_by_referral.get(referral.id, 0)
        
        is_active = False
        if referral.subscription:
            from app.database.models import SubscriptionStatus
            is_active = (
                referral.subscription.status == SubscriptionStatus.ACTIVE.value and 
                referral.subscription.end_date > current_time
            )
        
        referrals_detail.append({
            'referral_id': referral.id,
            'referral_name': referral.full_name,
            'referral_telegram_id': referral.telegram_id,
            'total_earned_kopeks': earned,
            'is_active': is_active,
            'registration_date': referral.created_at,
            'has_subscription': bool(referral.subscription)
        })
    
    referrals_detail.sort(key=lambda x: x['total_earned_kopeks'], reverse=True)
    
    return {
        'invited_count': base_stats['invited_count'],
        'active_referrals': base_stats['active_referrals'], 
        'total_earned_kopeks': base_stats['total_earned_kopeks'],
        'month_earned_kopeks': base_stats['month_earned_kopeks'],
        'referrals_detail': referrals_detail
    }
    
@admin_required
@error_handler
async def extend_user_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    user_id = int(callback.data.split('_')[-1])
    
    await state.update_data(extending_user_id=user_id)
    
    await callback.message.edit_text(
        "⏰ <b>Продление подписки</b>\n\n"
        "Введите количество дней для изменения:\n"
        "• Положительные значения продлят подписку\n"
        "• Отрицательные сократят срок подписки\n"
        "• Диапазон: от -365 до 365 дней (0 недопустимо)\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="-7 дней", callback_data=f"admin_sub_extend_days_{user_id}_-7"),
                types.InlineKeyboardButton(text="-30 дней", callback_data=f"admin_sub_extend_days_{user_id}_-30")
            ],
            [
                types.InlineKeyboardButton(text="7 дней", callback_data=f"admin_sub_extend_days_{user_id}_7"),
                types.InlineKeyboardButton(text="30 дней", callback_data=f"admin_sub_extend_days_{user_id}_30")
            ],
            [
                types.InlineKeyboardButton(text="90 дней", callback_data=f"admin_sub_extend_days_{user_id}_90"),
                types.InlineKeyboardButton(text="180 дней", callback_data=f"admin_sub_extend_days_{user_id}_180")
            ],
            [
                types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_subscription_{user_id}")
            ]
        ])
    )
    
    await state.set_state(AdminStates.extending_subscription)
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_extension_days(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    days = int(parts[-1])
    
    if days == 0 or days < -365 or days > 365:
        await callback.answer("❌ Количество дней должно быть от -365 до 365, исключая 0", show_alert=True)
        return

    success = await _extend_subscription_by_days(db, user_id, days, db_user.id)

    if success:
        if days > 0:
            action_text = f"продлена на {days} дней"
        else:
            action_text = f"уменьшена на {abs(days)} дней"
        await callback.message.edit_text(
            f"✅ Подписка пользователя {action_text}",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка продления подписки",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_extension_text(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    user_id = data.get("extending_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: пользователь не найден")
        await state.clear()
        return
    
    try:
        days = int(message.text.strip())
        
        if days == 0 or days < -365 or days > 365:
            await message.answer("❌ Количество дней должно быть от -365 до 365, исключая 0")
            return

        success = await _extend_subscription_by_days(db, user_id, days, db_user.id)

        if success:
            if days > 0:
                action_text = f"продлена на {days} дней"
            else:
                action_text = f"уменьшена на {abs(days)} дней"
            await message.answer(
                f"✅ Подписка пользователя {action_text}",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
        else:
            await message.answer("❌ Ошибка продления подписки")
        
    except ValueError:
        await message.answer("❌ Введите корректное число дней")
        return
    
    await state.clear()


@admin_required
@error_handler
async def add_subscription_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    user_id = int(callback.data.split('_')[-1])
    
    await state.update_data(traffic_user_id=user_id)
    
    await callback.message.edit_text(
        "📊 <b>Добавление трафика</b>\n\n"
        "Введите количество ГБ для добавления:\n"
        "• Например: 50, 100, 500\n"
        "• Максимум: 10000 ГБ\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="50 ГБ", callback_data=f"admin_sub_traffic_add_{user_id}_50"),
                types.InlineKeyboardButton(text="100 ГБ", callback_data=f"admin_sub_traffic_add_{user_id}_100")
            ],
            [
                types.InlineKeyboardButton(text="500 ГБ", callback_data=f"admin_sub_traffic_add_{user_id}_500"),
                types.InlineKeyboardButton(text="1000 ГБ", callback_data=f"admin_sub_traffic_add_{user_id}_1000")
            ],
            [
                types.InlineKeyboardButton(text="♾️ Безлимит", callback_data=f"admin_sub_traffic_add_{user_id}_0"),
            ],
            [
                types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_subscription_{user_id}")
            ]
        ])
    )
    
    await state.set_state(AdminStates.adding_traffic)
    await callback.answer()


@admin_required
@error_handler
async def process_traffic_addition_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    gb = int(parts[-1])
    
    success = await _add_subscription_traffic(db, user_id, gb, db_user.id)
    
    if success:
        traffic_text = "♾️ безлимитный" if gb == 0 else f"{gb} ГБ"
        await callback.message.edit_text(
            f"✅ К подписке пользователя добавлен трафик: {traffic_text}",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка добавления трафика",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def process_traffic_addition_text(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    user_id = data.get("traffic_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: пользователь не найден")
        await state.clear()
        return
    
    try:
        gb = int(message.text.strip())
        
        if gb < 0 or gb > 10000:
            await message.answer("❌ Количество ГБ должно быть от 0 до 10000 (0 = безлимит)")
            return
        
        success = await _add_subscription_traffic(db, user_id, gb, db_user.id)
        
        if success:
            traffic_text = "♾️ безлимитный" if gb == 0 else f"{gb} ГБ"
            await message.answer(
                f"✅ К подписке пользователя добавлен трафик: {traffic_text}",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
        else:
            await message.answer("❌ Ошибка добавления трафика")
        
    except ValueError:
        await message.answer("❌ Введите корректное число ГБ")
        return
    
    await state.clear()


@admin_required
@error_handler
async def deactivate_user_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    await callback.message.edit_text(
        "🚫 <b>Деактивация подписки</b>\n\n"
        "Вы уверены, что хотите деактивировать подписку этого пользователя?\n"
        "Пользователь потеряет доступ к сервису.",
        reply_markup=get_confirmation_keyboard(
            f"admin_sub_deactivate_confirm_{user_id}",
            f"admin_user_subscription_{user_id}",
            db_user.language
        )
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_subscription_deactivation(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    success = await _deactivate_user_subscription(db, user_id, db_user.id)
    
    if success:
        await callback.message.edit_text(
            "✅ Подписка пользователя деактивирована",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка деактивации подписки",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def activate_user_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    success = await _activate_user_subscription(db, user_id, db_user.id)
    
    if success:
        await callback.message.edit_text(
            "✅ Подписка пользователя активирована",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка активации подписки",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def grant_trial_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    success = await _grant_trial_subscription(db, user_id, db_user.id)
    
    if success:
        await callback.message.edit_text(
            "✅ Пользователю выдан триальный период",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка выдачи триального периода",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def grant_paid_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    user_id = int(callback.data.split('_')[-1])
    
    await state.update_data(granting_user_id=user_id)
    
    await callback.message.edit_text(
        "💎 <b>Выдача подписки</b>\n\n"
        "Введите количество дней подписки:\n"
        "• Например: 30, 90, 180, 365\n"
        "• Максимум: 730 дней\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="30 дней", callback_data=f"admin_sub_grant_days_{user_id}_30"),
                types.InlineKeyboardButton(text="90 дней", callback_data=f"admin_sub_grant_days_{user_id}_90")
            ],
            [
                types.InlineKeyboardButton(text="180 дней", callback_data=f"admin_sub_grant_days_{user_id}_180"),
                types.InlineKeyboardButton(text="365 дней", callback_data=f"admin_sub_grant_days_{user_id}_365")
            ],
            [
                types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_subscription_{user_id}")
            ]
        ])
    )
    
    await state.set_state(AdminStates.granting_subscription)
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_grant_days(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    days = int(parts[-1])
    
    success = await _grant_paid_subscription(db, user_id, days, db_user.id)
    
    if success:
        await callback.message.edit_text(
            f"✅ Пользователю выдана подписка на {days} дней",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка выдачи подписки",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_grant_text(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    user_id = data.get("granting_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: пользователь не найден")
        await state.clear()
        return
    
    try:
        days = int(message.text.strip())
        
        if days <= 0 or days > 730:
            await message.answer("❌ Количество дней должно быть от 1 до 730")
            return
        
        success = await _grant_paid_subscription(db, user_id, days, db_user.id)
        
        if success:
            await message.answer(
                f"✅ Пользователю выдана подписка на {days} дней",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
        else:
            await message.answer("❌ Ошибка выдачи подписки")
        
    except ValueError:
        await message.answer("❌ Введите корректное число дней")
        return
    
    await state.clear()

@admin_required
@error_handler
async def show_user_servers_management(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])

    if await _render_user_subscription_overview(callback, db, user_id):
        await callback.answer()


@admin_required
@error_handler
async def show_server_selection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    await _show_servers_for_user(callback, user_id, db)
    await callback.answer()

async def _show_servers_for_user(
    callback: types.CallbackQuery,
    user_id: int,
    db: AsyncSession
):
    try:
        user = await get_user_by_id(db, user_id)
        current_squads = []
        if user and user.subscription:
            current_squads = user.subscription.connected_squads or []
        
        all_servers, _ = await get_all_server_squads(db, available_only=False)
        
        servers_to_show = []
        for server in all_servers:
            if server.is_available or server.squad_uuid in current_squads:
                servers_to_show.append(server)
        
        if not servers_to_show:
            await callback.message.edit_text(
                "❌ Доступные серверы не найдены",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
            return
        
        text = f"🌍 <b>Управление серверами</b>\n\n"
        text += f"Нажмите на сервер чтобы добавить/убрать:\n"
        text += f"✅ - выбранный сервер\n"
        text += f"⚪ - доступный сервер\n"
        text += f"🔒 - неактивный (только для уже назначенных)\n\n"
        
        keyboard = []
        selected_servers = [s for s in servers_to_show if s.squad_uuid in current_squads]
        available_servers = [s for s in servers_to_show if s.squad_uuid not in current_squads and s.is_available]
        inactive_servers = [s for s in servers_to_show if s.squad_uuid not in current_squads and not s.is_available]
        
        sorted_servers = selected_servers + available_servers + inactive_servers
        
        for server in sorted_servers[:20]: 
            is_selected = server.squad_uuid in current_squads
            
            if is_selected:
                emoji = "✅"
            elif server.is_available:
                emoji = "⚪"
            else:
                emoji = "🔒"
            
            display_name = server.display_name
            if not server.is_available and not is_selected:
                display_name += " (неактивный)"
            
            keyboard.append([
                types.InlineKeyboardButton(
                    text=f"{emoji} {display_name}",
                    callback_data=f"admin_user_toggle_server_{user_id}_{server.id}"
                )
            ])
        
        if len(servers_to_show) > 20:
            text += f"\n📝 Показано первых 20 из {len(servers_to_show)} серверов"
        
        keyboard.append([
            types.InlineKeyboardButton(text="✅ Готово", callback_data=f"admin_user_subscription_{user_id}"),
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user_subscription_{user_id}")
        ])
        
        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка показа серверов: {e}")

@admin_required
@error_handler
async def toggle_user_server(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[4]) 
    server_id = int(parts[5])
    
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.subscription:
            await callback.answer("❌ Пользователь или подписка не найдены", show_alert=True)
            return
        
        server = await get_server_squad_by_id(db, server_id)
        if not server:
            await callback.answer("❌ Сервер не найден", show_alert=True)
            return
        
        subscription = user.subscription
        current_squads = list(subscription.connected_squads or [])
        
        if server.squad_uuid in current_squads:
            current_squads.remove(server.squad_uuid)
            action_text = "удален"
        else:
            current_squads.append(server.squad_uuid)
            action_text = "добавлен"
        
        subscription.connected_squads = current_squads
        subscription.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(subscription)
        
        if user.remnawave_uuid:
            try:
                remnawave_service = RemnaWaveService()
                async with remnawave_service.get_api_client() as api:
                    await api.update_user(
                        uuid=user.remnawave_uuid,
                        active_internal_squads=current_squads,
                        description=settings.format_remnawave_user_description(
                            full_name=user.full_name,
                            username=user.username,
                            telegram_id=user.telegram_id
                        )
                    )
                logger.info(f"✅ Обновлены серверы в RemnaWave для пользователя {user.telegram_id}")
            except Exception as rw_error:
                logger.error(f"❌ Ошибка обновления RemnaWave: {rw_error}")
        
        logger.info(f"Админ {db_user.id}: сервер {server.display_name} {action_text} для пользователя {user_id}")
        
        await refresh_server_selection_screen(callback, user_id, db_user, db)
        
    except Exception as e:
        logger.error(f"Ошибка переключения сервера: {e}")
        await callback.answer("❌ Ошибка изменения сервера", show_alert=True)

async def refresh_server_selection_screen(
    callback: types.CallbackQuery,
    user_id: int,
    db_user: User,
    db: AsyncSession
):
    try:
        user = await get_user_by_id(db, user_id)
        current_squads = []
        if user and user.subscription:
            current_squads = user.subscription.connected_squads or []
        
        servers, _ = await get_all_server_squads(db, available_only=True)
        
        if not servers:
            await callback.message.edit_text(
                "❌ Доступные серверы не найдены",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
            return
        
        text = f"🌍 <b>Управление серверами</b>\n\n"
        text += f"Нажмите на сервер чтобы добавить/убрать:\n\n"
        
        keyboard = []
        for server in servers[:15]:
            is_selected = server.squad_uuid in current_squads
            emoji = "✅" if is_selected else "⚪"
            
            keyboard.append([
                types.InlineKeyboardButton(
                    text=f"{emoji} {server.display_name}",
                    callback_data=f"admin_user_toggle_server_{user_id}_{server.id}"
                )
            ])
        
        if len(servers) > 15:
            text += f"\n📝 Показано первых 15 из {len(servers)} серверов"
        
        keyboard.append([
            types.InlineKeyboardButton(text="✅ Готово", callback_data=f"admin_user_subscription_{user_id}"),
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user_subscription_{user_id}")
        ])
        
        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка обновления экрана серверов: {e}")


@admin_required
@error_handler
async def start_devices_edit(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    user_id = int(callback.data.split('_')[-1])
    
    await state.update_data(editing_devices_user_id=user_id)
    
    await callback.message.edit_text(
        "📱 <b>Изменение количества устройств</b>\n\n"
        "Введите новое количество устройств (от 1 до 10):\n"
        "• Текущее значение будет заменено\n"
        "• Примеры: 1, 2, 5, 10\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="1", callback_data=f"admin_user_devices_set_{user_id}_1"),
                types.InlineKeyboardButton(text="2", callback_data=f"admin_user_devices_set_{user_id}_2"),
                types.InlineKeyboardButton(text="3", callback_data=f"admin_user_devices_set_{user_id}_3")
            ],
            [
                types.InlineKeyboardButton(text="5", callback_data=f"admin_user_devices_set_{user_id}_5"),
                types.InlineKeyboardButton(text="10", callback_data=f"admin_user_devices_set_{user_id}_10")
            ],
            [
                types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_subscription_{user_id}")
            ]
        ])
    )
    
    await state.set_state(AdminStates.editing_user_devices)
    await callback.answer()


@admin_required
@error_handler
async def set_user_devices_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    devices = int(parts[-1])
    
    success = await _update_user_devices(db, user_id, devices, db_user.id)
    
    if success:
        await callback.message.edit_text(
            f"✅ Количество устройств изменено на: {devices}",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка изменения количества устройств",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def process_devices_edit_text(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    user_id = data.get("editing_devices_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: пользователь не найден")
        await state.clear()
        return
    
    try:
        devices = int(message.text.strip())
        
        if devices <= 0 or devices > 10:
            await message.answer("❌ Количество устройств должно быть от 1 до 10")
            return
        
        success = await _update_user_devices(db, user_id, devices, db_user.id)
        
        if success:
            await message.answer(
                f"✅ Количество устройств изменено на: {devices}",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
        else:
            await message.answer("❌ Ошибка изменения количества устройств")
        
    except ValueError:
        await message.answer("❌ Введите корректное число устройств")
        return
    
    await state.clear()


@admin_required
@error_handler
async def start_traffic_edit(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    user_id = int(callback.data.split('_')[-1])
    
    await state.update_data(editing_traffic_user_id=user_id)
    
    await callback.message.edit_text(
        "📊 <b>Изменение лимита трафика</b>\n\n"
        "Введите новый лимит трафика в ГБ:\n"
        "• 0 - безлимитный трафик\n"
        "• Примеры: 50, 100, 500, 1000\n"
        "• Максимум: 10000 ГБ\n\n"
        "Или нажмите /cancel для отмены",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="50 ГБ", callback_data=f"admin_user_traffic_set_{user_id}_50"),
                types.InlineKeyboardButton(text="100 ГБ", callback_data=f"admin_user_traffic_set_{user_id}_100")
            ],
            [
                types.InlineKeyboardButton(text="500 ГБ", callback_data=f"admin_user_traffic_set_{user_id}_500"),
                types.InlineKeyboardButton(text="1000 ГБ", callback_data=f"admin_user_traffic_set_{user_id}_1000")
            ],
            [
                types.InlineKeyboardButton(text="♾️ Безлимит", callback_data=f"admin_user_traffic_set_{user_id}_0")
            ],
            [
                types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_subscription_{user_id}")
            ]
        ])
    )
    
    await state.set_state(AdminStates.editing_user_traffic)
    await callback.answer()


@admin_required
@error_handler
async def set_user_traffic_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    traffic_gb = int(parts[-1])
    
    success = await _update_user_traffic(db, user_id, traffic_gb, db_user.id)
    
    if success:
        traffic_text = "♾️ безлимитный" if traffic_gb == 0 else f"{traffic_gb} ГБ"
        await callback.message.edit_text(
            f"✅ Лимит трафика изменен на: {traffic_text}",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка изменения лимита трафика",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


@admin_required
@error_handler
async def process_traffic_edit_text(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    user_id = data.get("editing_traffic_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: пользователь не найден")
        await state.clear()
        return
    
    try:
        traffic_gb = int(message.text.strip())
        
        if traffic_gb < 0 or traffic_gb > 10000:
            await message.answer("❌ Лимит трафика должен быть от 0 до 10000 ГБ (0 = безлимит)")
            return
        
        success = await _update_user_traffic(db, user_id, traffic_gb, db_user.id)
        
        if success:
            traffic_text = "♾️ безлимитный" if traffic_gb == 0 else f"{traffic_gb} ГБ"
            await message.answer(
                f"✅ Лимит трафика изменен на: {traffic_text}",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
        else:
            await message.answer("❌ Ошибка изменения лимита трафика")
        
    except ValueError:
        await message.answer("❌ Введите корректное число ГБ")
        return
    
    await state.clear()


@admin_required
@error_handler
async def confirm_reset_devices(
    callback: types.CallbackQuery,
    db_user: User
):
    user_id = int(callback.data.split('_')[-1])
    
    await callback.message.edit_text(
        "🔄 <b>Сброс устройств пользователя</b>\n\n"
        "⚠️ <b>ВНИМАНИЕ!</b>\n"
        "Вы уверены, что хотите сбросить все HWID устройства этого пользователя?\n\n"
        "Это действие:\n"
        "• Удалит все привязанные устройства\n"
        "• Пользователь сможет заново подключить устройства\n"
        "• Действие необратимо!\n\n"
        "Продолжить?",
        reply_markup=get_confirmation_keyboard(
            f"admin_user_reset_devices_confirm_{user_id}",
            f"admin_user_subscription_{user_id}",
            db_user.language
        )
    )
    await callback.answer()


@admin_required
@error_handler
async def reset_user_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.remnawave_uuid:
            await callback.answer("❌ Пользователь не найден или не связан с RemnaWave", show_alert=True)
            return
        
        remnawave_service = RemnaWaveService()
        async with remnawave_service.get_api_client() as api:
            success = await api.reset_user_devices(user.remnawave_uuid)
        
        if success:
            if user.subscription is not None:
                try:
                    await revoke_all_device_links(db, user.subscription.id)
                except Exception as revoke_error:
                    logger.error(f"Ошибка отзыва привязок пользователя {user_id}: {revoke_error}")
            await callback.message.edit_text(
                "✅ Устройства пользователя успешно сброшены",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
            logger.info(f"Админ {db_user.id} сбросил устройства пользователя {user_id}")
        else:
            await callback.message.edit_text(
                "❌ Ошибка сброса устройств",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📱 Подписка и настройки", callback_data=f"admin_user_subscription_{user_id}")]
                ])
            )
        
    except Exception as e:
        logger.error(f"Ошибка сброса устройств: {e}")
        await callback.answer("❌ Ошибка сброса устройств", show_alert=True)

async def _update_user_devices(db: AsyncSession, user_id: int, devices: int, admin_id: int) -> bool:
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.subscription:
            logger.error(f"Пользователь {user_id} или подписка не найдены")
            return False
        
        subscription = user.subscription
        old_devices = subscription.device_limit
        subscription.device_limit = devices
        subscription.updated_at = datetime.utcnow()
        
        await db.commit()
        
        if user.remnawave_uuid:
            try:
                remnawave_service = RemnaWaveService()
                async with remnawave_service.get_api_client() as api:
                    await api.update_user(
                        uuid=user.remnawave_uuid,
                        hwid_device_limit=devices,
                        description=settings.format_remnawave_user_description(
                            full_name=user.full_name,
                            username=user.username,
                            telegram_id=user.telegram_id
                        )
                    )
                logger.info(f"✅ Обновлен лимит устройств в RemnaWave для пользователя {user.telegram_id}")
            except Exception as rw_error:
                logger.error(f"❌ Ошибка обновления лимита устройств в RemnaWave: {rw_error}")
        
        logger.info(f"Админ {admin_id} изменил лимит устройств пользователя {user_id}: {old_devices} -> {devices}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка обновления лимита устройств: {e}")
        await db.rollback()
        return False


async def _update_user_traffic(db: AsyncSession, user_id: int, traffic_gb: int, admin_id: int) -> bool:
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.subscription:
            logger.error(f"Пользователь {user_id} или подписка не найдены")
            return False
        
        subscription = user.subscription
        old_traffic = subscription.traffic_limit_gb
        subscription.traffic_limit_gb = traffic_gb
        subscription.updated_at = datetime.utcnow()
        
        await db.commit()
        
        if user.remnawave_uuid:
            try:
                from app.external.remnawave_api import TrafficLimitStrategy
                
                remnawave_service = RemnaWaveService()
                async with remnawave_service.get_api_client() as api:
                    await api.update_user(
                        uuid=user.remnawave_uuid,
                        traffic_limit_bytes=traffic_gb * (1024**3) if traffic_gb > 0 else 0,
                        traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                        description=settings.format_remnawave_user_description(
                            full_name=user.full_name,
                            username=user.username,
                            telegram_id=user.telegram_id
                        )
                    )
                logger.info(f"✅ Обновлен лимит трафика в RemnaWave для пользователя {user.telegram_id}")
            except Exception as rw_error:
                logger.error(f"❌ Ошибка обновления лимита трафика в RemnaWave: {rw_error}")
        
        traffic_text_old = "безлимитный" if old_traffic == 0 else f"{old_traffic} ГБ"
        traffic_text_new = "безлимитный" if traffic_gb == 0 else f"{traffic_gb} ГБ"
        logger.info(f"Админ {admin_id} изменил лимит трафика пользователя {user_id}: {traffic_text_old} -> {traffic_text_new}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка обновления лимита трафика: {e}")
        await db.rollback()
        return False


async def _extend_subscription_by_days(db: AsyncSession, user_id: int, days: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id, extend_subscription
        from app.services.subscription_service import SubscriptionService
        
        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False

        old_end_date = subscription.end_date
        await extend_subscription(db, subscription, days)
        if days > 0:
            await record_subscription_renewal_event(
                db,
                user_id=user_id,
                subscription_id=subscription.id,
                amount_kopeks=0,
                period_days=days,
                previous_end_date=old_end_date,
                new_end_date=subscription.end_date,
                source="admin_manual",
            )

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
        
        if days > 0:
            logger.info(f"Админ {admin_id} продлил подписку пользователя {user_id} на {days} дней")
        else:
            logger.info(f"Админ {admin_id} сократил подписку пользователя {user_id} на {abs(days)} дней")
        return True

    except Exception as e:
        logger.error(f"Ошибка продления подписки: {e}")
        return False


async def _add_subscription_traffic(db: AsyncSession, user_id: int, gb: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id, add_subscription_traffic
        from app.services.subscription_service import SubscriptionService
        
        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False
        
        if gb == 0:  
            subscription.traffic_limit_gb = 0
            await db.commit()
        else:
            await add_subscription_traffic(db, subscription, gb)
        
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
        
        traffic_text = "безлимитный" if gb == 0 else f"{gb} ГБ"
        logger.info(f"Админ {admin_id} добавил трафик {traffic_text} пользователю {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка добавления трафика: {e}")
        return False


async def _deactivate_user_subscription(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id, deactivate_subscription
        from app.services.subscription_service import SubscriptionService
        
        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False
        
        await deactivate_subscription(db, subscription)
        
        user = await get_user_by_id(db, user_id)
        if user and user.remnawave_uuid:
            subscription_service = SubscriptionService()
            await subscription_service.disable_remnawave_user(user.remnawave_uuid)
        
        logger.info(f"Админ {admin_id} деактивировал подписку пользователя {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка деактивации подписки: {e}")
        return False


async def _activate_user_subscription(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService
        from app.database.models import SubscriptionStatus
        from datetime import datetime
        
        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False
        
        subscription.status = SubscriptionStatus.ACTIVE.value
        if subscription.end_date <= datetime.utcnow():
            subscription.end_date = datetime.utcnow() + timedelta(days=1)
        
        await db.commit()
        await db.refresh(subscription)
        
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
        
        logger.info(f"Админ {admin_id} активировал подписку пользователя {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка активации подписки: {e}")
        return False


async def _grant_trial_subscription(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id, create_trial_subscription
        from app.services.subscription_service import SubscriptionService
        
        existing_subscription = await get_subscription_by_user_id(db, user_id)
        if existing_subscription:
            logger.error(f"У пользователя {user_id} уже есть подписка")
            return False
        
        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        subscription = await create_trial_subscription(
            db,
            user_id,
            device_limit=forced_devices,
        )
        
        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(db, subscription)
        
        logger.info(f"Админ {admin_id} выдал триальную подписку пользователю {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка выдачи триальной подписки: {e}")
        return False


async def _grant_paid_subscription(db: AsyncSession, user_id: int, days: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id, create_paid_subscription
        from app.services.subscription_service import SubscriptionService
        from app.config import settings
        
        existing_subscription = await get_subscription_by_user_id(db, user_id)
        if existing_subscription:
            logger.error(f"У пользователя {user_id} уже есть подписка")
            return False
        
        trial_squads: list[str] = []

        try:
            from app.database.crud.server_squad import get_random_trial_squad_uuid

            trial_uuid = await get_random_trial_squad_uuid(db)
            if trial_uuid:
                trial_squads = [trial_uuid]
        except Exception as error:
            logger.error(
                "Не удалось подобрать сквад при выдаче подписки админом %s: %s",
                admin_id,
                error,
            )

        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        device_limit = settings.DEFAULT_DEVICE_LIMIT
        if forced_devices is not None:
            device_limit = forced_devices

        subscription = await create_paid_subscription(
            db=db,
            user_id=user_id,
            duration_days=days,
            traffic_limit_gb=settings.DEFAULT_TRAFFIC_LIMIT_GB,
            device_limit=device_limit,
            connected_squads=trial_squads,
            update_server_counters=True,
        )
        await record_subscription_purchase_event(
            db,
            user_id=user_id,
            subscription_id=subscription.id,
            amount_kopeks=0,
            period_days=days,
            was_trial_conversion=False,
            source="admin_manual",
            starts_at=subscription.start_date,
            ends_at=subscription.end_date,
        )

        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(db, subscription)
        
        logger.info(f"Админ {admin_id} выдал платную подписку на {days} дней пользователю {user_id}")
        return True

    except Exception as e:
        logger.error(f"Ошибка выдачи платной подписки: {e}")
        return False


async def _admin_assign_tariff(
    db: AsyncSession,
    user_id: int,
    plan_id: int,
    period_days: int,
    admin_id: int,
) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id

        plan = await get_plan_by_id(db, plan_id)
        if not plan or not plan.is_active:
            logger.error(f"Тариф {plan_id} не найден или неактивен")
            return False

        squads = [s.squad_uuid for s in await get_active_server_squads(db) if s.squad_uuid]
        now = datetime.utcnow()
        end_date = now + timedelta(days=period_days)

        subscription = await get_subscription_by_user_id(db, user_id)
        was_trial_conversion = bool(subscription and subscription.is_trial)

        if subscription is not None:
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.is_trial = False
            subscription.start_date = now
            subscription.end_date = end_date
            subscription.traffic_limit_gb = plan.traffic_limit_gb
            subscription.device_limit = plan.device_limit
            subscription.connected_squads = squads
            subscription.plan_id = plan.id
            subscription.plan_period_days = period_days
        else:
            subscription = Subscription(
                user_id=user_id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now,
                end_date=end_date,
                traffic_limit_gb=plan.traffic_limit_gb,
                device_limit=plan.device_limit,
                connected_squads=squads,
                autopay_enabled=False,
                autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
                plan_id=plan.id,
                plan_period_days=period_days,
            )
            db.add(subscription)

        await db.commit()
        await db.refresh(subscription)

        await record_subscription_purchase_event(
            db,
            user_id=user_id,
            subscription_id=subscription.id,
            amount_kopeks=0,
            period_days=period_days,
            was_trial_conversion=was_trial_conversion,
            source="admin_tariff",
            starts_at=subscription.start_date,
            ends_at=subscription.end_date,
            extra={"plan_id": plan.id, "plan_code": plan.code},
        )

        try:
            remnawave_user = await SubscriptionService().create_remnawave_user(db, subscription)
            if remnawave_user is None:
                logger.error(f"Не удалось синхронизировать подписку {subscription.id} с панелью (см. ошибки выше)")
        except Exception as e:
            logger.warning(f"Не удалось синхронизировать подписку {subscription.id} с панелью: {e}")

        logger.info(
            f"Админ {admin_id} назначил тариф {plan.code} на {period_days} дней пользователю {user_id}"
        )
        return True

    except Exception as e:
        logger.error(f"Ошибка назначения тарифа: {e}")
        await db.rollback()
        return False


async def _admin_renew_tariff(db: AsyncSession, user_id: int, period_days: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id, extend_subscription

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False
        if subscription.plan_id is None:
            logger.error(f"У пользователя {user_id} нет тарифа — продление по тарифу невозможно")
            return False

        old_end_date = subscription.end_date
        await extend_subscription(db, subscription, period_days)
        subscription.plan_period_days = period_days
        await db.commit()

        await record_subscription_renewal_event(
            db,
            user_id=user_id,
            subscription_id=subscription.id,
            amount_kopeks=0,
            period_days=period_days,
            previous_end_date=old_end_date,
            new_end_date=subscription.end_date,
            source="admin_tariff",
        )

        await SubscriptionService().update_remnawave_user(db, subscription)

        logger.info(f"Админ {admin_id} продлил тариф пользователя {user_id} на {period_days} дней")
        return True

    except Exception as e:
        logger.error(f"Ошибка продления тарифа: {e}")
        return False


async def _admin_remove_tariff(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False
        if subscription.plan_id is None:
            logger.error(f"У пользователя {user_id} и так нет тарифа")
            return False

        plan_code = subscription.plan.code if subscription.plan else subscription.plan_id
        subscription.plan_id = None
        subscription.plan_period_days = None
        await db.commit()

        logger.info(f"Админ {admin_id} снял тариф {plan_code} с пользователя {user_id} (подписка стала legacy)")
        return True

    except Exception as e:
        logger.error(f"Ошибка снятия тарифа: {e}")
        await db.rollback()
        return False


async def _calculate_subscription_period_price(
    db: AsyncSession,
    target_user: User,
    subscription: Subscription,
    period_days: int,
    subscription_service: Optional[SubscriptionService] = None,
) -> int:
    """Рассчитывает стоимость подписки для администратора с учётом всех параметров."""

    service = subscription_service or SubscriptionService()

    connected_squads = list(subscription.connected_squads or [])
    server_ids = []

    if connected_squads:
        try:
            server_ids = await get_server_ids_by_uuids(db, connected_squads)
            if len(server_ids) != len(connected_squads):
                logger.warning(
                    "Не удалось сопоставить все сервера подписки пользователя %s для расчёта цены",
                    target_user.telegram_id,
                )
        except Exception as e:
            logger.error(
                "Не удалось получить идентификаторы серверов для расчёта цены подписки пользователя %s: %s",
                target_user.telegram_id,
                e,
            )
            server_ids = []
    traffic_limit_gb = subscription.traffic_limit_gb
    if traffic_limit_gb is None:
        traffic_limit_gb = settings.DEFAULT_TRAFFIC_LIMIT_GB

    device_limit = subscription.device_limit
    if not device_limit or device_limit < 0:
        device_limit = settings.DEFAULT_DEVICE_LIMIT

    total_price, _ = await service.calculate_subscription_price(
        period_days=period_days,
        traffic_gb=traffic_limit_gb,
        server_squad_ids=server_ids,
        devices=device_limit,
        db=db,
        user=target_user,
        promo_group=target_user.promo_group,
    )

    return total_price

@admin_required
@error_handler
async def cleanup_inactive_users(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    
    user_service = UserService()
    deleted_count = await user_service.cleanup_inactive_users(db)
    
    await callback.message.edit_text(
        f"✅ Очистка завершена\n\n"
        f"Удалено неактивных пользователей: {deleted_count}",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")]
        ])
    )
    await callback.answer()

@admin_required
@error_handler
async def change_subscription_type(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    
    if not profile or not profile["subscription"]:
        await callback.answer("❌ Пользователь или подписка не найдены", show_alert=True)
        return
    
    subscription = profile["subscription"]
    current_type = "🎁 Триал" if subscription.is_trial else "💎 Платная"
    
    text = f"🔄 <b>Смена типа подписки</b>\n\n"
    text += f"👤 {profile['user'].full_name}\n"
    text += f"📱 Текущий тип: {current_type}\n\n"
    text += f"Выберите новый тип подписки:"
    
    keyboard = []
    
    if subscription.is_trial:
        keyboard.append([
            InlineKeyboardButton(
                text="💎 Сделать платной", 
                callback_data=f"admin_sub_type_paid_{user_id}"
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                text="🎁 Сделать триальной", 
                callback_data=f"admin_sub_type_trial_{user_id}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(
            text="⬅️ Назад", 
            callback_data=f"admin_user_subscription_{user_id}"
        )
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@admin_required
@error_handler
async def admin_buy_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])
    
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    
    if not profile:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    target_user = profile["user"]
    subscription = profile["subscription"]
    
    if not subscription:
        await callback.answer("❌ У пользователя нет подписки", show_alert=True)
        return
    
    available_periods = settings.get_available_subscription_periods()

    subscription_service = SubscriptionService()
    period_buttons = []

    for period in available_periods:
        try:
            price_kopeks = await _calculate_subscription_period_price(
                db,
                target_user,
                subscription,
                period,
                subscription_service=subscription_service,
            )
        except Exception as e:
            logger.error(
                "Ошибка расчёта стоимости подписки для пользователя %s и периода %s дней: %s",
                target_user.telegram_id,
                period,
                e,
            )
            continue

        period_buttons.append([
            types.InlineKeyboardButton(
                text=f"{period} дней ({settings.format_price(price_kopeks)})",
                callback_data=f"admin_buy_sub_confirm_{user_id}_{period}_{price_kopeks}"
            )
        ])

    if not period_buttons:
        await callback.answer("❌ Не удалось рассчитать стоимость подписки", show_alert=True)
        return

    period_buttons.append([
        types.InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"admin_user_subscription_{user_id}"
        )
    ])

    text = f"💳 <b>Покупка подписки для пользователя</b>\n\n"
    target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
    text += f"👤 {target_user_link} (ID: {target_user.telegram_id})\n"
    text += f"💰 Баланс пользователя: {settings.format_price(target_user.balance_kopeks)}\n\n"
    traffic_text = "Безлимит" if (subscription.traffic_limit_gb or 0) <= 0 else f"{subscription.traffic_limit_gb} ГБ"
    devices_limit = subscription.device_limit
    if devices_limit is None:
        devices_limit = settings.DEFAULT_DEVICE_LIMIT
    servers_count = len(subscription.connected_squads or [])
    text += f"📶 Трафик: {traffic_text}\n"
    text += f"📱 Устройства: {devices_limit}\n"
    text += f"🌐 Серверов: {servers_count}\n\n"
    text += "Выберите период подписки:\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=period_buttons)
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[4])
    period_days = int(parts[5])
    price_kopeks_from_callback = int(parts[6]) if len(parts) > 6 else None
    
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    
    if not profile:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    target_user = profile["user"]
    subscription = profile["subscription"]

    if not subscription:
        await callback.answer("❌ У пользователя нет подписки", show_alert=True)
        return

    subscription_service = SubscriptionService()

    try:
        price_kopeks = await _calculate_subscription_period_price(
            db,
            target_user,
            subscription,
            period_days,
            subscription_service=subscription_service,
        )
    except Exception as e:
        logger.error(
            "Ошибка расчёта стоимости подписки при подтверждении админом для пользователя %s: %s",
            target_user.telegram_id,
            e,
        )
        await callback.answer("❌ Не удалось рассчитать стоимость подписки", show_alert=True)
        return

    if price_kopeks_from_callback is not None and price_kopeks_from_callback != price_kopeks:
        logger.info(
            "Стоимость подписки для пользователя %s изменилась с %s до %s при подтверждении",
            target_user.telegram_id,
            price_kopeks_from_callback,
            price_kopeks,
        )

    if target_user.balance_kopeks < price_kopeks:
        missing_kopeks = price_kopeks - target_user.balance_kopeks
        await callback.message.edit_text(
            f"❌ Недостаточно средств на балансе пользователя\n\n"
            f"💰 Баланс пользователя: {settings.format_price(target_user.balance_kopeks)}\n"
            f"💳 Стоимость подписки: {settings.format_price(price_kopeks)}\n"
            f"📉 Не хватает: {settings.format_price(missing_kopeks)}\n\n"
            f"Пополните баланс пользователя перед покупкой.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="⬅️ Назад к подписке",
                    callback_data=f"admin_user_subscription_{user_id}"
                )]
            ])
        )
        await callback.answer()
        return
    
    text = f"💳 <b>Подтверждение покупки подписки</b>\n\n"
    target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
    text += f"👤 {target_user_link} (ID: {target_user.telegram_id})\n"
    text += f"📅 Период подписки: {period_days} дней\n"
    text += f"💰 Стоимость: {settings.format_price(price_kopeks)}\n"
    text += f"💰 Баланс пользователя: {settings.format_price(target_user.balance_kopeks)}\n\n"
    traffic_text = "Безлимит" if (subscription.traffic_limit_gb or 0) <= 0 else f"{subscription.traffic_limit_gb} ГБ"
    devices_limit = subscription.device_limit
    if devices_limit is None:
        devices_limit = settings.DEFAULT_DEVICE_LIMIT
    servers_count = len(subscription.connected_squads or [])
    text += f"📶 Трафик: {traffic_text}\n"
    text += f"📱 Устройства: {devices_limit}\n"
    text += f"🌐 Серверов: {servers_count}\n\n"
    text += "Вы уверены, что хотите купить подписку для этого пользователя?"
    
    keyboard = [
        [
            types.InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"admin_buy_sub_execute_{user_id}_{period_days}_{price_kopeks}"
            )
        ],
        [
            types.InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=f"admin_sub_buy_{user_id}"
            )
        ]
    ]
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription_execute(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[4])
    period_days = int(parts[5])
    price_kopeks_from_callback = int(parts[6]) if len(parts) > 6 else None
    
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    
    if not profile:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    target_user = profile["user"]
    subscription = profile["subscription"]

    if not subscription:
        await callback.answer("❌ У пользователя нет подписки", show_alert=True)
        return

    subscription_service = SubscriptionService()

    try:
        price_kopeks = await _calculate_subscription_period_price(
            db,
            target_user,
            subscription,
            period_days,
            subscription_service=subscription_service,
        )
    except Exception as e:
        logger.error(
            "Ошибка расчёта стоимости подписки при списании средств админом для пользователя %s: %s",
            target_user.telegram_id,
            e,
        )
        await callback.answer("❌ Не удалось рассчитать стоимость подписки", show_alert=True)
        return

    if price_kopeks_from_callback is not None and price_kopeks_from_callback != price_kopeks:
        logger.info(
            "Стоимость подписки для пользователя %s изменилась с %s до %s перед списанием",
            target_user.telegram_id,
            price_kopeks_from_callback,
            price_kopeks,
        )

    if target_user.balance_kopeks < price_kopeks:
        await callback.answer("❌ Недостаточно средств на балансе пользователя", show_alert=True)
        return
    
    try:
        from app.database.crud.user import subtract_user_balance
        success = await subtract_user_balance(
            db, target_user, price_kopeks,
            f"Покупка подписки на {period_days} дней (администратор)"
        )
        
        if not success:
            await callback.answer("❌ Ошибка списания средств", show_alert=True)
            return
        
        if subscription:
            current_time = datetime.utcnow()
            bonus_period = timedelta()

            if (
                subscription.is_trial
                and settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID
                and subscription.end_date
            ):
                remaining_trial_delta = subscription.end_date - current_time
                if remaining_trial_delta.total_seconds() > 0:
                    bonus_period = remaining_trial_delta
                    logger.info(
                        "Админ продлевает подписку: добавляем оставшееся время триала (%s) пользователю %s",
                        bonus_period,
                        target_user.telegram_id,
                    )

            extension_base_date = current_time
            if subscription.end_date and subscription.end_date > current_time:
                extension_base_date = subscription.end_date
            else:
                subscription.start_date = current_time

            subscription.end_date = extension_base_date + timedelta(days=period_days) + bonus_period
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.updated_at = current_time

            if subscription.is_trial or not subscription.is_active:
                subscription.is_trial = False
                if subscription.traffic_limit_gb != 0: 
                    subscription.traffic_limit_gb = 0
                subscription.device_limit = settings.DEFAULT_DEVICE_LIMIT
                if subscription.is_trial:
                    subscription.traffic_used_gb = 0.0
            
            await db.commit()
            await db.refresh(subscription)
            
            from app.database.crud.transaction import create_transaction
            transaction = await create_transaction(
                db=db,
                user_id=target_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_kopeks,
                description=f"Продление подписки на {period_days} дней (администратор)"
            )
            
            try:
                from app.services.remnawave_service import RemnaWaveService
                from app.external.remnawave_api import UserStatus, TrafficLimitStrategy
                remnawave_service = RemnaWaveService()
                
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                if target_user.remnawave_uuid:
                    async with remnawave_service.get_api_client() as api:
                        update_kwargs = dict(
                            uuid=target_user.remnawave_uuid,
                            status=UserStatus.ACTIVE if subscription.is_active else UserStatus.EXPIRED,
                            expire_at=subscription.end_date,
                            traffic_limit_bytes=subscription.traffic_limit_gb * (1024**3) if subscription.traffic_limit_gb > 0 else 0,
                            traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                            description=settings.format_remnawave_user_description(
                                full_name=target_user.full_name,
                                username=target_user.username,
                                telegram_id=target_user.telegram_id
                            ),
                            active_internal_squads=subscription.connected_squads,
                        )

                        if hwid_limit is not None:
                            update_kwargs['hwid_device_limit'] = hwid_limit

                        remnawave_user = await api.update_user(**update_kwargs)
                else:
                    username = settings.format_remnawave_username(
                        full_name=target_user.full_name,
                        username=target_user.username,
                        telegram_id=target_user.telegram_id,
                    )
                    async with remnawave_service.get_api_client() as api:
                        create_kwargs = dict(
                            username=username,
                            expire_at=subscription.end_date,
                            status=UserStatus.ACTIVE if subscription.is_active else UserStatus.EXPIRED,
                            traffic_limit_bytes=subscription.traffic_limit_gb * (1024**3) if subscription.traffic_limit_gb > 0 else 0,
                            traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                            telegram_id=target_user.telegram_id,
                            description=settings.format_remnawave_user_description(
                                full_name=target_user.full_name,
                                username=target_user.username,
                                telegram_id=target_user.telegram_id
                            ),
                            active_internal_squads=subscription.connected_squads,
                        )

                        if hwid_limit is not None:
                            create_kwargs['hwid_device_limit'] = hwid_limit

                        remnawave_user = await api.create_user(**create_kwargs)
                    
                    if remnawave_user and hasattr(remnawave_user, 'uuid'):
                        target_user.remnawave_uuid = remnawave_user.uuid
                        await db.commit()
                
                if remnawave_user:
                    logger.info(f"Пользователь {target_user.telegram_id} успешно обновлен в RemnaWave")
                else:
                    logger.error(f"Ошибка обновления пользователя {target_user.telegram_id} в RemnaWave")
            except Exception as e:
                logger.error(f"Ошибка работы с RemnaWave для пользователя {target_user.telegram_id}: {e}")
            
            message = f"✅ Подписка пользователя продлена на {period_days} дней"
        else:
            message = "❌ Ошибка: у пользователя нет существующей подписки"
        
        target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
        await callback.message.edit_text(
            f"{message}\n\n"
            f"👤 {target_user_link} (ID: {target_user.telegram_id})\n"
            f"💰 Списано: {settings.format_price(price_kopeks)}\n"
            f"📅 Подписка действительна до: {format_datetime(subscription.end_date)}",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="⬅️ Назад к подписке",
                    callback_data=f"admin_user_subscription_{user_id}"
                )]
            ]),
            parse_mode="HTML"
        )
        
        try:
            if callback.bot:
                await callback.bot.send_message(
                    chat_id=target_user.telegram_id,
                    text=f"💳 <b>Администратор продлил вашу подписку</b>\n\n"
                         f"📅 Подписка продлена на {period_days} дней\n"
                         f"💰 Списано с баланса: {settings.format_price(price_kopeks)}\n"
                         f"📅 Подписка действительна до: {format_datetime(subscription.end_date)}",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления пользователю {target_user.telegram_id}: {e}")
        
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка покупки подписки администратором: {e}")
        await callback.answer("❌ Ошибка при покупке подписки", show_alert=True)
        
        await db.rollback()


@admin_required
@error_handler
async def change_subscription_type_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    new_type = parts[-2]  # 'paid' или 'trial'
    user_id = int(parts[-1])
    
    success = await _change_subscription_type(db, user_id, new_type, db_user.id)
    
    if success:
        type_text = "платной" if new_type == "paid" else "триальной"
        await callback.message.edit_text(
            f"✅ Тип подписки успешно изменен на {type_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка изменения типа подписки",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
            ])
        )
    
    await callback.answer()


async def _change_subscription_type(db: AsyncSession, user_id: int, new_type: str, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService
        
        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f"Подписка не найдена для пользователя {user_id}")
            return False
        
        new_is_trial = (new_type == "trial")
        
        if subscription.is_trial == new_is_trial:
            logger.info(f"Тип подписки уже установлен корректно для пользователя {user_id}")
            return True
        
        old_type = "триальной" if subscription.is_trial else "платной"
        new_type_text = "триальной" if new_is_trial else "платной"
        
        subscription.is_trial = new_is_trial
        subscription.updated_at = datetime.utcnow()
        
        if not new_is_trial and subscription.is_trial:
            user = await get_user_by_id(db, user_id)
            if user:
                user.has_had_paid_subscription = True
        
        await db.commit()
        
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
        
        logger.info(f"Админ {admin_id} изменил тип подписки пользователя {user_id}: {old_type} -> {new_type_text}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка изменения типа подписки: {e}")
        await db.rollback()
        return False


@admin_required
@error_handler
async def show_user_tariff_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    from app.database.crud.subscription import get_subscription_by_user_id

    user_id = int(callback.data.split('_')[-1])
    subscription = await get_subscription_by_user_id(db, user_id)

    text = "🧾 <b>Тариф пользователя</b>\n\n"
    keyboard = [
        [types.InlineKeyboardButton(
            text="📋 Назначить / сменить тариф",
            callback_data=f"admin_sub_tariff_assign_{user_id}"
        )]
    ]

    if subscription and subscription.plan_id and subscription.plan:
        plan = subscription.plan
        traffic_text = "♾️" if plan.traffic_limit_gb == 0 else f"{plan.traffic_limit_gb} ГБ"
        text += f"<b>Тариф:</b> {plan.display_name}\n"
        text += f"<b>Период:</b> {_tariff_period_label(subscription.plan_period_days)}\n"
        text += f"<b>Трафик по тарифу:</b> {traffic_text}\n"
        text += f"<b>Устройства по тарифу:</b> {plan.device_limit}\n"
        text += f"<b>Окончание подписки:</b> {format_datetime(subscription.end_date)}\n"
        keyboard.append([types.InlineKeyboardButton(
            text="⏰ Продлить по тарифу",
            callback_data=f"admin_sub_tariff_renew_{user_id}"
        )])
        keyboard.append([types.InlineKeyboardButton(
            text="🗑 Убрать тариф (в legacy)",
            callback_data=f"admin_sub_tariff_remove_{user_id}"
        )])
    else:
        text += "Тариф не назначен (legacy / ручные параметры)."

    keyboard.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"admin_user_subscription_{user_id}"
    )])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_tariff_assign_plans(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    from app.database.crud.subscription import get_subscription_by_user_id

    user_id = int(callback.data.split('_')[-1])

    plans = await list_active_plans(db)
    if not plans:
        await callback.answer("❌ Нет активных тарифов", show_alert=True)
        return

    subscription = await get_subscription_by_user_id(db, user_id)
    current_plan_id = subscription.plan_id if subscription else None

    keyboard = []
    for plan in plans:
        traffic_text = "♾️" if plan.traffic_limit_gb == 0 else f"{plan.traffic_limit_gb} ГБ"
        marker = "✅ " if plan.id == current_plan_id else ""
        keyboard.append([types.InlineKeyboardButton(
            text=f"{marker}{plan.display_name} · {traffic_text} · {plan.device_limit} устр.",
            callback_data=f"admin_sub_tariff_plan_{user_id}_{plan.id}"
        )])

    keyboard.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"admin_sub_tariff_menu_{user_id}"
    )])

    await callback.message.edit_text(
        "🧾 <b>Назначение тарифа</b>\n\nВыберите тариф:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def show_tariff_assign_periods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    plan_id = int(parts[-1])

    plan = await get_plan_by_id(db, plan_id)
    if not plan or not plan.is_active:
        await callback.answer("❌ Тариф не найден или неактивен", show_alert=True)
        return

    target_user = await get_user_by_id(db, user_id)
    cohort = resolve_pricing_cohort(target_user)

    keyboard = []
    for days in SUPPORTED_PERIOD_DAYS:
        price = await get_plan_price(db, plan_id, days, cohort=cohort)
        price_text = settings.format_price(price) if price is not None else "цена не задана"
        keyboard.append([types.InlineKeyboardButton(
            text=f"{_tariff_period_label(days)} — {price_text}",
            callback_data=f"admin_sub_tariff_apply_{user_id}_{plan_id}_{days}"
        )])

    keyboard.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"admin_sub_tariff_assign_{user_id}"
    )])

    await callback.message.edit_text(
        f"🧾 <b>Тариф {plan.display_name}</b>\n\n"
        "Выберите период. Цена указана справочно — назначение бесплатное, баланс не списывается.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_tariff_assign(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    from app.database.crud.subscription import get_subscription_by_user_id

    parts = callback.data.split('_')
    user_id = int(parts[-3])
    plan_id = int(parts[-2])
    days = int(parts[-1])

    plan = await get_plan_by_id(db, plan_id)
    if not plan or not plan.is_active:
        await callback.answer("❌ Тариф не найден или неактивен", show_alert=True)
        return

    subscription = await get_subscription_by_user_id(db, user_id)
    traffic_text = "♾️" if plan.traffic_limit_gb == 0 else f"{plan.traffic_limit_gb} ГБ"
    end_date = datetime.utcnow() + timedelta(days=days)

    text = (
        "🧾 <b>Подтверждение назначения тарифа</b>\n\n"
        f"<b>Тариф:</b> {plan.display_name}\n"
        f"<b>Период:</b> {_tariff_period_label(days)}\n"
        f"<b>Трафик:</b> {traffic_text}\n"
        f"<b>Устройства:</b> {plan.device_limit}\n\n"
        "⚠️ Подписка будет перезапущена: начало — сейчас, "
        f"окончание — {format_datetime(end_date)}.\n"
        "⚠️ Текущие лимиты и серверы будут заменены значениями тарифа.\n"
    )
    if subscription and subscription.is_trial:
        text += "⚠️ Триальная подписка будет конвертирована в платную.\n"
    text += "\n💰 Бесплатно — баланс пользователя не списывается."

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"admin_sub_tariff_apply_confirm_{user_id}_{plan_id}_{days}"
            )],
            [types.InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=f"admin_sub_tariff_menu_{user_id}"
            )]
        ])
    )
    await callback.answer()


@admin_required
@error_handler
async def execute_tariff_assign(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-3])
    plan_id = int(parts[-2])
    days = int(parts[-1])

    if days not in SUPPORTED_PERIOD_DAYS:
        await callback.answer("❌ Недопустимый период", show_alert=True)
        return

    plan = await get_plan_by_id(db, plan_id)
    success = await _admin_assign_tariff(db, user_id, plan_id, days, db_user.id)

    back_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
    ])

    if success:
        plan_name = plan.display_name if plan else str(plan_id)
        await callback.message.edit_text(
            f"✅ Тариф {plan_name} назначен на {_tariff_period_label(days)}",
            reply_markup=back_keyboard
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка назначения тарифа",
            reply_markup=back_keyboard
        )
    await callback.answer()


@admin_required
@error_handler
async def show_tariff_renew_periods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    from app.database.crud.subscription import get_subscription_by_user_id

    user_id = int(callback.data.split('_')[-1])

    subscription = await get_subscription_by_user_id(db, user_id)
    if not subscription or subscription.plan_id is None:
        await callback.answer("❌ У пользователя нет тарифа", show_alert=True)
        return

    keyboard = []
    current_period = subscription.plan_period_days
    if current_period in SUPPORTED_PERIOD_DAYS:
        keyboard.append([types.InlineKeyboardButton(
            text=f"По тарифу: {_tariff_period_label(current_period)}",
            callback_data=f"admin_sub_tariff_renew_go_{user_id}_{current_period}"
        )])
    for days in SUPPORTED_PERIOD_DAYS:
        if days == current_period:
            continue
        keyboard.append([types.InlineKeyboardButton(
            text=_tariff_period_label(days),
            callback_data=f"admin_sub_tariff_renew_go_{user_id}_{days}"
        )])

    keyboard.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"admin_sub_tariff_menu_{user_id}"
    )])

    plan_name = subscription.plan.display_name if subscription.plan else "—"
    await callback.message.edit_text(
        f"⏰ <b>Продление по тарифу {plan_name}</b>\n\n"
        f"Текущее окончание: {format_datetime(subscription.end_date)}\n"
        "Выберите период продления (бесплатно):",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@admin_required
@error_handler
async def execute_tariff_renew(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    days = int(parts[-1])

    if days not in SUPPORTED_PERIOD_DAYS:
        await callback.answer("❌ Недопустимый период", show_alert=True)
        return

    success = await _admin_renew_tariff(db, user_id, days, db_user.id)

    back_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
    ])

    if success:
        await callback.message.edit_text(
            f"✅ Подписка продлена по тарифу на {_tariff_period_label(days)}",
            reply_markup=back_keyboard
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка продления по тарифу",
            reply_markup=back_keyboard
        )
    await callback.answer()


@admin_required
@error_handler
async def confirm_tariff_remove(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    from app.database.crud.subscription import get_subscription_by_user_id

    user_id = int(callback.data.split('_')[-1])

    subscription = await get_subscription_by_user_id(db, user_id)
    if not subscription or subscription.plan_id is None:
        await callback.answer("❌ У пользователя нет тарифа", show_alert=True)
        return

    traffic_text = "♾️" if subscription.traffic_limit_gb == 0 else f"{subscription.traffic_limit_gb} ГБ"
    plan_name = subscription.plan.display_name if subscription.plan else "—"

    await callback.message.edit_text(
        "🗑 <b>Снятие тарифа</b>\n\n"
        f"Тариф <b>{plan_name}</b> будет отвязан — подписка станет legacy.\n"
        f"Текущие лимиты ({traffic_text} / {subscription.device_limit} устр.) "
        "и срок действия сохраняются.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"admin_sub_tariff_remove_confirm_{user_id}"
            )],
            [types.InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=f"admin_sub_tariff_menu_{user_id}"
            )]
        ])
    )
    await callback.answer()


@admin_required
@error_handler
async def execute_tariff_remove(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    user_id = int(callback.data.split('_')[-1])

    success = await _admin_remove_tariff(db, user_id, db_user.id)

    back_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📱 К подписке", callback_data=f"admin_user_subscription_{user_id}")]
    ])

    if success:
        await callback.message.edit_text(
            "✅ Тариф снят — подписка переведена в legacy",
            reply_markup=back_keyboard
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка снятия тарифа",
            reply_markup=back_keyboard
        )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    
    dp.callback_query.register(
        show_users_menu,
        F.data == "admin_users"
    )
    
    dp.callback_query.register(
        show_users_list,
        F.data == "admin_users_list"
    )
    
    dp.callback_query.register(
        show_users_statistics,
        F.data == "admin_users_stats"
    )
    
    dp.callback_query.register(
        show_user_subscription,
        F.data.startswith("admin_user_subscription_")
    )

    dp.callback_query.register(
        show_user_transactions,
        F.data.startswith("admin_user_transactions_")
    )

    dp.callback_query.register(
        show_user_statistics,
        F.data.startswith("admin_user_statistics_")
    )

    dp.callback_query.register(
        block_user,
        F.data.startswith("admin_user_block_confirm_")
    )

    dp.callback_query.register(
        delete_user_account,
        F.data.startswith("admin_user_delete_confirm_")
    )

    dp.callback_query.register(
        confirm_user_block,
        F.data.startswith("admin_user_block_") & ~F.data.contains("confirm")
    )

    dp.callback_query.register(
        unblock_user,
        F.data.startswith("admin_user_unblock_confirm_")
    )

    dp.callback_query.register(
        confirm_user_unblock,
        F.data.startswith("admin_user_unblock_") & ~F.data.contains("confirm")
    )

    dp.callback_query.register(
        confirm_user_delete,
        F.data.startswith("admin_user_delete_") & ~F.data.contains("confirm")
    )
    
    dp.callback_query.register(
        handle_users_list_pagination_fixed,
        F.data.startswith("admin_users_list_page_")
    )
    
    dp.callback_query.register(
        handle_users_balance_list_pagination,
        F.data.startswith("admin_users_balance_list_page_")
    )
    
    dp.callback_query.register(
        handle_users_traffic_list_pagination,
        F.data.startswith("admin_users_traffic_list_page_")
    )

    dp.callback_query.register(
        handle_users_activity_list_pagination,
        F.data.startswith("admin_users_activity_list_page_")
    )

    dp.callback_query.register(
        handle_users_spending_list_pagination,
        F.data.startswith("admin_users_spending_list_page_")
    )

    dp.callback_query.register(
        handle_users_purchases_list_pagination,
        F.data.startswith("admin_users_purchases_list_page_")
    )

    dp.callback_query.register(
        handle_users_ready_to_renew_pagination,
        F.data.startswith("admin_users_ready_to_renew_list_page_")
    )

    dp.callback_query.register(
        handle_users_campaign_list_pagination,
        F.data.startswith("admin_users_campaign_list_page_")
    )
    
    dp.callback_query.register(
        start_user_search,
        F.data.in_({"admin_users_search", "admin_users_search_root"})
    )
    
    dp.message.register(
        process_user_search,
        AdminStates.waiting_for_user_search
    )
    
    dp.callback_query.register(
        show_user_management,
        F.data.startswith("admin_user_manage_")
    )

    dp.callback_query.register(
        show_user_promo_group,
        F.data.startswith("admin_user_promo_group_") & ~F.data.contains("_set_") & ~F.data.contains("_toggle_")
    )

    dp.callback_query.register(
        set_user_promo_group,
        F.data.startswith("admin_user_promo_group_toggle_")
    )

    dp.callback_query.register(
        toggle_user_partner_account,
        F.data.startswith("admin_user_partner_toggle_")
    )

    dp.callback_query.register(
        start_balance_edit,
        F.data.startswith("admin_user_balance_")
    )

    dp.message.register(
        process_balance_edit,
        AdminStates.editing_user_balance
    )

    dp.callback_query.register(
        show_user_referrals,
        F.data.startswith("admin_user_referrals_") & ~F.data.contains("_edit")
    )

    dp.callback_query.register(
        start_edit_referral_percent,
        F.data.startswith("admin_user_referral_percent_")
        & ~F.data.contains("_set_")
        & ~F.data.contains("_reset")
    )

    dp.callback_query.register(
        set_referral_percent_button,
        F.data.startswith("admin_user_referral_percent_set_")
        | F.data.startswith("admin_user_referral_percent_reset_")
    )

    dp.message.register(
        process_referral_percent_input,
        AdminStates.editing_user_referral_percent,
    )

    dp.callback_query.register(
        start_edit_user_referrals,
        F.data.startswith("admin_user_referrals_edit_")
    )

    dp.message.register(
        process_edit_user_referrals,
        AdminStates.editing_user_referrals
    )

    dp.callback_query.register(
        start_send_user_message,
        F.data.startswith("admin_user_send_message_")
    )

    dp.message.register(
        process_send_user_message,
        AdminStates.sending_user_message
    )
    
    dp.callback_query.register(
        show_inactive_users,
        F.data == "admin_users_inactive"
    )
    
    dp.callback_query.register(
        cleanup_inactive_users,
        F.data == "admin_cleanup_inactive"
    )

    
    dp.callback_query.register(
        extend_user_subscription,
        F.data.startswith("admin_sub_extend_") & ~F.data.contains("days") & ~F.data.contains("confirm")
    )
    
    dp.callback_query.register(
        process_subscription_extension_days,
        F.data.startswith("admin_sub_extend_days_")
    )
    
    dp.message.register(
        process_subscription_extension_text,
        AdminStates.extending_subscription
    )
    
    dp.callback_query.register(
        add_subscription_traffic,
        F.data.startswith("admin_sub_traffic_") & ~F.data.contains("add")
    )
    
    dp.callback_query.register(
        process_traffic_addition_button,
        F.data.startswith("admin_sub_traffic_add_")
    )
    
    dp.message.register(
        process_traffic_addition_text,
        AdminStates.adding_traffic
    )
    
    dp.callback_query.register(
        deactivate_user_subscription,
        F.data.startswith("admin_sub_deactivate_") & ~F.data.contains("confirm")
    )
    
    dp.callback_query.register(
        confirm_subscription_deactivation,
        F.data.startswith("admin_sub_deactivate_confirm_")
    )
    
    dp.callback_query.register(
        activate_user_subscription,
        F.data.startswith("admin_sub_activate_")
    )
    
    dp.callback_query.register(
        grant_trial_subscription,
        F.data.startswith("admin_sub_grant_trial_")
    )
    
    dp.callback_query.register(
        grant_paid_subscription,
        F.data.startswith("admin_sub_grant_") & ~F.data.contains("trial") & ~F.data.contains("days")
    )
    
    dp.callback_query.register(
        process_subscription_grant_days,
        F.data.startswith("admin_sub_grant_days_")
    )
    
    dp.message.register(
        process_subscription_grant_text,
        AdminStates.granting_subscription
    )

    dp.callback_query.register(
        show_user_tariff_menu,
        F.data.startswith("admin_sub_tariff_menu_")
    )

    dp.callback_query.register(
        show_tariff_assign_plans,
        F.data.startswith("admin_sub_tariff_assign_")
    )

    dp.callback_query.register(
        show_tariff_assign_periods,
        F.data.startswith("admin_sub_tariff_plan_")
    )

    dp.callback_query.register(
        execute_tariff_assign,
        F.data.startswith("admin_sub_tariff_apply_confirm_")
    )

    dp.callback_query.register(
        confirm_tariff_assign,
        F.data.startswith("admin_sub_tariff_apply_") & ~F.data.contains("confirm")
    )

    dp.callback_query.register(
        execute_tariff_renew,
        F.data.startswith("admin_sub_tariff_renew_go_")
    )

    dp.callback_query.register(
        show_tariff_renew_periods,
        F.data.startswith("admin_sub_tariff_renew_") & ~F.data.contains("_go_")
    )

    dp.callback_query.register(
        execute_tariff_remove,
        F.data.startswith("admin_sub_tariff_remove_confirm_")
    )

    dp.callback_query.register(
        confirm_tariff_remove,
        F.data.startswith("admin_sub_tariff_remove_") & ~F.data.contains("confirm")
    )

    dp.callback_query.register(
        show_user_servers_management,
        F.data.startswith("admin_user_servers_")
    )
    
    dp.callback_query.register(
        show_server_selection,
        F.data.startswith("admin_user_change_server_")
    )
    
    dp.callback_query.register(
        toggle_user_server,
        F.data.startswith("admin_user_toggle_server_") & ~F.data.endswith("_add") & ~F.data.endswith("_remove")
    )
    
    dp.callback_query.register(
        start_devices_edit,
        F.data.startswith("admin_user_devices_") & ~F.data.contains("set")
    )
    
    dp.callback_query.register(
        set_user_devices_button,
        F.data.startswith("admin_user_devices_set_")
    )
    
    dp.message.register(
        process_devices_edit_text,
        AdminStates.editing_user_devices
    )
    
    dp.callback_query.register(
        start_traffic_edit,
        F.data.startswith("admin_user_traffic_") & ~F.data.contains("set")
    )
    
    dp.callback_query.register(
        set_user_traffic_button,
        F.data.startswith("admin_user_traffic_set_")
    )
    
    dp.message.register(
        process_traffic_edit_text,
        AdminStates.editing_user_traffic
    )
    
    dp.callback_query.register(
        confirm_reset_devices,
        F.data.startswith("admin_user_reset_devices_") & ~F.data.contains("confirm")
    )
    
    dp.callback_query.register(
        reset_user_devices,
        F.data.startswith("admin_user_reset_devices_confirm_")
    )

    dp.callback_query.register(
        change_subscription_type,
        F.data.startswith("admin_sub_change_type_")
    )
    
    dp.callback_query.register(
        change_subscription_type_confirm,
        F.data.startswith("admin_sub_type_")
    )
    
    # Регистрация обработчика покупки подписки администратором
    dp.callback_query.register(
        admin_buy_subscription,
        F.data.startswith("admin_sub_buy_")
    )
    
    # Регистрация дополнительных обработчиков для покупки подписки
    dp.callback_query.register(
        admin_buy_subscription_confirm,
        F.data.startswith("admin_buy_sub_confirm_")
    )
    
    dp.callback_query.register(
        admin_buy_subscription_execute,
        F.data.startswith("admin_buy_sub_execute_")
    )
    
    # Регистрация обработчиков для фильтрации пользователей
    dp.callback_query.register(
        show_users_filters,
        F.data == "admin_users_filters"
    )
    
    dp.callback_query.register(
        show_users_list_by_balance,
        F.data == "admin_users_balance_filter"
    )
    
    dp.callback_query.register(
        show_users_list_by_traffic,
        F.data == "admin_users_traffic_filter"
    )

    dp.callback_query.register(
        show_users_list_by_last_activity,
        F.data == "admin_users_activity_filter"
    )

    dp.callback_query.register(
        show_users_list_by_spending,
        F.data == "admin_users_spending_filter"
    )

    dp.callback_query.register(
        show_users_list_by_purchases,
        F.data == "admin_users_purchases_filter"
    )
    
    dp.callback_query.register(
        show_users_ready_to_renew,
        F.data == "admin_users_ready_to_renew_filter"
    )

    dp.callback_query.register(
        show_users_list_by_campaign,
        F.data == "admin_users_campaign_filter"
    )
    
