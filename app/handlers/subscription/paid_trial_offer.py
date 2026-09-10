"""Гейт «активация за 1 ₽» (A/B вместо бесплатного триала, только бот).

Экраны по документу «1 рубль»:
1. гейт — «Активируй N дня доступа», кнопка «Активировать»;
2. условия — раскрытие рекуррента (цена тарифа продления), кнопка
   «Активировать за 1 ₽» и справка «Как отменить автосписание»;
3. счёт — ссылка на оплату по СБП и проверка статуса.

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
from app.utils.formatters import format_days_declension
from app.utils.photo_message import edit_or_answer_photo
from app.utils.pricing_utils import format_period_description

logger = logging.getLogger(__name__)

OFFER_CALLBACK = "paid_trial_offer"
TERMS_CALLBACK = "paid_trial_offer_terms"
CANCEL_HELP_CALLBACK = "paid_trial_offer_cancel_help"
PAY_CALLBACK = "paid_trial_offer_pay"

TRIAL_IMAGE_PATH = os.path.join("images", "trial.webp")


def _days_label(language: str) -> str:
    return format_days_declension(settings.get_trial_paid_offer_access_days(), language)


def _price_label() -> str:
    return settings.format_price(settings.get_trial_paid_offer_price_kopeks())


def _renewal_label(texts, plan_name: str, renewal_price_kopeks: int, language: str) -> str:
    """«Solo (290 ₽/мес)» — тариф и цена продления за период."""
    period_days = settings.get_trial_paid_offer_renewal_period_days()
    if period_days == 30:
        period = texts.t("PAID_TRIAL_OFFER_PER_MONTH", "мес")
    else:
        period = format_period_description(period_days, language)
    return f"{plan_name} ({settings.format_price(renewal_price_kopeks)}/{period})"


def _support_button(texts) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"),
        callback_data="menu_support",
    )


def _unavailable(texts) -> str:
    return texts.t("PAID_TRIAL_OFFER_UNAVAILABLE", "Предложение недоступно")


async def _send_screen(
    message_or_callback: Any,
    *,
    is_callback: bool,
    text: str,
    keyboard: types.InlineKeyboardMarkup,
    photo_path: Optional[str],
) -> None:
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


# ------------------------------------------------------------ шаг 1: гейт


def build_gate_text(texts, language: str) -> str:
    return texts.t(
        "PAID_TRIAL_OFFER_GATE_TEXT",
        "🎁 <b>Активируй {days} доступа</b>\n\n"
        "▎ Доступ бесплатный на {days}. Чтобы отсечь РКН ботов и мультиаккаунты, "
        "необходимо пройти активацию через символическую оплату в {price}.\n\n"
        "Дальше расскажем условия — без сюрпризов.",
    ).format(days=_days_label(language), price=_price_label())


def build_gate_keyboard(texts, *, with_back: bool) -> types.InlineKeyboardMarkup:
    rows = [
        [
            types.InlineKeyboardButton(
                text=texts.t("PAID_TRIAL_OFFER_ACTIVATE_BUTTON", "✅ Активировать"),
                callback_data=TERMS_CALLBACK,
            )
        ],
        [_support_button(texts)],
    ]
    if with_back:
        rows.append(
            [types.InlineKeyboardButton(text=texts.t("MAIN_MENU_BUTTON", "⬅️Назад"), callback_data="main_menu")]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def render_paid_trial_offer(
    db: AsyncSession,
    user: User,
    message_or_callback: Any,
    *,
    is_callback: bool,
    language: Optional[str] = None,
) -> bool:
    """Шаг 1 (гейт). False — оффер сейчас показать нельзя (нет тарифа/цены)."""
    lang = language or getattr(user, "language", None) or settings.DEFAULT_LANGUAGE
    texts = get_texts(lang)

    if await trial_paid_offer_service.resolve_plan(db, user) is None:
        return False

    await _send_screen(
        message_or_callback,
        is_callback=is_callback,
        text=build_gate_text(texts, lang),
        keyboard=build_gate_keyboard(texts, with_back=is_callback),
        photo_path=TRIAL_IMAGE_PATH if os.path.exists(TRIAL_IMAGE_PATH) else None,
    )
    return True


async def show_paid_trial_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)
    if not trial_paid_offer_service.is_offer_available(db_user):
        await callback.answer(_unavailable(texts), show_alert=True)
        return
    if not await render_paid_trial_offer(db, db_user, callback, is_callback=True):
        await callback.answer(_unavailable(texts), show_alert=True)
        return
    await callback.answer()


# --------------------------------------------------------- шаг 2: условия


def build_terms_text(texts, language: str, plan_name: str, renewal_price_kopeks: int) -> str:
    return texts.t(
        "PAID_TRIAL_OFFER_TERMS_TEXT",
        "💳 <b>Активация за {price}</b>\n\n"
        "Сейчас спишем <b>{price}</b> — это подтверждает, что ты живой человек, а не бот.\n\n"
        "▎ Первые <b>{days} — бесплатно</b>.\n"
        "▎ После пробного периода доступ продолжится на тарифе <b>{renewal}</b>, "
        "списание автоматически со счета.\n\n"
        "▎<b>Отменить можно в любой момент</b> в разделе «Управление подпиской» — "
        "тогда ничего не спишется.",
    ).format(
        price=_price_label(),
        days=_days_label(language),
        renewal=_renewal_label(texts, plan_name, renewal_price_kopeks, language),
    )


def build_terms_keyboard(texts) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t("PAID_TRIAL_OFFER_PAY_BUTTON", "✅ Активировать за {price}").format(
                        price=_price_label()
                    ),
                    callback_data=PAY_CALLBACK,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("PAID_TRIAL_OFFER_CANCEL_HELP_BUTTON", "❔ Как отменить автосписание"),
                    callback_data=CANCEL_HELP_CALLBACK,
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=OFFER_CALLBACK)],
        ]
    )


async def show_paid_trial_terms(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)
    if not trial_paid_offer_service.is_offer_available(db_user):
        await callback.answer(_unavailable(texts), show_alert=True)
        return
    resolved = await trial_paid_offer_service.resolve_plan(db, db_user)
    if resolved is None:
        await callback.answer(_unavailable(texts), show_alert=True)
        return
    plan, renewal_price = resolved

    await _send_screen(
        callback,
        is_callback=True,
        text=build_terms_text(texts, db_user.language, plan.display_name, renewal_price),
        keyboard=build_terms_keyboard(texts),
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


async def show_paid_trial_cancel_help(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)
    text = texts.t(
        "PAID_TRIAL_OFFER_CANCEL_HELP_TEXT",
        "❔ <b>Как отменить автосписание</b>\n\n"
        "В любой момент: «Управление подпиской → Автоплатеж → Отключить автоплатёж СБП». "
        "После отключения ничего не спишется, а доступ останется до конца оплаченного срока.\n\n"
        "Если что-то не получается — напиши в поддержку, поможем.",
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [_support_button(texts)],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=TERMS_CALLBACK)],
        ]
    )
    await _send_screen(
        callback,
        is_callback=True,
        text=text,
        keyboard=keyboard,
        photo_path=PAY_IMAGE_PATH if os.path.exists(PAY_IMAGE_PATH) else None,
    )
    await callback.answer()


# ------------------------------------------------------------ шаг 3: счёт


async def pay_paid_trial_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)
    if not trial_paid_offer_service.is_offer_available(db_user):
        await callback.answer(_unavailable(texts), show_alert=True)
        return

    result = await trial_paid_offer_service.create_offer_invoice(db, callback.bot, db_user)
    payment_url = (result or {}).get("payment_url")
    if not result or not payment_url:
        await callback.answer(
            texts.t("PAID_TRIAL_OFFER_INVOICE_ERROR", "Не удалось создать счёт. Попробуйте позже."),
            show_alert=True,
        )
        return

    snapshot = result.get("snapshot") or {}
    price_label = settings.format_price(int(snapshot.get("price_kopeks") or 0))
    text = texts.t(
        "PAID_TRIAL_OFFER_INVOICE_TEXT",
        "💳 <b>Оплата — {price}</b>\n\n"
        "Нажми кнопку ниже: оплата по СБП ({provider}) занимает до 10 секунд. "
        "После оплаты доступ включится автоматически, ссылка придёт сюда.",
    ).format(price=price_label, provider=settings.get_onepayment_display_name())

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
            [_support_button(texts)],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=TERMS_CALLBACK)],
        ]
    )

    await _send_screen(
        callback,
        is_callback=True,
        text=text,
        keyboard=keyboard,
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
    dp.callback_query.register(show_paid_trial_terms, F.data == TERMS_CALLBACK)
    dp.callback_query.register(show_paid_trial_cancel_help, F.data == CANCEL_HELP_CALLBACK)
    dp.callback_query.register(pay_paid_trial_offer, F.data == PAY_CALLBACK)
