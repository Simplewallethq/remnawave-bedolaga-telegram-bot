"""Экраны оффера «доступ на сутки за 1 ₽» (A/B вместо бесплатного триала).

Показывается только пользователям варианта `paid_trial` без подписки —
`trial_paid_offer_service.is_offer_available`. Счёт выставляется через 1Payment
напрямую (мимо роутера), оплата привязывает СБП к автопродлению; активацию
проводит колбек 1Payment по снимку `paid_trial` в metadata счёта.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.trial_paid_offer_service import (
    PAY_IMAGE_PATH,
    trial_paid_offer_service,
)
from app.utils.photo_message import edit_or_answer_photo

logger = logging.getLogger(__name__)

OFFER_CALLBACK = "paid_trial_offer"
PAY_CALLBACK = "paid_trial_offer_pay"


def _offer_text(texts, plan_name: str, renewal_price_kopeks: int) -> str:
    return texts.t(
        "PAID_TRIAL_OFFER_TEXT",
        "⚡ <b>Доступ {name} на {days} дн. за {price}</b>\n\n"
        "Оплати по СБП — и через минуту VPN уже работает.\n\n"
        "🔁 Дальше подписка продлевается автоматически: {renewal_price} за {period} дн. "
        "Автоплатёж можно отключить в любой момент в разделе "
        "«Управление подпиской → Автоплатеж».",
    ).format(
        name=plan_name,
        days=settings.get_trial_paid_offer_access_days(),
        price=settings.format_price(settings.get_trial_paid_offer_price_kopeks()),
        renewal_price=settings.format_price(renewal_price_kopeks),
        period=settings.get_trial_paid_offer_renewal_period_days(),
    )


def _offer_keyboard(texts) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t("PAID_TRIAL_OFFER_PAY_BUTTON", "⚡ Оплатить {price}").format(
                        price=settings.format_price(settings.get_trial_paid_offer_price_kopeks())
                    ),
                    callback_data=PAY_CALLBACK,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("PAID_TRIAL_OFFER_TARIFFS_BUTTON", "💎 Смотреть тарифы"),
                    callback_data="subscription_tariffs",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("MAIN_MENU_BUTTON", "⬅️Назад"),
                    callback_data="main_menu",
                )
            ],
        ]
    )


async def render_paid_trial_offer(
    db: AsyncSession,
    user: User,
    message_or_callback: Any,
    *,
    is_callback: bool,
    language: Optional[str] = None,
) -> bool:
    """Рисует экран оффера. False — оффер сейчас показать нельзя (нет тарифа/цены)."""
    lang = language or getattr(user, "language", None) or settings.DEFAULT_LANGUAGE
    texts = get_texts(lang)

    resolved = await trial_paid_offer_service.resolve_plan(db, user)
    if resolved is None:
        return False
    plan, renewal_price = resolved

    text = _offer_text(texts, plan.display_name, renewal_price)
    keyboard = _offer_keyboard(texts)
    photo_path = PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None

    if is_callback:
        await edit_or_answer_photo(
            callback=message_or_callback,
            caption=text,
            keyboard=keyboard,
            parse_mode="HTML",
            photo_path=photo_path,
        )
    elif photo_path:
        await message_or_callback.answer_photo(
            types.FSInputFile(photo_path),
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="HTML")
    return True


async def show_paid_trial_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)
    if not trial_paid_offer_service.is_offer_available(db_user):
        await callback.answer(
            texts.t("PAID_TRIAL_OFFER_UNAVAILABLE", "Предложение недоступно"),
            show_alert=True,
        )
        return

    shown = await render_paid_trial_offer(db, db_user, callback, is_callback=True)
    if not shown:
        await callback.answer(
            texts.t("PAID_TRIAL_OFFER_UNAVAILABLE", "Предложение недоступно"),
            show_alert=True,
        )
        return
    await callback.answer()


async def pay_paid_trial_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)
    if not trial_paid_offer_service.is_offer_available(db_user):
        await callback.answer(
            texts.t("PAID_TRIAL_OFFER_UNAVAILABLE", "Предложение недоступно"),
            show_alert=True,
        )
        return

    result = await trial_paid_offer_service.create_offer_invoice(db, callback.bot, db_user)
    payment_url = (result or {}).get("payment_url")
    if not result or not payment_url:
        await callback.answer(
            texts.t(
                "PAID_TRIAL_OFFER_INVOICE_ERROR",
                "Не удалось создать счёт. Попробуйте позже.",
            ),
            show_alert=True,
        )
        return

    snapshot = result.get("snapshot") or {}
    plan = result.get("plan")
    price_label = settings.format_price(int(snapshot.get("price_kopeks") or 0))
    text = texts.t(
        "PAID_TRIAL_OFFER_INVOICE_TEXT",
        "<b>Оплата — {price}</b>\n"
        "Доступ {name} на {days} дн.\n\n"
        "🔒 Защищённый платёж по СБП ({provider})\n"
        "Обычно занимает до 10 секунд",
    ).format(
        price=price_label,
        name=getattr(plan, "display_name", ""),
        days=snapshot.get("access_days"),
        provider=settings.get_onepayment_display_name(),
    )
    text += "\n\n" + texts.t(
        "ONEPAYMENT_INVOICE_NOTE",
        "🔁 Оплатив по СБП, вы подключаете автопродление подписки: перед окончанием срока "
        "стоимость продления (за вычетом баланса) списывается автоматически. Отключить можно "
        "в разделе «Управление подпиской → Автоплатеж».",
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t("PAYMENT_AUTO_PAY_BUTTON_WITH_AMOUNT", "💳 Оплатить – {amount}").format(
                        amount=price_label
                    ),
                    url=payment_url,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("CHECK_STATUS_BUTTON", "📊 Проверить статус"),
                    callback_data=f"check_onepayment_{result['local_payment_id']}",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"),
                    callback_data="menu_support",
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=OFFER_CALLBACK)],
        ]
    )

    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=keyboard,
        parse_mode="HTML",
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await _remember_invoice_message(db, result, callback)
    await callback.answer()


async def _remember_invoice_message(
    db: AsyncSession, result: dict, callback: types.CallbackQuery
) -> None:
    """Пишет chat/message_id счёта в metadata — колбек удалит его после оплаты."""
    try:
        from app.services import payment_service as payment_module

        payment = await payment_module.get_onepayment_payment_by_id(db, result["local_payment_id"])
        if payment is None:
            return
        metadata = dict(getattr(payment, "metadata_json", {}) or {})
        metadata["invoice_message"] = {
            "chat_id": callback.message.chat.id,
            "message_id": callback.message.message_id,
        }
        await payment_module.update_onepayment_payment(db, payment, metadata=metadata)
    except Exception as error:
        logger.debug("Платный триал: не удалось сохранить сообщение счёта: %s", error)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_paid_trial_offer, F.data == OFFER_CALLBACK)
    dp.callback_query.register(pay_paid_trial_offer, F.data == PAY_CALLBACK)
