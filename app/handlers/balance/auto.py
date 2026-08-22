"""Единая кнопка «Оплатить»: счёт выставляется выбранным роутером шлюзом.

Экран намеренно повторяет текущий универсальный экран Platega — для
пользователя меняется только URL за кнопкой, но не вёрстка. Это условие
чистоты эксперимента: иначе мы бы A/B-тестировали копирайт вместе со шлюзом.

Собственный рендер (а не делегирование в process_<провайдер>_payment_amount)
нужен потому, что провайдерные хендлеры сами отвечают пользователю об ошибке
и чистят FSM — перехватить их отказ и повторить другим шлюзом невозможно.
"""

import logging
import os

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.blacklist_service import blacklist_service
from app.services.payment_gateway_router import (
    SOURCE_BALANCE,
    payment_gateway_router,
)
from app.services.payment_service import PaymentService
from app.utils.decorators import error_handler

from .vpn_deposit_bonus import (
    build_vpn_deposit_bonus_metadata,
    merge_vpn_deposit_bonus_metadata,
    should_bypass_minimum,
)

logger = logging.getLogger(__name__)


def _resolve_source(state_data: dict, default: str) -> str:
    """Поверхность выводим из FSM, чтобы не раздувать callback-данные."""
    from app.services.payment_gateway_router import SOURCE_PARTIAL

    if isinstance(state_data.get("tariff_checkout_summary"), dict):
        return SOURCE_PARTIAL
    return default


@error_handler
async def process_auto_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
    *,
    source: str = SOURCE_BALANCE,
) -> bool:
    texts = get_texts(db_user.language)

    is_blacklisted, blacklist_reason = await blacklist_service.is_user_blacklisted(
        message.from_user.id,
        message.from_user.username,
    )
    if is_blacklisted:
        logger.warning(
            "\U0001f6ab Пользователь %s находится в черном списке: %s",
            message.from_user.id,
            blacklist_reason,
        )
        try:
            await message.answer(
                f"\U0001f6ab Оплата невозможна\n\n"
                f"Причина: {blacklist_reason}\n\n"
                f"Если вы считаете, что это ошибка, обратитесь в поддержку."
            )
        except Exception as error:  # pragma: no cover - диагностический лог
            logger.error("Ошибка при отправке сообщения о блокировке: %s", error)
        return False

    state_data = await state.get_data()
    source = _resolve_source(state_data, source)
    bypass_minimum = should_bypass_minimum(state_data, amount_kopeks)

    eligible = payment_gateway_router.eligible_gateways(
        amount_kopeks, bypass_minimum=bypass_minimum
    )
    if not eligible:
        min_kopeks = payment_gateway_router.combined_min_kopeks()
        max_kopeks = payment_gateway_router.combined_max_kopeks()
        if min_kopeks and amount_kopeks < min_kopeks and not bypass_minimum:
            await message.answer(
                texts.t(
                    "PAYMENT_AUTO_AMOUNT_TOO_LOW",
                    "Минимальная сумма оплаты: {amount}",
                ).format(amount=settings.format_price(min_kopeks))
            )
        elif max_kopeks and amount_kopeks > max_kopeks:
            await message.answer(
                texts.t(
                    "PAYMENT_AUTO_AMOUNT_TOO_HIGH",
                    "Максимальная сумма оплаты: {amount}",
                ).format(amount=settings.format_price(max_kopeks))
            )
        else:
            await message.answer(
                texts.t(
                    "PAYMENT_AUTO_UNAVAILABLE",
                    "❌ Оплата временно недоступна. Попробуйте позже "
                    "или обратитесь в поддержку.",
                )
            )
        return False

    routed = await payment_gateway_router.create_invoice(
        db,
        payment_service=PaymentService(message.bot),
        user=db_user,
        amount_kopeks=amount_kopeks,
        source=source,
        description=settings.get_balance_payment_description(amount_kopeks),
        language=db_user.language,
        metadata=build_vpn_deposit_bonus_metadata(
            db_user,
            state_data,
            amount_kopeks=amount_kopeks,
        ),
        bypass_minimum=bypass_minimum,
    )

    if not routed:
        await message.answer(
            texts.t(
                "PAYMENT_AUTO_CREATE_ERROR",
                "❌ Не удалось создать счёт. Попробуйте позже "
                "или обратитесь в поддержку.",
            )
        )
        await state.clear()
        return False

    await _render_invoice(
        message,
        db,
        db_user,
        state,
        state_data=state_data,
        routed=routed,
        amount_kopeks=amount_kopeks,
    )
    return True


async def _render_invoice(
    message: types.Message,
    db: AsyncSession,
    db_user: User,
    state: FSMContext,
    *,
    state_data: dict,
    routed,
    amount_kopeks: int,
) -> None:
    texts = get_texts(db_user.language)
    amount_label = settings.format_price(amount_kopeks)

    pay_button_text = texts.t(
        "PAYMENT_AUTO_PAY_BUTTON_WITH_AMOUNT",
        "\U0001f4b3 Оплатить – {amount}",
    ).format(amount=amount_label)

    back_callback = (
        state_data.get("invoice_back_callback")
        or state_data.get("platega_back_callback")
        or "balance_topup_reset"
    )
    tariff_summary = state_data.get("tariff_checkout_summary")

    rows = [[types.InlineKeyboardButton(text=pay_button_text, url=routed.payment_url)]]

    if isinstance(tariff_summary, dict):
        balance_callback = tariff_summary.get("balance_callback")
        if isinstance(balance_callback, str) and balance_callback:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            "TARIFF_BALANCE_PAY_BUTTON", "\U0001f4b0 Оплатить с баланса"
                        ),
                        callback_data=balance_callback,
                    )
                ]
            )
        rows.append(
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)]
        )
        instructions = texts.t(
            "TARIFF_PLATEGA_PAYMENT_INSTRUCTIONS",
            (
                "Стоимость услуги: {total}\n"
                "На балансе: {balance}\n"
                "Не хватает: {missing}\n\n"
                "\U0001f512 Защищённый платеж (Карта / СБП / Крипто)\n"
                "Обычно занимает до 10 секунд"
            ),
        ).format(
            total=settings.format_price(int(tariff_summary.get("total_kopeks") or 0)),
            balance=settings.format_price(
                int(tariff_summary.get("balance_kopeks") or 0)
            ),
            missing=settings.format_price(
                int(tariff_summary.get("missing_kopeks") or amount_kopeks)
            ),
        )
    else:
        rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=texts.t("CHECK_STATUS_BUTTON", "\U0001f4ca Проверить статус"),
                        callback_data=routed.check_callback,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t("SUPPORT_BUTTON", "\U0001f198 Поддержка"),
                        callback_data="menu_support",
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.BACK, callback_data=back_callback
                    )
                ],
            ]
        )
        instructions = texts.t(
            "PAYMENT_AUTO_INSTRUCTIONS",
            (
                "<b>Оплата — {amount}</b>\n"
                "Карта / СБП / Крипто\n\n"
                "\U0001f512 Защищённый платеж\n"
                "Обычно занимает до 10 секунд"
            ),
        ).format(amount=amount_label)

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=rows)

    # Универсальные ключи с фолбэком на platega_*: пользователи, оставшиеся
    # в старом состоянии после включения роутера, не должны видеть мусор.
    prompt_message_id = state_data.get("topup_prompt_message_id") or state_data.get(
        "platega_prompt_message_id"
    )
    prompt_chat_id = (
        state_data.get("topup_prompt_chat_id")
        or state_data.get("platega_prompt_chat_id")
        or message.chat.id
    )

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - зависит от прав бота
        logger.warning("Не удалось удалить сообщение с суммой: %s", delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - диагностический лог
            logger.warning(
                "Не удалось удалить сообщение с запросом суммы: %s", delete_error
            )

    pay_image_path = os.path.join("images", "pay.jpg")
    if os.path.exists(pay_image_path):
        invoice_message = await message.answer_photo(
            FSInputFile(pay_image_path),
            caption=instructions,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        invoice_message = await message.answer(
            instructions,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    extra_metadata = merge_vpn_deposit_bonus_metadata(
        {},
        db_user,
        state_data,
        amount_kopeks=amount_kopeks,
    )
    await payment_gateway_router.attach_invoice_message(
        db,
        routed,
        chat_id=invoice_message.chat.id,
        message_id=invoice_message.message_id,
        extra_metadata=extra_metadata or None,
    )

    await state.update_data(
        topup_invoice_message_id=invoice_message.message_id,
        topup_invoice_chat_id=invoice_message.chat.id,
        topup_invoice_gateway=routed.gateway,
    )

    from .main import clear_state_preserve_topup_amount

    await clear_state_preserve_topup_amount(state)
