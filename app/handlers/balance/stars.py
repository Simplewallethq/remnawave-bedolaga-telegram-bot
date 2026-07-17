import logging
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.blacklist_service import blacklist_service
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.photo_message import edit_or_answer_photo
from app.external.telegram_stars import TelegramStarsService

logger = logging.getLogger(__name__)


@error_handler
async def start_stars_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await callback.answer("❌ Пополнение через Stars временно недоступно", show_alert=True)
        return

    # Формируем текст сообщения в зависимости от настройки
    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        message_text = (
            f"⭐ <b>Пополнение через Telegram Stars</b>\n\n"
            f"Выберите сумму пополнения или введите вручную:"
        )
    else:
        message_text = texts.TOP_UP_AMOUNT

    # Создаем клавиатуру
    keyboard = get_back_keyboard(db_user.language)

    # Если включен быстрый выбор суммы и не отключены кнопки, добавляем кнопки
    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        from .main import get_quick_amount_buttons
        quick_amount_buttons = get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            # Вставляем кнопки быстрого выбора перед кнопкой "Назад"
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await edit_or_answer_photo(
        callback,
        message_text,
        keyboard,
        parse_mode="HTML",
    )

    await state.update_data(
        stars_prompt_message_id=callback.message.message_id,
        stars_prompt_chat_id=callback.message.chat.id,
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method="stars")
    await callback.answer()


@error_handler
async def process_stars_payment_amount(
    message: types.Message,
    db_user: User,
    amount_kopeks: int,
    state: FSMContext
):
    # Проверяем, находится ли пользователь в черном списке
    is_blacklisted, blacklist_reason = await blacklist_service.is_user_blacklisted(
        message.from_user.id,
        message.from_user.username
    )

    if is_blacklisted:
        logger.warning(f"🚫 Пользователь {message.from_user.id} находится в черном списке: {blacklist_reason}")
        try:
            await message.answer(
                f"🚫 Оплата невозможна\n\n"
                f"Причина: {blacklist_reason}\n\n"
                f"Если вы считаете, что это ошибка, обратитесь в поддержку."
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения о блокировке: {e}")
        return

    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await message.answer("⚠️ Оплата Stars временно недоступна")
        return

    try:
        amount_rubles = amount_kopeks / 100
        stars_amount = TelegramStarsService.calculate_stars_from_rubles(amount_rubles)
        stars_rate = settings.get_stars_rate()

        payment_service = PaymentService(message.bot)

        # Частичная оплата тарифа: фиксируем разбивку и уносим токен в payload
        # инвойса (у Stars нет своей записи платежа с metadata).
        from app.services.tariff_partial_payment_service import (
            build_invoice_checkout_snapshot,
            stash_snapshot_for_stars,
        )

        stars_payload = f"balance_{db_user.id}_{amount_kopeks}"
        snapshot = await build_invoice_checkout_snapshot(db_user.id, amount_kopeks)
        if snapshot:
            token = await stash_snapshot_for_stars(db_user.id, snapshot)
            if token:
                stars_payload += f"_ts{token}"

        invoice_link = await payment_service.create_stars_invoice(
            amount_kopeks=amount_kopeks,
            description=f"Пополнение баланса на {texts.format_price(amount_kopeks)}",
            payload=stars_payload
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f"⭐ Оплатить ({stars_amount} Stars)", url=invoice_link)],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data="balance_topup")]
        ])

        state_data = await state.get_data()

        prompt_message_id = state_data.get("stars_prompt_message_id")
        prompt_chat_id = state_data.get("stars_prompt_chat_id", message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning("Не удалось удалить сообщение с суммой Stars: %s", delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning(
                    "Не удалось удалить сообщение с запросом суммы Stars: %s",
                    delete_error,
                )

        invoice_caption = (
            f"<b>Оплата через Telegram Stars</b>\n\n"
            f"Сумма: {texts.format_price(amount_kopeks)}\n"
            f"К оплате: {stars_amount} Stars\n\n"
            f"Нажмите кнопку ниже для оплаты:"
        )

        if settings.ENABLE_LOGO_MODE:
            from app.utils.bot_registry import get_logo_for_bot
            logo_path = get_logo_for_bot(message.bot.id if message.bot else None)
            invoice_message = await message.answer_photo(
                photo=FSInputFile(logo_path),
                caption=invoice_caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            invoice_message = await message.answer(
                invoice_caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        await state.update_data(
            stars_invoice_message_id=invoice_message.message_id,
            stars_invoice_chat_id=invoice_message.chat.id,
        )

        await state.set_state(None)

    except Exception as e:
        logger.error(f"Ошибка создания Stars invoice: {e}")
        await message.answer("⚠️ Ошибка создания платежа")
