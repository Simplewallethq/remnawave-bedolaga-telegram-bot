import logging
import math
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.blacklist_service import blacklist_service
from app.services.payment_service import PaymentService
from app.utils.decorators import error_handler
from app.states import BalanceStates

logger = logging.getLogger(__name__)


@error_handler
async def start_cryptobot_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    texts = get_texts(db_user.language)
    
    if not settings.is_cryptobot_enabled():
        await callback.answer("❌ Оплата криптовалютой временно недоступна", show_alert=True)
        return
    
    from app.utils.currency_converter import currency_converter
    try:
        current_rate = await currency_converter.get_usd_to_rub_rate()
        rate_text = f"💱 Текущий курс: 1 USD = {current_rate:.2f} ₽"
    except Exception as e:
        logger.warning(f"Не удалось получить курс валют: {e}")
        current_rate = 95.0
        rate_text = f"💱 Курс: 1 USD ≈ {current_rate:.0f} ₽"
    
    available_assets = settings.get_cryptobot_assets()
    assets_text = ", ".join(available_assets)
    
    # Формируем текст сообщения в зависимости от настройки
    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        message_text = (
            f"🪙 <b>Пополнение криптовалютой</b>\n\n"
            f"Выберите сумму пополнения или введите вручную сумму "
            f"от {settings.CRYPTOBOT_MIN_AMOUNT} до 100,000 ₽:\n\n"
            f"💰 Доступные активы: {assets_text}\n"
            f"⚡ Мгновенное зачисление на баланс\n"
            f"🔒 Безопасная оплата через CryptoBot\n\n"
            f"{rate_text}\n"
            f"Сумма будет автоматически конвертирована в USD для оплаты."
        )
    else:
        message_text = (
            f"🪙 <b>Пополнение криптовалютой</b>\n\n"
            f"Введите сумму для пополнения от {settings.CRYPTOBOT_MIN_AMOUNT} до 100,000 ₽:\n\n"
            f"💰 Доступные активы: {assets_text}\n"
            f"⚡ Мгновенное зачисление на баланс\n"
            f"🔒 Безопасная оплата через CryptoBot\n\n"
            f"{rate_text}\n"
            f"Сумма будет автоматически конвертирована в USD для оплаты."
        )
    
    # Создаем клавиатуру
    keyboard = get_back_keyboard(db_user.language)
    
    # Если включен быстрый выбор суммы и не отключены кнопки, добавляем кнопки
    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        from .main import get_quick_amount_buttons
        quick_amount_buttons = get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            # Вставляем кнопки быстрого выбора перед кнопкой "Назад"
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard
    
    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method="cryptobot",
        current_rate=current_rate,
        cryptobot_prompt_message_id=callback.message.message_id,
        cryptobot_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_cryptobot_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
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

    if not settings.is_cryptobot_enabled():
        await message.answer("❌ Оплата криптовалютой временно недоступна")
        return
    
    amount_rubles = amount_kopeks / 100
    
    if amount_rubles < settings.CRYPTOBOT_MIN_AMOUNT:
        await message.answer(f"Минимальная сумма пополнения: {settings.CRYPTOBOT_MIN_AMOUNT} ₽")
        return
    
    if amount_rubles > 100000:
        await message.answer("Максимальная сумма пополнения: 100,000 ₽")
        return
    
    try:
        data = await state.get_data()
        current_rate = data.get('current_rate')
        
        if not current_rate:
            from app.utils.currency_converter import currency_converter
            current_rate = await currency_converter.get_usd_to_rub_rate()
        
        amount_usd = amount_rubles / current_rate
        
        amount_usd = math.ceil(amount_usd * 100) / 100
        
        if amount_usd < settings.CRYPTOBOT_MIN_AMOUNT_USD:
            await message.answer(f"❌ Минимальная сумма для оплаты в USD: {settings.CRYPTOBOT_MIN_AMOUNT_USD:.2f} USD")
            return
        
        if amount_usd > 1000:
            await message.answer("❌ Максимальная сумма для оплаты в USD: 1,000 USD")
            return
        
        payment_service = PaymentService(message.bot)
        
        payment_result = await payment_service.create_cryptobot_payment(
            db=db,
            user_id=db_user.id,
            amount_usd=amount_usd,
            asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            description=f"Пополнение баланса на {amount_rubles:.0f} ₽ ({amount_usd:.2f} USD)",
            payload=f"balance_{db_user.id}_{amount_kopeks}",
            amount_kopeks=amount_kopeks,
        )
        
        if not payment_result:
            await message.answer("❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.")
            await state.clear()
            return
        
        bot_invoice_url = payment_result.get("bot_invoice_url")
        mini_app_invoice_url = payment_result.get("mini_app_invoice_url")
        
        payment_url = bot_invoice_url or mini_app_invoice_url
        
        if not payment_url:
            await message.answer("❌ Ошибка получения ссылки для оплаты. Обратитесь в поддержку.")
            await state.clear()
            return
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🪙 Оплатить через Crypto Bot", url=payment_url)],
            [types.InlineKeyboardButton(text="📊 Проверить статус", callback_data=f"check_cryptobot_{payment_result['local_payment_id']}")],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data="balance_topup")]
        ])

        state_data = await state.get_data()
        prompt_message_id = state_data.get("cryptobot_prompt_message_id")
        prompt_chat_id = state_data.get("cryptobot_prompt_chat_id", message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - depends on bot rights
            logger.warning(
                "Не удалось удалить сообщение с суммой CryptoBot: %s",
                delete_error,
            )

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - diagnostics
                logger.warning(
                    "Не удалось удалить сообщение с запросом суммы CryptoBot: %s",
                    delete_error,
                )

        invoice_message = await message.answer(
            f"<b>Оплата через Crypto Bot</b>\n\n"
            f"Сумма: {amount_rubles:.0f} ₽ (~{amount_usd:.2f} USDT)\n\n"
            f"Нажмите кнопку ниже для оплаты криптовалютой.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        await state.update_data(
            cryptobot_invoice_message_id=invoice_message.message_id,
            cryptobot_invoice_chat_id=invoice_message.chat.id,
        )

        from .main import clear_state_preserve_topup_amount
        await clear_state_preserve_topup_amount(state)

        logger.info(f"Создан CryptoBot платеж для пользователя {db_user.telegram_id}: "
                   f"{amount_rubles:.0f} ₽ ({amount_usd:.2f} USD), ID: {payment_result['invoice_id']}")
        
    except Exception as e:
        logger.error(f"Ошибка создания CryptoBot платежа: {e}")
        await message.answer("❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.")
        await state.clear()


@error_handler
async def check_cryptobot_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession
):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
        
        from app.database.crud.cryptobot import get_cryptobot_payment_by_id
        payment = await get_cryptobot_payment_by_id(db, local_payment_id)
        
        if not payment:
            await callback.answer("❌ Платеж не найден", show_alert=True)
            return
        
        status_emoji = {
            "active": "⏳",
            "paid": "✅",
            "expired": "❌"
        }
        
        status_text = {
            "active": "Ожидает оплаты",
            "paid": "Оплачен",
            "expired": "Истек"
        }
        
        emoji = status_emoji.get(payment.status, "❓")
        status = status_text.get(payment.status, "Неизвестно")
        
        message_text = (f"🪙 Статус платежа:\n\n"
                       f"🆔 ID: {payment.invoice_id[:8]}...\n"
                       f"💰 Сумма: {payment.amount} {payment.asset}\n"
                       f"📊 Статус: {emoji} {status}\n"
                       f"📅 Создан: {payment.created_at.strftime('%d.%m.%Y %H:%M')}\n")
        
        if payment.is_paid:
            message_text += "\n✅ Платеж успешно завершен!\n\nСредства зачислены на баланс."
        elif payment.is_pending:
            message_text += "\n⏳ Платеж ожидает оплаты. Нажмите кнопку 'Оплатить' выше."
        elif payment.is_expired:
            message_text += (
                f"\n❌ Платеж истек. Обратитесь в {settings.get_support_contact_display()}"
            )
        
        await callback.answer(message_text, show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка проверки статуса CryptoBot платежа: {e}")
        await callback.answer("❌ Ошибка проверки статуса", show_alert=True)