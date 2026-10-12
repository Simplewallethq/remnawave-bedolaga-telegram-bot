"""Хендлер AuraPay: кнопка «Проверить оплату» под счётом из роутера."""

from __future__ import annotations

import logging
from typing import Dict

from aiogram import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService, get_user_by_id as fetch_user_by_id
from app.utils.decorators import error_handler

logger = logging.getLogger(__name__)


@error_handler
async def check_aurapay_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    try:
        local_payment_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный идентификатор платежа", show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    status_info = await payment_service.get_aurapay_payment_status(db, local_payment_id)

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
        logger.debug("Не удалось получить пользователя для статуса AuraPay: %s", error)

    texts = get_texts(user_language)
    status_labels: Dict[str, Dict[str, str]] = {
        "PENDING": {"emoji": "⏳", "label": texts.t("AURAPAY_STATUS_PENDING", "Ожидает оплаты")},
        "PAID": {"emoji": "✅", "label": texts.t("AURAPAY_STATUS_PAID", "Оплачен")},
        "EXPIRED": {"emoji": "⌛", "label": texts.t("AURAPAY_STATUS_EXPIRED", "Истёк")},
        "REFUNDED": {"emoji": "↩️", "label": texts.t("AURAPAY_STATUS_REFUNDED", "Возвращён")},
    }
    label_info = status_labels.get(
        payment.status,
        {"emoji": "❓", "label": texts.t("AURAPAY_STATUS_UNKNOWN", "Неизвестно")},
    )

    message_lines = [
        texts.t("ONEPAYMENT_STATUS_TITLE", "🏦 <b>Статус платежа {provider}</b>").format(
            provider=settings.get_aurapay_display_name()
        ),
        "",
        f"🆔 ID: {payment.order_id}",
        f"💰 Сумма: {settings.format_price(payment.amount_kopeks)}",
        f"📊 Статус: {label_info['emoji']} {label_info['label']}",
        f"📅 Создан: {payment.created_at.strftime('%d.%m.%Y %H:%M') if payment.created_at else '—'}",
    ]

    if payment.is_paid:
        message_lines.append(
            "\n"
            + texts.t(
                "ONEPAYMENT_STATUS_PAID_NOTE",
                "✅ Платеж успешно завершен! Средства уже на балансе.",
            )
        )
    elif payment.status == "PENDING":
        message_lines.append(
            "\n"
            + texts.t(
                "ONEPAYMENT_STATUS_PENDING_NOTE",
                "⏳ Платеж еще не завершен. Завершите оплату по ссылке и проверьте статус позже.",
            )
        )

    await callback.message.answer("\n".join(message_lines), parse_mode="HTML")
    await callback.answer()
