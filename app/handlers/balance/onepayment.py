"""Хендлеры 1Payment: проверка счёта, кнопка «Оплатить картой» (через Platega)
и управление СБП-привязкой в меню «Автоплатеж»."""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from aiogram import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import OnePaymentBinding, User
from app.localization.texts import get_texts
from app.services.payment.onepayment_card_alt import (
    get_card_alt_block,
    parse_card_alt_callback,
)
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


async def resolve_renewal_charge(
    db: AsyncSession, db_user: User
) -> Optional[Dict[str, Any]]:
    """Что и когда спишется за продление: {"name", "price_kopeks", "period_days"}.

    None — подписки нет или цена не считается. Для тарифных подписок —
    цена плана на купленный период в когорте пользователя, для legacy — расчёт
    за 30 дней.
    """
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
            return {"name": plan.display_name, "price_kopeks": int(price), "period_days": period_days}

        from app.services.subscription_service import SubscriptionService

        price = await SubscriptionService().calculate_renewal_price(
            subscription, 30, db, user=db_user
        )
        return {"name": None, "price_kopeks": int(price), "period_days": 30}
    except Exception as error:  # pragma: no cover - меню не должно падать
        logger.debug("1Payment: не удалось посчитать цену продления для меню: %s", error)
        return None


def _period_label(texts, period_days: int, language: str) -> str:
    """«месяц», «3 месяца», «год» — для строки «Сумма: … раз в {период}»."""
    from app.utils.pricing_utils import format_period_description

    fixed = {
        30: texts.t("AUTOPAY_PERIOD_MONTH", "месяц"),
        90: texts.t("AUTOPAY_PERIOD_3_MONTHS", "3 месяца"),
        180: texts.t("AUTOPAY_PERIOD_6_MONTHS", "полгода"),
        360: texts.t("AUTOPAY_PERIOD_YEAR", "год"),
        720: texts.t("AUTOPAY_PERIOD_2_YEARS", "2 года"),
    }
    return fixed.get(period_days) or format_period_description(period_days, language)


async def build_onepayment_autopay_status(
    db: AsyncSession, db_user: User, binding: Optional[OnePaymentBinding]
) -> Optional[Dict[str, Any]]:
    """Описание активного автоплатежа по СБП для меню «Автоплатеж».

    None — привязки нет. Иначе {"active": bool, "note": str | None,
    "amount_kopeks", "period_label", "next_charge_at", "lead_label"}: при
    приостановке/сбое active=False и note с пояснением.
    """
    if binding is None:
        return None
    texts = get_texts(db_user.language)
    provider = settings.get_onepayment_display_name()

    if binding.status == OnePaymentBinding.STATUS_FAILED:
        return {
            "active": False,
            "note": texts.t(
                "ONEPAYMENT_AUTOPAY_STATUS_FAILED",
                "СБП ({provider}): ⚠️ автосписание не удалось — требуется переподключение "
                "(оплатите подписку по СБП ещё раз).",
            ).format(provider=provider),
        }

    if not settings.is_onepayment_recurring_enabled():
        return {
            "active": False,
            "note": texts.t(
                "ONEPAYMENT_AUTOPAY_STATUS_PAUSED",
                "СБП ({provider}): привязка активна, автосписание временно приостановлено.",
            ).format(provider=provider),
        }

    charge = await resolve_renewal_charge(db, db_user)
    subscription = getattr(db_user, "subscription", None)
    end_date = getattr(subscription, "end_date", None)
    if getattr(subscription, "is_paid_trial", False):
        hours = settings.get_trial_paid_offer_recurring_hours_before()
        lead = timedelta(hours=hours)
        lead_label = texts.t("AUTOPAY_LEAD_HOURS", "{hours} ч.").format(hours=hours)
    else:
        days = settings.get_onepayment_recurring_days_before()
        lead = timedelta(days=days)
        lead_label = texts.t("AUTOPAY_LEAD_DAYS", "{days} дн.").format(days=days)

    return {
        "active": True,
        "note": None,
        "amount_kopeks": charge["price_kopeks"] if charge else None,
        "period_label": _period_label(texts, charge["period_days"], db_user.language) if charge else None,
        "next_charge_at": (end_date - lead) if end_date else None,
        "lead_label": lead_label,
    }


async def build_onepayment_autopay_status_line(
    db: AsyncSession, db_user: User, binding: Optional[OnePaymentBinding]
) -> Optional[str]:
    """Совместимая строка о СБП-привязке (используется вне меню «Автоплатеж»)."""
    status = await build_onepayment_autopay_status(db, db_user, binding)
    if status is None:
        return None
    if not status["active"]:
        return status["note"]
    texts = get_texts(db_user.language)
    provider = settings.get_onepayment_display_name()
    if status["amount_kopeks"] is not None:
        return texts.t(
            "ONEPAYMENT_AUTOPAY_STATUS_LINE",
            "СБП ({provider}): ✅ продление {price} списывается автоматически "
            "за {days} до окончания подписки (за вычетом баланса).",
        ).format(provider=provider, price=settings.format_price(status["amount_kopeks"]), days=status["lead_label"])
    return texts.t(
        "ONEPAYMENT_AUTOPAY_STATUS_LINE_NO_PRICE",
        "СБП ({provider}): ✅ продление подписки списывается автоматически "
        "за {days} до окончания (за вычетом баланса).",
    ).format(provider=provider, days=status["lead_label"])


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
    card_alt_paid = False
    card_alt_status: Optional[str] = None
    card_alt = get_card_alt_block(getattr(payment, "metadata_json", None))
    if card_alt and not payment.is_paid:
        # Пользователь мог заплатить картой через Platega — проверяем и её счёт
        # (при пропущенном вебхуке проверка сама финализирует оплату).
        try:
            card_info = await payment_service.get_platega_payment_status(
                db, int(card_alt["platega_payment_id"])
            )
        except Exception as error:
            logger.warning("Не удалось проверить карточный счёт для 1Payment: %s", error)
            card_info = None
        if card_info:
            card_alt_paid = bool(card_info.get("is_paid"))
            card_alt_status = str(card_info.get("status") or "")

    if payment.is_paid or card_alt_paid:
        message_lines.append(
            "\n"
            + texts.t(
                "ONEPAYMENT_STATUS_PAID_NOTE",
                "✅ Платеж успешно завершен! Средства уже на балансе.",
            )
        )
    elif payment.status in {"INIT", "PENDING"}:
        if card_alt_status:
            message_lines.append(
                texts.t(
                    "ONEPAYMENT_STATUS_CARD_LINE", "💳 Оплата картой: {status}"
                ).format(status=card_alt_status)
            )
        message_lines.append(
            "\n"
            + texts.t(
                "ONEPAYMENT_STATUS_PENDING_NOTE",
                "⏳ Платеж еще не завершен. Завершите оплату по ссылке и проверьте статус позже.",
            )
        )

    await callback.message.answer("\n".join(message_lines), parse_mode="HTML")
    await callback.answer()


def _card_alt_button_text(texts, amount_kopeks: int) -> str:
    return texts.t(
        "ONEPAYMENT_CARD_ALT_BUTTON", "💳 Оплатить картой – {amount}"
    ).format(amount=settings.format_price(amount_kopeks))


def _replace_card_alt_button(
    markup: Optional[types.InlineKeyboardMarkup],
    *,
    callback_data: str,
    text: str,
    url: str,
) -> Optional[types.InlineKeyboardMarkup]:
    """Та же клавиатура, где кнопка-callback «Оплатить картой» стала URL-кнопкой."""
    if markup is None:
        return None
    rows = []
    replaced = False
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            if button.callback_data == callback_data:
                new_row.append(types.InlineKeyboardButton(text=text, url=url))
                replaced = True
            else:
                new_row.append(button)
        rows.append(new_row)
    if not replaced:
        return None
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@error_handler
async def request_onepayment_card_alternative(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """«Оплатить картой» на СБП-счёте 1Payment: выставляет карточный счёт Platega
    и подменяет кнопку на ссылку оплаты."""
    from app.services import payment_service as payment_module

    texts = get_texts(db_user.language)
    local_payment_id = parse_card_alt_callback(callback.data)
    if local_payment_id is None:
        await callback.answer("❌ Некорректный идентификатор платежа", show_alert=True)
        return

    payment = await payment_module.get_onepayment_payment_by_id(db, local_payment_id)
    if payment is None or payment.user_id != db_user.id:
        await callback.answer(
            texts.t("ONEPAYMENT_CARD_ALT_NOT_FOUND", "❌ Счёт не найден"), show_alert=True
        )
        return
    if payment.is_paid:
        await callback.answer(
            texts.t("ONEPAYMENT_CARD_ALT_ALREADY_PAID", "✅ Этот счёт уже оплачен"),
            show_alert=True,
        )
        return

    result = await PaymentService(callback.bot).create_onepayment_card_alternative(db, payment)
    if not result:
        await callback.answer(
            texts.t(
                "ONEPAYMENT_CARD_ALT_ERROR",
                "❌ Не удалось подготовить оплату картой. Попробуйте позже или оплатите по СБП.",
            ),
            show_alert=True,
        )
        return

    button_text = _card_alt_button_text(texts, payment.amount_kopeks)
    markup = _replace_card_alt_button(
        callback.message.reply_markup if callback.message else None,
        callback_data=callback.data,
        text=button_text,
        url=result["redirect_url"],
    )

    edited = False
    if markup is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
            edited = True
        except Exception as error:  # pragma: no cover - зависит от Telegram
            logger.warning("Не удалось обновить клавиатуру счёта 1Payment: %s", error)

    if edited:
        await callback.answer(
            texts.t(
                "ONEPAYMENT_CARD_ALT_READY",
                "💳 Ссылка готова — нажмите «Оплатить картой» ещё раз",
            ),
            show_alert=False,
        )
        return

    # Кнопку заменить не вышло (сообщение без клавиатуры / старое) — шлём ссылку отдельно.
    await callback.message.answer(
        texts.t(
            "ONEPAYMENT_CARD_ALT_MESSAGE",
            "💳 <b>Оплата картой — {amount}</b>\n\n🔒 Защищённый платеж\nОбычно занимает до 10 секунд",
        ).format(amount=settings.format_price(payment.amount_kopeks)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=button_text, url=result["redirect_url"])],
                [
                    types.InlineKeyboardButton(
                        text=texts.t("CHECK_STATUS_BUTTON", "\U0001f4ca Проверить статус"),
                        callback_data=f"check_platega_{result['platega_payment_id']}",
                    )
                ],
            ]
        ),
        parse_mode="HTML",
    )
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
