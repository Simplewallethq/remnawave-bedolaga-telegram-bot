"""Handlers for Platega balance interactions."""

import logging
import os
from datetime import datetime
from typing import List

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import (
    can_use_platega_subscription,
    get_back_keyboard,
    get_platega_autopay_keyboard,
)
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.photo_message import edit_or_answer_photo
from .vpn_deposit_bonus import build_vpn_deposit_bonus_metadata, merge_vpn_deposit_bonus_metadata, should_bypass_minimum

logger = logging.getLogger(__name__)

PLATEGA_SUBSCRIPTION_MANAGEMENT_ORIGIN = "management"


async def show_platega_autopay_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    *,
    answer_callback: bool = True,
) -> None:
    """Show and manage the user's recurring Platega balance top-up."""
    from app.services import payment_service as payment_module

    texts = get_texts(db_user.language)
    subscription = await payment_module.get_active_platega_subscription_for_user(
        db, db_user.id
    )
    amount = (
        texts.t("PLATEGA_AUTOPAY_CURRENT_AMOUNT", "{amount}/мес").format(
            amount=settings.format_price(subscription.amount_kopeks)
        )
        if subscription
        else texts.t("PLATEGA_AUTOPAY_NOT_CONFIGURED", "не настроен")
    )
    text = texts.t(
        "PLATEGA_AUTOPAY_MENU_TEXT",
        "Здесь вы можете настроить автоплатеж чтобы всегда оставаться на связи.\n\n"
        "Текущий автоплатеж: {amount}",
    ).format(amount=amount)
    await edit_or_answer_photo(
        callback,
        text,
        get_platega_autopay_keyboard(
            db_user.language,
            has_active_subscription=subscription is not None,
            can_connect=(
                settings.is_platega_enabled()
                and can_use_platega_subscription(db_user.username)
            ),
        ),
        photo_path="images/pay.webp" if os.path.exists("images/pay.webp") else None,
    )
    if answer_callback:
        await callback.answer()


async def start_platega_autopay_setup(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    if not (
        settings.is_platega_enabled()
        and can_use_platega_subscription(db_user.username)
    ):
        await callback.answer(
            texts.t(
                "PLATEGA_TEMPORARILY_UNAVAILABLE",
                "❌ Оплата через Platega временно недоступна",
            ),
            show_alert=True,
        )
        return

    await edit_or_answer_photo(
        callback,
        texts.t(
            "PLATEGA_AUTOPAY_AMOUNT_PROMPT",
            "💳 <b>Подключение автоплатежа</b>\n\n"
            "Введите сумму в рублях для регулярного пополнения:",
        ),
        types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.BACK,
                        callback_data="subscription_platega_autopay",
                    )
                ]
            ]
        ),
        photo_path="images/pay.webp" if os.path.exists("images/pay.webp") else None,
    )
    await state.clear()
    await state.update_data(
        payment_method="platega_subscription",
        platega_subscription_origin=PLATEGA_SUBSCRIPTION_MANAGEMENT_ORIGIN,
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await callback.answer()


async def request_platega_autopay_cancellation(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    from app.services import payment_service as payment_module

    subscription = await payment_module.get_active_platega_subscription_for_user(
        db, db_user.id
    )
    texts = get_texts(db_user.language)
    if not subscription:
        await callback.answer(
            texts.t("PLATEGA_SUBSCRIPTION_NOT_FOUND", "Регулярные платежи не найдены."),
            show_alert=True,
        )
        return

    await edit_or_answer_photo(
        callback,
        texts.t(
            "PLATEGA_SUBSCRIPTION_CANCEL_CONFIRM",
            "Вы уверены, что хотите отменить регулярные платежи?",
        ),
        types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t("CONFIRM", "✅ Подтвердить"),
                        callback_data=(
                            "subscription_platega_autopay_confirm_cancel:"
                            f"{subscription.id}"
                        ),
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
        photo_path="images/pay.webp" if os.path.exists("images/pay.webp") else None,
    )
    await callback.answer()


async def confirm_platega_autopay_cancellation(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    try:
        subscription_id = int(callback.data.rsplit(":", 1)[-1])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    texts = get_texts(db_user.language)
    result = await PaymentService(callback.bot).cancel_platega_subscription(
        db, subscription_id=subscription_id, user_id=db_user.id
    )
    if not result:
        await callback.answer(
            texts.t(
                "PLATEGA_SUBSCRIPTION_CANCEL_ERROR",
                "Не удалось отменить регулярные платежи. Попробуйте позже.",
            ),
            show_alert=True,
        )
        return

    await show_platega_autopay_menu(
        callback, db_user, db, answer_callback=False
    )
    await callback.answer(
        texts.t("PLATEGA_SUBSCRIPTION_CANCELLED", "Регулярные платежи отменены."),
        show_alert=True,
    )


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
    try:
        method_code = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer("❌ Некорректный способ оплаты", show_alert=True)
        return

    if method_code not in _get_active_methods():
        await callback.answer("⚠️ Этот способ сейчас недоступен", show_alert=True)
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
            metadata=build_vpn_deposit_bonus_metadata(
                db_user,
                data,
                amount_kopeks=amount_kopeks,
            ),
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
                amount_kopeks=amount_kopeks,
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
async def process_platega_subscription_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)

    if not settings.is_platega_enabled():
        await message.answer(
            texts.t(
                "PLATEGA_TEMPORARILY_UNAVAILABLE",
                "❌ Оплата через Platega временно недоступна",
            )
        )
        return

    if amount_kopeks % 100:
        await message.answer(
            texts.t(
                "PLATEGA_SUBSCRIPTION_WHOLE_RUBLES",
                "Для регулярных платежей укажите сумму в целых рублях.",
            )
        )
        return

    if amount_kopeks < settings.PLATEGA_MIN_AMOUNT_KOPEKS:
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

    data = await state.get_data()
    payment_service = PaymentService(message.bot)
    try:
        result = await payment_service.create_platega_subscription(
            db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks),
        )
    except Exception as error:
        logger.exception("Ошибка создания регулярной подписки Platega: %s", error)
        result = None

    if result and result.get("already_exists"):
        await message.answer(
            texts.t(
                "PLATEGA_SUBSCRIPTION_ALREADY_EXISTS",
                "Регулярные платежи уже подключены. Отменить их можно в разделе автоплатежа.",
            )
        )
        await state.clear()
        return

    redirect_url = (result or {}).get("redirect_url")
    if not redirect_url:
        await message.answer(
            texts.t(
                "PLATEGA_SUBSCRIPTION_CREATE_ERROR",
                "❌ Не удалось подключить регулярные платежи. Попробуйте позже.",
            )
        )
        await state.clear()
        return

    try:
        await message.delete()
    except Exception as error:  # pragma: no cover - depends on bot rights
        logger.warning("Не удалось удалить сообщение с суммой регулярного платежа: %s", error)

    amount_rubles = amount_kopeks // 100
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ " + texts.t(
                        "PLATEGA_SUBSCRIPTION_PAY_BUTTON",
                        "Автоплатеж — {amount} руб/мес",
                    ).format(amount=amount_rubles),
                    url=redirect_url,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.CANCEL,
                    callback_data=(
                        f"cancel_platega_subscription:{result['subscription'].id}:"
                        f"{PLATEGA_SUBSCRIPTION_MANAGEMENT_ORIGIN}"
                        if data.get("platega_subscription_origin")
                        == PLATEGA_SUBSCRIPTION_MANAGEMENT_ORIGIN
                        else f"cancel_platega_subscription:{result['subscription'].id}"
                    ),
                )
            ],
        ]
    )
    instructions = texts.t(
        "PLATEGA_SUBSCRIPTION_INSTRUCTIONS",
        "Чтобы подключить автоплатеж нажми на кнопку и соверши платеж.\n\n"
        "Баланс пополняется после каждого успешного ежемесячного списания.",
    )
    pay_image_path = os.path.join("images", "pay.webp")
    if os.path.exists(pay_image_path):
        await message.answer_photo(
            FSInputFile(pay_image_path),
            caption=instructions,
            reply_markup=keyboard,
        )
    else:
        await message.answer(instructions, reply_markup=keyboard)
    await state.clear()


@error_handler
async def cancel_pending_platega_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Cancel an unconfirmed recurring-payment setup and return to its payment choices."""
    try:
        parts = callback.data.split(":", 2)
        _, subscription_id_text = parts[:2]
        origin = parts[2] if len(parts) == 3 else "balance"
        subscription_id = int(subscription_id_text)
    except (AttributeError, IndexError, ValueError):
        await callback.answer("❌ Некорректная подписка", show_alert=True)
        return

    from app.services import payment_service as payment_module

    subscription = await payment_module.get_platega_subscription_by_id_for_update(
        db, subscription_id
    )
    if not subscription or subscription.user_id != db_user.id:
        await callback.answer("⚠️ Регулярные платежи не найдены", show_alert=True)
        return

    amount_kopeks = subscription.amount_kopeks
    provider_subscription_id = subscription.platega_subscription_id
    if subscription.status != "SUBSCRIPTION_CANCELLED":
        await payment_module.update_platega_subscription(
            db,
            subscription=subscription,
            status="SUBSCRIPTION_CANCELLED",
            cancelled_at=datetime.utcnow(),
            set_cancelled_at=True,
            active_user_id=None,
            set_active_user_id=True,
        )

    await state.clear()
    payment_service = PaymentService(callback.bot)
    try:
        service = getattr(payment_service, "platega_service", None)
        if service and service.is_configured:
            await service.cancel_subscription(provider_subscription_id)
    except Exception as error:  # pragma: no cover - network errors
        logger.warning(
            "Не удалось отменить регулярную подписку Platega %s: %s",
            provider_subscription_id,
            error,
        )

    if origin == PLATEGA_SUBSCRIPTION_MANAGEMENT_ORIGIN:
        await show_platega_autopay_menu(callback, db_user, db)
        return

    # The user can immediately choose another method even if Platega is unavailable.
    await state.update_data(topup_amount_kopeks=amount_kopeks)

    from .main import _render_payment_methods_with_amount

    await _render_payment_methods_with_amount(
        callback.message, db_user, amount_kopeks
    )
    await callback.answer()


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
            metadata=build_vpn_deposit_bonus_metadata(
                db_user,
                state_data,
                amount_kopeks=amount_kopeks,
            ),
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

    state_data = await state.get_data()
    back_callback = state_data.get("platega_back_callback", "balance_topup_reset")
    tariff_summary = state_data.get("tariff_checkout_summary")

    rows = [[types.InlineKeyboardButton(text=pay_button_text, url=redirect_url)]]
    if isinstance(tariff_summary, dict):
        balance_callback = tariff_summary.get("balance_callback")
        if isinstance(balance_callback, str) and balance_callback:
            rows.append([
                types.InlineKeyboardButton(
                    text=texts.t("TARIFF_BALANCE_PAY_BUTTON", "💰 Оплатить с баланса"),
                    callback_data=balance_callback,
                )
            ])
        rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])
        instructions_template = texts.t(
            "TARIFF_PLATEGA_PAYMENT_INSTRUCTIONS",
            (
                "Стоимость услуги: {total}\n"
                "На балансе: {balance}\n"
                "Не хватает: {missing}\n\n"
                "🔒 Защищённый платеж (Карта / СБП / Крипто)\n"
                "Обычно занимает до 10 секунд"
            ),
        )
        instructions = instructions_template.format(
            total=settings.format_price(int(tariff_summary.get("total_kopeks") or 0)),
            balance=settings.format_price(int(tariff_summary.get("balance_kopeks") or 0)),
            missing=settings.format_price(int(tariff_summary.get("missing_kopeks") or amount_kopeks)),
        )
    else:
        rows.extend([
            [
                types.InlineKeyboardButton(
                    text=texts.t("CHECK_STATUS_BUTTON", "📊 Проверить статус"),
                    callback_data=f"check_platega_{local_payment_id}",
                )
            ],
            [types.InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)],
        ])
        instructions = texts.t(
            "PLATEGA_UNIVERSAL_PAYMENT_INSTRUCTIONS",
            (
                "<b>Оплата подписки — {amount}</b>\n"
                "Карта / СБП / Крипто\n\n"
                "🔒 Защищённый платеж\n"
                "Обычно занимает до 10 секунд"
            ),
        ).format(title=payment_title, amount=amount_label)

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=rows)
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

    pay_image_path = os.path.join("images", "pay.webp")
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
                amount_kopeks=amount_kopeks,
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
