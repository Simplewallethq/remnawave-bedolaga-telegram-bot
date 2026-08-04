"""Handlers for Platega balance interactions."""

import logging
from typing import List

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from .vpn_deposit_bonus import build_vpn_deposit_bonus_metadata, merge_vpn_deposit_bonus_metadata, should_bypass_minimum

logger = logging.getLogger(__name__)


def _get_active_methods() -> List[int]:
    methods = settings.get_platega_active_methods()
    return [code for code in methods if code in {2, 10, 11, 12, 13}]


def _is_sbp_method(method_code: int) -> bool:
    """Determine if the Platega method is SBP-based (QR code)."""
    title = settings.get_platega_method_display_title(method_code).lower()
    return "сбп" in title or "sbp" in title or "qr" in title or method_code == 2


def _get_method_button_label(method_code: int) -> str:
    """Get display label for Platega method selection button."""
    if _is_sbp_method(method_code):
        return "СБП по QR коду"
    return "Банковская карта"


def _get_payment_title(method_code: int) -> str:
    """Get payment screen title based on method type."""
    if _is_sbp_method(method_code):
        return "Оплата по СБП через QR код"
    return "Оплата по банковской карте"


def _get_pay_button_text(method_code: int) -> str:
    """Get pay button text based on method type."""
    if _is_sbp_method(method_code):
        return "💳 Оплатить по СБП"
    return "💳 Оплатить по банковской карте"


async def _prompt_amount(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    method_code: int,
) -> None:
    texts = get_texts(db_user.language)
    method_name = settings.get_platega_method_display_title(method_code)

    # Всегда фиксируем выбранный метод для последующей обработки
    await state.update_data(payment_method="platega", platega_method=method_code)

    data = await state.get_data()
    pending_amount = int(data.get("platega_pending_amount") or 0)

    if pending_amount > 0:
        # Если сумма уже известна (например, после быстрого выбора),
        # сразу создаём платеж и сбрасываем временное значение.
        await state.update_data(platega_pending_amount=None)

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await process_platega_payment_amount(
                message,
                db_user,
                db,
                pending_amount,
                state,
            )
        return

    min_amount_label = settings.format_price(settings.PLATEGA_MIN_AMOUNT_KOPEKS)
    max_amount_kopeks = settings.PLATEGA_MAX_AMOUNT_KOPEKS
    max_amount_label = (
        settings.format_price(max_amount_kopeks)
        if max_amount_kopeks and max_amount_kopeks > 0
        else ""
    )

    default_prompt_body = (
        "Введите сумму для пополнения от {min_amount} до {max_amount}.\n"
        if max_amount_kopeks and max_amount_kopeks > 0
        else "Введите сумму для пополнения от {min_amount}.\n"
    )

    prompt_template = texts.t(
        "PLATEGA_TOPUP_PROMPT",
        (
            "💳 <b>Оплата через Platega ({method_name})</b>\n\n"
            f"{default_prompt_body}"
            "Оплата происходит через Platega."
        ),
    )

    keyboard = get_back_keyboard(db_user.language)

    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        from .main import get_quick_amount_buttons

        quick_amount_buttons = get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await message.edit_text(
        prompt_template.format(
            method_name=method_name,
            min_amount=min_amount_label,
            max_amount=max_amount_label,
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        platega_prompt_message_id=message.message_id,
        platega_prompt_chat_id=message.chat.id,
    )


@error_handler
async def start_platega_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if not settings.is_platega_enabled():
        await callback.answer(
            texts.t(
                "PLATEGA_TEMPORARILY_UNAVAILABLE",
                "❌ Оплата через Platega временно недоступна",
            ),
            show_alert=True,
        )
        return

    active_methods = _get_active_methods()
    if not active_methods:
        await callback.answer(
            texts.t(
                "PLATEGA_METHODS_NOT_CONFIGURED",
                "⚠️ На стороне Platega нет доступных методов оплаты",
            ),
            show_alert=True,
        )
        return

    await state.update_data(payment_method="platega")
    data = await state.get_data()
    has_pending_amount = bool(int(data.get("platega_pending_amount") or 0))

    if len(active_methods) == 1:
        await _prompt_amount(callback.message, db_user, state, active_methods[0])
        await callback.answer()
        return

    method_buttons: list[list[types.InlineKeyboardButton]] = []
    for method_code in active_methods:
        label = _get_method_button_label(method_code)
        method_buttons.append(
            [
                types.InlineKeyboardButton(
                    text=label,
                    callback_data=f"platega_method_{method_code}",
                )
            ]
        )

    back_callback = data.get("platega_back_callback", "balance_topup")
    method_buttons.append(
        [types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)]
    )

    await callback.message.edit_text(
        texts.t(
            "PLATEGA_SELECT_PAYMENT_METHOD",
            "Выберите способ оплаты:",
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=method_buttons),
    )
    if not has_pending_amount:
        await state.set_state(BalanceStates.waiting_for_platega_method)
    await callback.answer()


@error_handler
async def handle_platega_method_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    try:
        method_code = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer(texts.t("PAYMENT_METHOD_INVALID"), show_alert=True)
        return

    if method_code not in _get_active_methods():
        await callback.answer(texts.t("PAYMENT_METHOD_UNAVAILABLE"), show_alert=True)
        return

    await _prompt_amount(callback.message, db_user, state, method_code)
    await callback.answer()


@error_handler
async def process_platega_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if not settings.is_platega_enabled():
        await message.answer(
            texts.t(
                "PLATEGA_TEMPORARILY_UNAVAILABLE",
                "❌ Оплата через Platega временно недоступна",
            )
        )
        return

    data = await state.get_data()
    method_code = int(data.get("platega_method", 0))
    if method_code not in _get_active_methods():
        await message.answer(
            texts.t(
                "PLATEGA_METHOD_SELECTION_REQUIRED",
                "⚠️ Выберите способ оплаты Platega перед вводом суммы",
            )
        )
        await state.set_state(BalanceStates.waiting_for_platega_method)
        return

    bypass_minimum = should_bypass_minimum(data, amount_kopeks)

    if amount_kopeks < settings.PLATEGA_MIN_AMOUNT_KOPEKS and not bypass_minimum:
        await message.answer(
            texts.t(
                "PLATEGA_AMOUNT_TOO_LOW",
                "Минимальная сумма для оплаты через Platega: {amount}",
            ).format(amount=settings.format_price(settings.PLATEGA_MIN_AMOUNT_KOPEKS))
        )
        return

    if amount_kopeks > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
        await message.answer(
            texts.t(
                "PLATEGA_AMOUNT_TOO_HIGH",
                "Максимальная сумма для оплаты через Platega: {amount}",
            ).format(amount=settings.format_price(settings.PLATEGA_MAX_AMOUNT_KOPEKS))
        )
        return

    try:
        payment_service = PaymentService(message.bot)
        payment_result = await payment_service.create_platega_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks),
            language=db_user.language,
            payment_method_code=method_code,
            metadata=build_vpn_deposit_bonus_metadata(db_user, data),
        )
    except Exception as error:
        logger.exception("Ошибка создания платежа Platega: %s", error)
        payment_result = None

    if not payment_result or not payment_result.get("redirect_url"):
        await message.answer(
            texts.t(
                "PLATEGA_PAYMENT_ERROR",
                "❌ Ошибка создания платежа Platega. Попробуйте позже или обратитесь в поддержку.",
            )
        )
        await state.clear()
        return

    redirect_url = payment_result.get("redirect_url")
    local_payment_id = payment_result.get("local_payment_id")
    transaction_id = payment_result.get("transaction_id")
    payment_title = _get_payment_title(method_code)
    pay_button_text = _get_pay_button_text(method_code)

    back_callback = data.get("platega_back_callback", "balance_topup")

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=pay_button_text,
                    url=redirect_url,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("CHECK_STATUS_BUTTON", "📊 Проверить статус"),
                    callback_data=f"check_platega_{local_payment_id}",
                )
            ],
            [types.InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)],
        ]
    )

    instructions_template = texts.t(
        "PLATEGA_PAYMENT_INSTRUCTIONS",
        (
            "<b>{title}</b>\n\n"
            "Сумма: {amount}\n\n"
            "Нажмите кнопку \"Оплатить\" и осуществите перевод. Средства зачислятся автоматически.\n\n"
            "Если возникнут проблемы, обратитесь в Поддержку"
        ),
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get("platega_prompt_message_id")
    prompt_chat_id = state_data.get("platega_prompt_chat_id", message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - зависит от прав бота
        logger.warning("Не удалось удалить сообщение с суммой Platega: %s", delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - диагностический лог
            logger.warning(
                "Не удалось удалить сообщение с запросом суммы Platega: %s",
                delete_error,
            )

    invoice_message = await message.answer(
        instructions_template.format(
            title=payment_title,
            amount=settings.format_price(amount_kopeks),
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    try:
        from app.services import payment_service as payment_module

        payment = await payment_module.get_platega_payment_by_id(db, local_payment_id)
        if payment:
            payment_metadata = dict(getattr(payment, "metadata_json", {}) or {})
            payment_metadata["invoice_message"] = {
                "chat_id": invoice_message.chat.id,
                "message_id": invoice_message.message_id,
            }
            payment_metadata = merge_vpn_deposit_bonus_metadata(
                payment_metadata,
                db_user,
                state_data,
            )
            await payment_module.update_platega_payment(
                db,
                payment=payment,
                metadata=payment_metadata,
            )
    except Exception as error:  # pragma: no cover - диагностический лог
        logger.warning("Не удалось сохранить данные сообщения Platega: %s", error)

    await state.update_data(
        platega_invoice_message_id=invoice_message.message_id,
        platega_invoice_chat_id=invoice_message.chat.id,
    )

    from .main import clear_state_preserve_topup_amount
    await clear_state_preserve_topup_amount(state)


async def _prompt_universal_amount(
    message: types.Message,
    db_user: User,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    platega_name = settings.get_platega_display_name()

    await state.update_data(payment_method="platega_universal", platega_method=None)

    data = await state.get_data()
    pending_amount = int(data.get("platega_pending_amount") or 0)

    if pending_amount > 0:
        await state.update_data(platega_pending_amount=None)

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await process_platega_universal_payment_amount(
                message,
                db_user,
                db,
                pending_amount,
                state,
            )
        return

    min_amount_label = settings.format_price(settings.PLATEGA_MIN_AMOUNT_KOPEKS)
    max_amount_kopeks = settings.PLATEGA_MAX_AMOUNT_KOPEKS
    max_amount_label = (
        settings.format_price(max_amount_kopeks)
        if max_amount_kopeks and max_amount_kopeks > 0
        else ""
    )

    default_prompt_body = (
        "Введите сумму для пополнения от {min_amount} до {max_amount}.\n"
        if max_amount_kopeks and max_amount_kopeks > 0
        else "Введите сумму для пополнения от {min_amount}.\n"
    )

    prompt_template = texts.t(
        "PLATEGA_UNIVERSAL_TOPUP_PROMPT",
        (
            "💳 <b>Оплата через {platega_name}</b>\n\n"
            f"{default_prompt_body}"
            "Способ оплаты выбирается на странице {platega_name}."
        ),
    )

    keyboard = get_back_keyboard(db_user.language)

    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        from .main import get_quick_amount_buttons

        quick_amount_buttons = get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await message.edit_text(
        prompt_template.format(
            platega_name=platega_name,
            min_amount=min_amount_label,
            max_amount=max_amount_label,
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        platega_prompt_message_id=message.message_id,
        platega_prompt_chat_id=message.chat.id,
    )


@error_handler
async def start_platega_universal_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if not settings.is_platega_universal_enabled():
        await callback.answer(
            texts.t(
                "PLATEGA_UNIVERSAL_TEMPORARILY_UNAVAILABLE",
                "❌ Универсальная оплата через Platega временно недоступна",
            ),
            show_alert=True,
        )
        return

    await _prompt_universal_amount(callback.message, db_user, state)
    await callback.answer()


@error_handler
async def process_platega_universal_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if not settings.is_platega_universal_enabled():
        await message.answer(
            texts.t(
                "PLATEGA_UNIVERSAL_TEMPORARILY_UNAVAILABLE",
                "❌ Универсальная оплата через Platega временно недоступна",
            )
        )
        return

    state_data = await state.get_data()
    bypass_minimum = should_bypass_minimum(state_data, amount_kopeks)

    if amount_kopeks < settings.PLATEGA_MIN_AMOUNT_KOPEKS and not bypass_minimum:
        await message.answer(
            texts.t(
                "PLATEGA_AMOUNT_TOO_LOW",
                "Минимальная сумма для оплаты через Platega: {amount}",
            ).format(amount=settings.format_price(settings.PLATEGA_MIN_AMOUNT_KOPEKS))
        )
        return

    if amount_kopeks > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
        await message.answer(
            texts.t(
                "PLATEGA_AMOUNT_TOO_HIGH",
                "Максимальная сумма для оплаты через Platega: {amount}",
            ).format(amount=settings.format_price(settings.PLATEGA_MAX_AMOUNT_KOPEKS))
        )
        return

    try:
        payment_service = PaymentService(message.bot)
        payment_result = await payment_service.create_platega_universal_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks),
            language=db_user.language,
            metadata=build_vpn_deposit_bonus_metadata(db_user, state_data),
        )
    except Exception as error:
        logger.exception("Ошибка создания универсального платежа Platega: %s", error)
        payment_result = None

    if not payment_result or not payment_result.get("redirect_url"):
        await message.answer(
            texts.t(
                "PLATEGA_PAYMENT_ERROR",
                "❌ Ошибка создания платежа Platega. Попробуйте позже или обратитесь в поддержку.",
            )
        )
        await state.clear()
        return

    redirect_url = payment_result.get("redirect_url")
    local_payment_id = payment_result.get("local_payment_id")
    amount_label = settings.format_price(amount_kopeks)
    payment_title = texts.t(
        "PLATEGA_UNIVERSAL_PAYMENT_TITLE",
        "Оплата через Platega",
    )
    pay_button_text = texts.t(
        "PLATEGA_UNIVERSAL_PAY_BUTTON_WITH_AMOUNT",
        "💳 Оплатить – {amount}",
    ).format(amount=amount_label)

    data = await state.get_data()
    back_callback = data.get("platega_back_callback", "balance_topup")

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=pay_button_text,
                    url=redirect_url,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t("CHECK_STATUS_BUTTON", "📊 Проверить статус"),
                    callback_data=f"check_platega_{local_payment_id}",
                )
            ],
            [types.InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)],
        ]
    )

    instructions_template = texts.t(
        "PLATEGA_UNIVERSAL_PAYMENT_INSTRUCTIONS",
        (
            "<b>Оплата подписки — {amount}</b>\n"
            "Карта / СБП / Крипто\n\n"
            "🔒 Защищённый платеж\n"
            "Обычно занимает до 10 секунд"
        ),
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get("platega_prompt_message_id")
    prompt_chat_id = state_data.get("platega_prompt_chat_id", message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - зависит от прав бота
        logger.warning("Не удалось удалить сообщение с суммой Platega: %s", delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - диагностический лог
            logger.warning(
                "Не удалось удалить сообщение с запросом суммы Platega: %s",
                delete_error,
            )

    invoice_message = await message.answer(
        instructions_template.format(
            title=payment_title,
            amount=amount_label,
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    try:
        from app.services import payment_service as payment_module

        payment = await payment_module.get_platega_payment_by_id(db, local_payment_id)
        if payment:
            payment_metadata = dict(getattr(payment, "metadata_json", {}) or {})
            payment_metadata["invoice_message"] = {
                "chat_id": invoice_message.chat.id,
                "message_id": invoice_message.message_id,
            }
            payment_metadata = merge_vpn_deposit_bonus_metadata(
                payment_metadata,
                db_user,
                state_data,
            )
            await payment_module.update_platega_payment(
                db,
                payment=payment,
                metadata=payment_metadata,
            )
    except Exception as error:  # pragma: no cover - диагностический лог
        logger.warning("Не удалось сохранить данные сообщения Platega: %s", error)

    await state.update_data(
        platega_invoice_message_id=invoice_message.message_id,
        platega_invoice_chat_id=invoice_message.chat.id,
    )

    from .main import clear_state_preserve_topup_amount
    await clear_state_preserve_topup_amount(state)


@error_handler
async def check_platega_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    try:
        local_payment_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("❌ Некорректный идентификатор платежа", show_alert=True)
        return

    payment_service = PaymentService(callback.bot)

    try:
        status_info = await payment_service.get_platega_payment_status(db, local_payment_id)
    except Exception as error:
        logger.exception("Ошибка проверки статуса Platega: %s", error)
        await callback.answer("⚠️ Ошибка проверки статуса", show_alert=True)
        return

    if not status_info:
        await callback.answer("⚠️ Платёж не найден", show_alert=True)
        return

    payment = status_info.get("payment")
    status = status_info.get("status")
    is_paid = status_info.get("is_paid")

    language = "ru"
    user = getattr(payment, "user", None)
    if user and getattr(user, "language", None):
        language = user.language

    texts = get_texts(language)

    if is_paid:
        await callback.answer(texts.t("PLATEGA_PAYMENT_ALREADY_CONFIRMED", "✅ Платёж уже зачислен"), show_alert=True)
    else:
        await callback.answer(
            texts.t("PLATEGA_PAYMENT_STATUS", "Текущий статус платежа: {status}").format(status=status),
            show_alert=True,
        )
