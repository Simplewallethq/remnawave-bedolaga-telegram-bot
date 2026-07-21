from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils.timezone import format_local_datetime


def build_success_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Управление подпиской",
                    callback_data="menu_subscription",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu",
                )
            ],
        ]
    )


def format_topup_success_message(amount: str) -> str:
    return (
        "✅ <b>Платёж прошёл</b>\n"
        f"Зачислено на баланс: {amount}\n\n"
        "⚠️Важно: пополнение баланса не активирует подписку — оформи её отдельно."
    )


def subscription_plan_name(subscription: Any = None, plan: Any = None) -> str:
    source = plan or getattr(subscription, "plan", None)
    return (
        getattr(source, "display_name", None)
        or getattr(source, "name", None)
        or getattr(source, "code", None)
        or "VPN"
    )


def format_days_ru(days: int | str | None) -> str:
    if days is None:
        return "период"

    try:
        value = int(days)
    except (TypeError, ValueError):
        return str(days)

    mod_100 = value % 100
    mod_10 = value % 10
    if 11 <= mod_100 <= 14:
        suffix = "дней"
    elif mod_10 == 1:
        suffix = "день"
    elif 2 <= mod_10 <= 4:
        suffix = "дня"
    else:
        suffix = "дней"
    return f"{value} {suffix}"


def format_success_date(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return format_local_datetime(value, "%d.%m.%Y")
    return str(value or "—")


def format_subscription_purchase_success(
    *,
    plan: str,
    period: int | str | None,
    end_date: datetime | str | None,
) -> str:
    safe_plan = escape(plan)
    return (
        "✅ <b>Подписка активирована</b>\n"
        f"Тариф {safe_plan} · {format_days_ru(period)}, работает до {format_success_date(end_date)}."
    )


def format_subscription_renewal_success(
    *,
    plan: str,
    days: int | str | None,
    end_date: datetime | str | None,
) -> str:
    safe_plan = escape(plan)
    return (
        "✅ <b>Подписка продлена</b>\n"
        f"Тариф {safe_plan} · +{format_days_ru(days)}, работает до {format_success_date(end_date)}."
    )
