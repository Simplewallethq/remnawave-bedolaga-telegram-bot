"""Хендлеры 1Payment: проверка счёта и управление СБП-привязкой в меню «Автоплатеж»."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from aiogram import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import OnePaymentBinding, User
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService, get_user_by_id as fetch_user_by_id
from app.utils.decorators import error_handler
from app.utils.photo_message import edit_or_answer_photo

logger = logging.getLogger(__name__)

PAY_IMAGE_PATH = os.path.join("images", "pay.webp")


async def get_onepayment_autopay_binding(
    db: AsyncSession, db_user: User
) -> Optional[OnePaymentBinding]:
    """Привязка для меню: активная, либо последняя FAILED (нужно переподключить)."""
    from app.services import payment_service as payment_module

    if not settings.is_onepayment_configured():
        return None
    binding = await payment_module.get_active_onepayment_binding_for_user(db, db_user.id)
    if binding is not None:
        return binding
    latest = await payment_module.get_latest_onepayment_binding_for_user(db, db_user.id)
    if latest is not None and latest.status == OnePaymentBinding.STATUS_FAILED:
        return latest
    return None


async def _resolve_renewal_price_label(db: AsyncSession, db_user: User) -> Optional[str]:
    subscription = getattr(db_user, "subscription", None)
    if subscription is None:
        return None
    try:
        if subscription.plan_id:
            from app.services.plan_pricing_service import (
                get_plan_by_id,
                get_plan_price,
                resolve_pricing_cohort,
            )

            period_days = subscription.plan_period_days or 0
            plan = subscription.plan or await get_plan_by_id(db, subscription.plan_id)
            if plan is None or period_days <= 0:
                return None
            price = await get_plan_price(
                db, plan.id, period_days, cohort=resolve_pricing_cohort(db_user)
            )
            if price is None:
                return None
            return f"{plan.display_name} · {settings.format_price(int(price))}"

        from app.services.subscription_service import SubscriptionService

        price = await SubscriptionService().calculate_renewal_price(
            subscription, 30, db, user=db_user
        )
        return settings.format_price(int(price))
    except Exception as error:  # pragma: no cover - меню не должно падать
        logger.debug("1Payment: не удалось посчитать цену продления для меню: %s", error)
        return None


async def build_onepayment_autopay_status_line(
    db: AsyncSession, db_user: User, binding: Optional[OnePaymentBinding]
) -> Optional[str]:
    """Строка о СБП-привязке для меню «Автоплатеж». None — привязки нет."""
    if binding is None:
        return None
    texts = get_texts(db_user.language)
    provider = settings.get_onepayment_display_name()

    if binding.status == OnePaymentBinding.STATUS_FAILED:
        return texts.t(
            "ONEPAYMENT_AUTOPAY_STATUS_FAILED",
            "СБП ({provider}): ⚠️ автосписание не удалось — требуется переподключение "
            "(оплатите подписку по СБП ещё раз).",
        ).format(provider=provider)

    if not settings.is_onepayment_recurring_enabled():
        return texts.t(
            "ONEPAYMENT_AUTOPAY_STATUS_PAUSED",
            "СБП ({provider}): привязка активна, автосписание временно приостановлено.",
        ).format(provider=provider)

    price_label = await _resolve_renewal_price_label(db, db_user)
    days = settings.get_onepayment_recurring_days_before()
    if price_label:
        return texts.t(
            "ONEPAYMENT_AUTOPAY_STATUS_LINE",
            "СБП ({provider}): ✅ продление {price} списывается автоматически "
            "за {days} дн. до окончания подписки (за вычетом баланса).",
        ).format(provider=provider, price=price_label, days=days)
    return texts.t(
        "ONEPAYMENT_AUTOPAY_STATUS_LINE_NO_PRICE",
        "СБП ({provider}): ✅ продление подписки списывается автоматически "
        "за {days} дн. до окончания (за вычетом баланса).",
    ).format(provider=provider, days=days)


@error_handler
async def check_onepayment_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    try:
        local_payment_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный идентификатор платежа", show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    status_info = await payment_service.get_onepayment_payment_status(db, local_payment_id)

    if not status_info:
        await callback.answer("❌ Платеж не найден", show_alert=True)
        return

    payment = status_info["payment"]

    user_language = "ru"
    try:
        user = await fetch_user_by_id(db, payment.user_id)
        if user and getattr(user, "language", None):
            user_language = user.language
    except Exception as error:
        logger.debug("Не удалось получить пользователя для статуса 1Payment: %s", error)

    texts = get_texts(user_language)
    status_labels: Dict[str, Dict[str, str]] = {
        "INIT": {"emoji": "⏳", "label": texts.t("ONEPAYMENT_STATUS_INIT", "Ожидает оплаты")},
        "PENDING": {"emoji": "⏳", "label": texts.t("ONEPAYMENT_STATUS_PENDING", "Обрабатывается")},
        "SUCCESS": {"emoji": "✅", "label": texts.t("ONEPAYMENT_STATUS_SUCCESS", "Оплачен")},
        "FAILURE": {"emoji": "❌", "label": texts.t("ONEPAYMENT_STATUS_FAILURE", "Отклонён")},
    }
    label_info = status_labels.get(
        payment.status,
        {"emoji": "❓", "label": texts.t("ONEPAYMENT_STATUS_UNKNOWN", "Неизвестно")},
    )

    message_lines = [
        texts.t(
            "ONEPAYMENT_STATUS_TITLE", "🏦 <b>Статус платежа {provider}</b>"
        ).format(provider=settings.get_onepayment_display_name()),
        "",
        f"🆔 ID: {payment.user_data}",
        f"💰 Сумма: {settings.format_price(payment.amount_kopeks)}",
        f"📊 Статус: {label_info['emoji']} {label_info['label']}",
        f"📅 Создан: {payment.created_at.strftime('%d.%m.%Y %H:%M') if payment.created_at else '—'}",
    ]
    if payment.is_paid:
        message_lines.append("\n✅ Платеж успешно завершен! Средства уже на балансе.")
    elif payment.status in {"INIT", "PENDING"}:
        message_lines.append(
            "\n⏳ Платеж еще не завершен. Завершите оплату по ссылке и проверьте статус позже."
        )

    await callback.message.answer("\n".join(message_lines), parse_mode="HTML")
    await callback.answer()


async def request_onepayment_autopay_cancellation(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    from app.services import payment_service as payment_module

    texts = get_texts(db_user.language)
    binding = await payment_module.get_active_onepayment_binding_for_user(db, db_user.id)
    if binding is None:
        await callback.answer(
            texts.t("ONEPAYMENT_AUTOPAY_NOT_FOUND", "Автоплатёж по СБП не найден."),
            show_alert=True,
        )
        return

    await edit_or_answer_photo(
        callback,
        texts.t(
            "ONEPAYMENT_AUTOPAY_CANCEL_CONFIRM",
            "Отключить автоплатёж по СБП? Подписка перестанет продлеваться автоматически — "
            "продлевать придётся вручную.",
        ),
        types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t("CONFIRM", "✅ Подтвердить"),
                        callback_data=f"subscription_onepayment_autopay_confirm_cancel:{binding.id}",
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.BACK,
                        callback_data="subscription_platega_autopay",
                    )
                ],
            ]
        ),
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


async def confirm_onepayment_autopay_cancellation(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    try:
        binding_id = int(callback.data.rsplit(":", 1)[-1])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    texts = get_texts(db_user.language)
    result = await PaymentService(callback.bot).cancel_onepayment_binding(
        db, binding_id=binding_id, user_id=db_user.id
    )
    if not result:
        await callback.answer(
            texts.t(
                "ONEPAYMENT_AUTOPAY_CANCEL_ERROR",
                "Не удалось отключить автоплатёж по СБП. Попробуйте позже.",
            ),
            show_alert=True,
        )
        return

    from .platega import show_platega_autopay_menu

    await show_platega_autopay_menu(callback, db_user, db, answer_callback=False)
    await callback.answer(
        texts.t("ONEPAYMENT_AUTOPAY_CANCELLED", "Автоплатёж по СБП отключён."),
        show_alert=True,
    )
