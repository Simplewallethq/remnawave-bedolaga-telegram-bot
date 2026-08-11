import html
import logging
import os
from aiogram import Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.states import BalanceStates
from app.database.crud.user import add_user_balance
from app.utils.price_display import calculate_user_price, format_price_button
from app.utils.pricing_utils import format_period_description
from app.database.crud.transaction import (
    get_user_transactions, get_user_transactions_count,
    create_transaction
)
from app.database.models import User, TransactionType, PaymentMethod
from app.keyboards.inline import (
    get_balance_keyboard, get_balance_topup_payment_methods_keyboard,
    get_payment_methods_keyboard,
    get_back_keyboard, get_pagination_keyboard
)
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service
from app.utils.success_notifications import (
    build_success_management_keyboard,
    format_topup_success_message,
)
from app.utils.pagination import paginate_list
from app.utils.decorators import error_handler
from app.utils.photo_message import edit_or_answer_photo

logger = logging.getLogger(__name__)

TRANSACTIONS_PER_PAGE = 10


async def clear_state_preserve_topup_amount(state: FSMContext) -> None:
    """Clear FSM state but keep topup_amount_kopeks so the invoice's
    "back" button can return to the payment-method selection screen."""
    data = await state.get_data()
    topup_amount = data.get("topup_amount_kopeks")
    await state.clear()
    if topup_amount:
        await state.update_data(topup_amount_kopeks=topup_amount)


def get_quick_amount_buttons(language: str, user: User) -> list:
    """
    Generate quick amount buttons with user-specific pricing and discounts.

    Args:
        language: User's language for formatting
        user: User object to calculate personalized discounts

    Returns:
        List of button rows for inline keyboard
    """
    if not settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED or settings.DISABLE_TOPUP_BUTTONS:
        return []

    from app.localization.texts import get_texts
    texts = get_texts(language)

    buttons = []
    periods = settings.get_available_subscription_periods()
    periods = periods[:6]  # Limit to 6 periods

    for period in periods:
        price_attr = f"PRICE_{period}_DAYS"
        if hasattr(settings, price_attr):
            base_price_kopeks = getattr(settings, price_attr)

            # Calculate price with user's promo group discount using unified system
            price_info = calculate_user_price(user, base_price_kopeks, period, "period")

            callback_data = f"quick_amount_{price_info.final_price}"

            # Format button text with discount display
            period_label = f"{period} дней"

            # For balance buttons, use simpler format without emoji and period label prefix
            if price_info.has_discount:
                button_text = (
                    f"{texts.format_price(price_info.base_price)} ➜ "
                    f"{texts.format_price(price_info.final_price)} "
                    f"(-{price_info.discount_percent}%) • {period_label}"
                )
            else:
                button_text = f"{texts.format_price(price_info.final_price)} • {period_label}"

            buttons.append(
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=callback_data
                )
            )

    keyboard_rows = []
    for i in range(0, len(buttons), 2):
        keyboard_rows.append(buttons[i:i + 2])

    return keyboard_rows


@error_handler
async def show_balance_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    texts = get_texts(db_user.language)

    balance_text = texts.BALANCE_INFO.format(
        balance=texts.format_price(db_user.balance_kopeks)
    )

    reply_markup = get_balance_keyboard(db_user.language)

    try:
        if callback.message and callback.message.text:
            await callback.message.edit_text(
                balance_text,
                reply_markup=reply_markup
            )
        elif callback.message and callback.message.caption:
            await callback.message.edit_caption(
                balance_text,
                reply_markup=reply_markup
            )
        else:
            await callback.message.answer(
                balance_text,
                reply_markup=reply_markup
            )
    except TelegramBadRequest as error:
        logger.warning(
            "Failed to edit balance message, sending a new one instead: %s",
            error,
        )
        await callback.message.answer(
            balance_text,
            reply_markup=reply_markup
        )
    await callback.answer()


@error_handler
async def show_balance_history(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    page: int = 1
):
    texts = get_texts(db_user.language)
    
    offset = (page - 1) * TRANSACTIONS_PER_PAGE
    
    raw_transactions = await get_user_transactions(
        db, db_user.id, 
        limit=TRANSACTIONS_PER_PAGE * 3, 
        offset=offset
    )
    
    seen_transactions = set()
    unique_transactions = []
    
    for transaction in raw_transactions:
        rounded_time = transaction.created_at.replace(second=0, microsecond=0)
        transaction_key = (
            transaction.amount_kopeks,
            transaction.description,
            rounded_time
        )
        
        if transaction_key not in seen_transactions:
            seen_transactions.add(transaction_key)
            unique_transactions.append(transaction)
            
            if len(unique_transactions) >= TRANSACTIONS_PER_PAGE:
                break
    
    all_transactions = await get_user_transactions(db, db_user.id, limit=1000)
    seen_all = set()
    total_unique = 0
    
    for transaction in all_transactions:
        rounded_time = transaction.created_at.replace(second=0, microsecond=0)
        transaction_key = (
            transaction.amount_kopeks,
            transaction.description,
            rounded_time
        )
        if transaction_key not in seen_all:
            seen_all.add(transaction_key)
            total_unique += 1
    
    if not unique_transactions:
        await callback.message.edit_text(
            "📊 История операций пуста",
            reply_markup=get_back_keyboard(db_user.language)
        )
        await callback.answer()
        return
    
    text = "📊 <b>История операций</b>\n\n"
    
    for transaction in unique_transactions:
        emoji = "💰" if transaction.type == TransactionType.DEPOSIT.value else "💸"
        amount_text = f"+{texts.format_price(transaction.amount_kopeks)}" if transaction.type == TransactionType.DEPOSIT.value else f"-{texts.format_price(transaction.amount_kopeks)}"
        
        text += f"{emoji} {amount_text}\n"
        text += f"📝 {transaction.description}\n"
        text += f"📅 {transaction.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
    
    keyboard = []
    total_pages = (total_unique + TRANSACTIONS_PER_PAGE - 1) // TRANSACTIONS_PER_PAGE
    
    if total_pages > 1:
        pagination_row = get_pagination_keyboard(
            page, total_pages, "balance_history", db_user.language
        )
        keyboard.extend(pagination_row)
    
    keyboard.append([
        types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_balance")
    ])
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@error_handler
async def handle_balance_history_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    page = int(callback.data.split('_')[-1])
    await show_balance_history(callback, db_user, db, page)


@error_handler
async def show_payment_methods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    texts = get_texts(db_user.language)

    # If there's a stored amount from a previous entry, go straight to payment methods
    data = await state.get_data()
    stored_amount = data.get("topup_amount_kopeks")
    if stored_amount and stored_amount > 0:
        prompt = texts.t(
            "BALANCE_TOPUP_CHOOSE_METHOD_PROMPT",
            "💳 Выберите способ оплаты на сумму {amount}:",
        ).format(amount=texts.format_price(stored_amount))

        keyboard = get_balance_topup_payment_methods_keyboard(
            stored_amount, db_user.language
        )

        try:
            await callback.message.edit_text(
                prompt,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass
            await callback.message.answer(
                prompt,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        await callback.answer()
        return

    prompt_text = texts.t(
        "BALANCE_TOPUP_ENTER_AMOUNT_PROMPT",
        "💳 <b>Пополнение баланса</b>\n\nВведите сумму в рублях для пополнения баланса:",
    )

    back_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=texts.BACK, callback_data="subscription")]
    ])

    image_path = os.path.join("images", "pay.jpg")
    await edit_or_answer_photo(
        callback,
        prompt_text,
        back_keyboard,
        photo_path=image_path if os.path.exists(image_path) else None,
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method="select_after")
    await callback.answer()


@error_handler
async def handle_balance_topup_reset(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext
):
    await state.clear()
    await show_payment_methods(callback, db_user, db, state)


async def _render_payment_methods_with_amount(
    message: types.Message,
    db_user: User,
    amount_kopeks: int,
):
    texts = get_texts(db_user.language)

    prompt = texts.t(
        "BALANCE_TOPUP_CHOOSE_METHOD_PROMPT",
        "💳 Выберите способ оплаты на сумму {amount}:",
    ).format(amount=texts.format_price(amount_kopeks))

    keyboard = get_balance_topup_payment_methods_keyboard(
        amount_kopeks, db_user.language
    )

    try:
        if message.text:
            await message.edit_text(prompt, reply_markup=keyboard, parse_mode="HTML")
        elif message.caption:
            await message.edit_caption(prompt, reply_markup=keyboard, parse_mode="HTML")
        else:
            await message.answer(prompt, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest:
        await message.answer(prompt, reply_markup=keyboard, parse_mode="HTML")


@error_handler
async def handle_payment_methods_unavailable(
    callback: types.CallbackQuery,
    db_user: User
):
    texts = get_texts(db_user.language)
    
    await callback.answer(
        texts.t(
            "PAYMENT_METHODS_UNAVAILABLE_ALERT",
            "⚠️ В данный момент автоматические способы оплаты временно недоступны. Для пополнения баланса обратитесь в техподдержку.",
        ),
        show_alert=True
    )


@error_handler
async def show_vpn_deposit_bonus_payment_methods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    campaign_metadata = await vpn_deposit_bonus_service.get_payable_campaign_metadata(
        db,
        db_user.id,
    )
    if not campaign_metadata:
        await callback.answer("Предложение уже истекло", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        topup_amount_kopeks=vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS,
        topup_purpose=vpn_deposit_bonus_service.PURPOSE,
        vpn_deposit_bonus_metadata=campaign_metadata,
        payment_method="select_after",
    )

    texts = get_texts(db_user.language)
    prompt = (
        "💳 <b>Бонус 100₽ на баланс</b>\n\n"
        "Сумма к оплате: 10₽\n"
        "После успешной оплаты начислим 100₽ на баланс.\n\n"
        "Выберите способ оплаты:"
    )
    keyboard = get_payment_methods_keyboard(
        vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS,
        db_user.language,
        include_tribute=False,
    )

    try:
        await callback.message.edit_text(prompt, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(prompt, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@error_handler
async def handle_successful_topup_with_cart(
    user_id: int,
    amount_kopeks: int,
    bot,
    db: AsyncSession
):
    from app.database.crud.user import get_user_by_id
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from app.bot import dp
    
    user = await get_user_by_id(db, user_id)
    if not user:
        return
    
    storage = dp.storage
    key = StorageKey(bot_id=bot.id, chat_id=user.telegram_id, user_id=user.telegram_id)
    
    try:
        state_data = await storage.get_data(key)
        current_state = await storage.get_state(key)
        
        if (current_state == "SubscriptionStates:cart_saved_for_topup" and 
            state_data.get('saved_cart')):
            
            texts = get_texts(user.language)
            total_price = state_data.get('total_price', 0)
            
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="🛒 Вернуться к оформлению подписки", 
                    callback_data="return_to_saved_cart"
                )],
                [types.InlineKeyboardButton(
                    text="💰 Мой баланс", 
                    callback_data="menu_balance"
                )],
                [types.InlineKeyboardButton(
                    text="🏠 Главное меню", 
                    callback_data="back_to_menu"
                )]
            ])
            
            success_text = format_topup_success_message(texts.format_price(amount_kopeks))
            
            await bot.send_message(
                chat_id=user.telegram_id,
                text=success_text,
                reply_markup=build_success_management_keyboard(),
                parse_mode="HTML"
            )
            
    except Exception as e:
        logger.error(f"Ошибка обработки успешного пополнения с корзиной: {e}")


@error_handler
async def request_support_topup(
    callback: types.CallbackQuery,
    db_user: User
):
    texts = get_texts(db_user.language)

    if not settings.is_support_topup_enabled():
        await callback.answer(
            texts.t(
                "SUPPORT_TOPUP_DISABLED",
                "Пополнение через поддержку отключено. Попробуйте другой способ оплаты.",
            ),
            show_alert=True,
        )
        return

    support_text = f"""
🛠️ <b>Пополнение через поддержку</b>

Для пополнения баланса обратитесь в техподдержку:
{settings.get_support_contact_display_html()}

Укажите:
• ID: {db_user.telegram_id}
• Сумму пополнения
• Способ оплаты

⏰ Время обработки: 1-24 часа

<b>Доступные способы:</b>
• Криптовалюта
• Переводы между банками
• Другие платежные системы
"""
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="💬 Написать в поддержку",
            url=settings.get_support_contact_url() or "https://t.me/"
        )],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data="balance_topup")]
    ])
    
    await callback.message.edit_text(
        support_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@error_handler
async def process_topup_amount(
    message: types.Message,
    db_user: User,
    state: FSMContext
):
    texts = get_texts(db_user.language)

    try:
        if not message.text:
            await message.answer(
                texts.INVALID_AMOUNT,
                reply_markup=get_back_keyboard(db_user.language)
            )
            return

        amount_text = message.text.strip()
        if not amount_text:
            await message.answer(
                texts.INVALID_AMOUNT,
                reply_markup=get_back_keyboard(db_user.language)
            )
            return

        amount_rubles = float(amount_text.replace(',', '.'))

        if amount_rubles < 1:
            await message.answer("Минимальная сумма пополнения: 1 ₽")
            return
        
        if amount_rubles > 50000:
            await message.answer("Максимальная сумма пополнения: 50,000 ₽")
            return
        
        amount_kopeks = int(amount_rubles * 100)
        data = await state.get_data()
        payment_method = data.get("payment_method", "stars")
        
        is_vpn_bonus = data.get("topup_purpose") == vpn_deposit_bonus_service.PURPOSE

        if is_vpn_bonus and not vpn_deposit_bonus_service.is_campaign_amount(amount_kopeks):
            # Пользователь ввёл произвольную сумму, оставаясь в состоянии кампании:
            # это обычное пополнение, контекст кампании нужно убрать, чтобы метка
            # не уехала в счёт на другую сумму.
            logger.info(
                "Сброс состояния %s для пользователя %s: введена сумма %s копеек",
                vpn_deposit_bonus_service.PURPOSE,
                db_user.id,
                amount_kopeks,
            )
            await state.update_data(
                topup_purpose=None,
                vpn_deposit_bonus_metadata=None,
            )
            data = await state.get_data()
            is_vpn_bonus = False

        if payment_method in ["yookassa", "yookassa_sbp"] and not is_vpn_bonus:
            if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS:
                min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
                await message.answer(f"❌ Минимальная сумма для оплаты через YooKassa: {min_rubles:.0f} ₽")
                return
            
            if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
                max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
                await message.answer(f"❌ Максимальная сумма для оплаты через YooKassa: {max_rubles:,.0f} ₽".replace(',', ' '))
                return
        
        if payment_method == "select_after":
            from app.database.database import AsyncSessionLocal
            from app.services import payment_service as payment_module

            await state.clear()
            await state.update_data(topup_amount_kopeks=amount_kopeks)

            async with AsyncSessionLocal() as db:
                active_subscription = (
                    await payment_module.get_active_platega_subscription_for_user(
                        db, db_user.id
                    )
                )

                if active_subscription:
                    from .platega import process_platega_universal_payment_amount

                    await process_platega_universal_payment_amount(
                        message, db_user, db, amount_kopeks, state
                    )
                    return

            await _render_payment_methods_with_amount(message, db_user, amount_kopeks)
            return

        if payment_method == "stars":
            from .stars import process_stars_payment_amount
            await process_stars_payment_amount(message, db_user, amount_kopeks, state)
        elif payment_method == "yookassa":
            from app.database.database import AsyncSessionLocal
            from .yookassa import process_yookassa_payment_amount
            async with AsyncSessionLocal() as db:
                await process_yookassa_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "yookassa_sbp":
            from app.database.database import AsyncSessionLocal
            from .yookassa import process_yookassa_sbp_payment_amount
            async with AsyncSessionLocal() as db:
                await process_yookassa_sbp_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "mulenpay":
            from app.database.database import AsyncSessionLocal
            from .mulenpay import process_mulenpay_payment_amount
            async with AsyncSessionLocal() as db:
                await process_mulenpay_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "platega":
            from app.database.database import AsyncSessionLocal
            from .platega import process_platega_payment_amount

            async with AsyncSessionLocal() as db:
                await process_platega_payment_amount(
                    message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "platega_universal":
            from app.database.database import AsyncSessionLocal
            from .platega import process_platega_universal_payment_amount

            async with AsyncSessionLocal() as db:
                await process_platega_universal_payment_amount(
                    message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "wata":
            from app.database.database import AsyncSessionLocal
            from .wata import process_wata_payment_amount

            async with AsyncSessionLocal() as db:
                await process_wata_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "pal24":
            from app.database.database import AsyncSessionLocal
            from .pal24 import process_pal24_payment_amount
            async with AsyncSessionLocal() as db:
                await process_pal24_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "cryptobot":
            from app.database.database import AsyncSessionLocal
            from .cryptobot import process_cryptobot_payment_amount
            async with AsyncSessionLocal() as db:
                await process_cryptobot_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "heleket":
            from app.database.database import AsyncSessionLocal
            from .heleket import process_heleket_payment_amount
            async with AsyncSessionLocal() as db:
                await process_heleket_payment_amount(message, db_user, db, amount_kopeks, state)
        elif payment_method == "cloudpayments":
            from app.database.database import AsyncSessionLocal
            from .cloudpayments import process_cloudpayments_amount
            async with AsyncSessionLocal() as db:
                await process_cloudpayments_amount(message, db_user, db, state)
        else:
            await message.answer("Неизвестный способ оплаты")

    except ValueError:
        await message.answer(
            texts.INVALID_AMOUNT,
            reply_markup=get_back_keyboard(db_user.language)
        )


@error_handler
async def handle_sbp_payment(
    callback: types.CallbackQuery,
    db: AsyncSession
):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
        
        from app.database.crud.yookassa import get_yookassa_payment_by_local_id
        payment = await get_yookassa_payment_by_local_id(db, local_payment_id)
        
        if not payment:
            await callback.answer("❌ Платеж не найден", show_alert=True)
            return
        
        import json
        metadata = json.loads(payment.metadata_json) if payment.metadata_json else {}
        confirmation_token = metadata.get("confirmation_token")
        
        if not confirmation_token:
            await callback.answer("❌ Токен подтверждения не найден", show_alert=True)
            return
        
        await callback.message.answer(
            f"Для оплаты через СБП откройте приложение вашего банка и подтвердите платеж.\\n\\n"
            f"Если у вас не открылось банковское приложение автоматически, вы можете:\\n"
            f"1. Скопировать этот токен: <code>{confirmation_token}</code>\\n"
            f"2. Открыть приложение вашего банка\\n"
            f"3. Найти функцию оплаты по токену\\n"
            f"4. Вставить токен и подтвердить платеж",
            parse_mode="HTML"
        )
        
        await callback.answer("Информация об оплате отправлена", show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка обработки embedded платежа СБП: {e}")
        await callback.answer("❌ Ошибка обработки платежа", show_alert=True)


@error_handler
async def handle_quick_amount_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    """
    Обработчик выбора суммы через кнопки быстрого выбора
    """
    # Извлекаем сумму из callback_data
    try:
        amount_kopeks = int(callback.data.split('_')[-1])
        amount_rubles = amount_kopeks / 100
        
        # Получаем метод оплаты из состояния
        data = await state.get_data()
        payment_method = data.get("payment_method", "yookassa")
        
        # Проверяем, какой метод оплаты был выбран и вызываем соответствующий обработчик
        if payment_method == "yookassa":
            from app.database.database import AsyncSessionLocal
            from .yookassa import process_yookassa_payment_amount
            async with AsyncSessionLocal() as db:
                await process_yookassa_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "yookassa_sbp":
            from app.database.database import AsyncSessionLocal
            from .yookassa import process_yookassa_sbp_payment_amount
            async with AsyncSessionLocal() as db:
                await process_yookassa_sbp_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "mulenpay":
            from app.database.database import AsyncSessionLocal
            from .mulenpay import process_mulenpay_payment_amount
            async with AsyncSessionLocal() as db:
                await process_mulenpay_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "platega":
            from app.database.database import AsyncSessionLocal
            from .platega import process_platega_payment_amount

            async with AsyncSessionLocal() as db:
                await process_platega_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "platega_universal":
            from app.database.database import AsyncSessionLocal
            from .platega import process_platega_universal_payment_amount

            async with AsyncSessionLocal() as db:
                await process_platega_universal_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "wata":
            from app.database.database import AsyncSessionLocal
            from .wata import process_wata_payment_amount

            async with AsyncSessionLocal() as db:
                await process_wata_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "pal24":
            from app.database.database import AsyncSessionLocal
            from .pal24 import process_pal24_payment_amount
            async with AsyncSessionLocal() as db:
                await process_pal24_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "cryptobot":
            from app.database.database import AsyncSessionLocal
            from .cryptobot import process_cryptobot_payment_amount

            async with AsyncSessionLocal() as db:
                await process_cryptobot_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "heleket":
            from app.database.database import AsyncSessionLocal
            from .heleket import process_heleket_payment_amount

            async with AsyncSessionLocal() as db:
                await process_heleket_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "cloudpayments":
            from app.database.database import AsyncSessionLocal
            from .cloudpayments import process_cloudpayments_payment_amount

            async with AsyncSessionLocal() as db:
                await process_cloudpayments_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif payment_method == "stars":
            from .stars import process_stars_payment_amount

            await process_stars_payment_amount(
                callback.message, db_user, amount_kopeks, state
            )
        else:
            await callback.answer("❌ Неизвестный способ оплаты", show_alert=True)
            return

    except ValueError:
        await callback.answer("❌ Ошибка обработки суммы", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка обработки быстрого выбора суммы: {e}")
        await callback.answer("❌ Ошибка обработки запроса", show_alert=True)


@error_handler
async def handle_topup_amount_callback(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    try:
        _, method, amount_str = callback.data.split("|", 2)
        amount_kopeks = int(amount_str)
    except ValueError:
        await callback.answer("❌ Некорректный запрос", show_alert=True)
        return

    if amount_kopeks <= 0:
        await callback.answer("❌ Некорректная сумма", show_alert=True)
        return

    try:
        if method == "yookassa":
            from app.database.database import AsyncSessionLocal
            from .yookassa import process_yookassa_payment_amount
            async with AsyncSessionLocal() as db:
                await process_yookassa_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "yookassa_sbp":
            from app.database.database import AsyncSessionLocal
            from .yookassa import process_yookassa_sbp_payment_amount
            async with AsyncSessionLocal() as db:
                await process_yookassa_sbp_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "mulenpay":
            from app.database.database import AsyncSessionLocal
            from .mulenpay import process_mulenpay_payment_amount
            async with AsyncSessionLocal() as db:
                await process_mulenpay_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "platega":
            from app.database.database import AsyncSessionLocal
            from .platega import process_platega_payment_amount, start_platega_payment

            data = await state.get_data()
            method_code = int(data.get("platega_method", 0)) if data else 0

            if method_code > 0:
                async with AsyncSessionLocal() as db:
                    await process_platega_payment_amount(
                        callback.message, db_user, db, amount_kopeks, state
                    )
            else:
                await state.update_data(platega_pending_amount=amount_kopeks)
                await start_platega_payment(callback, db_user, state)
        elif method == "platega_universal":
            from .platega import start_platega_universal_payment

            await state.update_data(platega_pending_amount=amount_kopeks)
            await start_platega_universal_payment(callback, db_user, state)
        elif method == "platega_subscription":
            from app.database.database import AsyncSessionLocal
            from .platega import process_platega_subscription_amount

            async with AsyncSessionLocal() as db:
                await process_platega_subscription_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "pal24":
            from app.database.database import AsyncSessionLocal
            from .pal24 import process_pal24_payment_amount
            async with AsyncSessionLocal() as db:
                await process_pal24_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "cryptobot":
            from app.database.database import AsyncSessionLocal
            from .cryptobot import process_cryptobot_payment_amount
            async with AsyncSessionLocal() as db:
                await process_cryptobot_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "heleket":
            from app.database.database import AsyncSessionLocal
            from .heleket import process_heleket_payment_amount
            async with AsyncSessionLocal() as db:
                await process_heleket_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "wata":
            from app.database.database import AsyncSessionLocal
            from .wata import process_wata_payment_amount
            async with AsyncSessionLocal() as db:
                await process_wata_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "cloudpayments":
            from app.database.database import AsyncSessionLocal
            from .cloudpayments import process_cloudpayments_payment_amount
            async with AsyncSessionLocal() as db:
                await process_cloudpayments_payment_amount(
                    callback.message, db_user, db, amount_kopeks, state
                )
        elif method == "stars":
            from .stars import process_stars_payment_amount
            await process_stars_payment_amount(
                callback.message, db_user, amount_kopeks, state
            )
        elif method == "tribute":
            from .tribute import start_tribute_payment
            await start_tribute_payment(callback, db_user)
            return
        else:
            await callback.answer("❌ Неизвестный способ оплаты", show_alert=True)
            return

        await callback.answer()

    except Exception as error:
        logger.error(f"Ошибка быстрого пополнения: {error}")
        await callback.answer("❌ Ошибка обработки запроса", show_alert=True)


def register_balance_handlers(dp: Dispatcher):
    
    dp.callback_query.register(
        show_balance_menu,
        F.data == "menu_balance"
    )
    
    dp.callback_query.register(
        show_balance_history,
        F.data == "balance_history"
    )
    
    dp.callback_query.register(
        handle_balance_history_pagination,
        F.data.startswith("balance_history_page_")
    )
    
    dp.callback_query.register(
        show_payment_methods,
        F.data == "balance_topup"
    )

    dp.callback_query.register(
        handle_balance_topup_reset,
        F.data == "balance_topup_reset"
    )

    dp.callback_query.register(
        show_vpn_deposit_bonus_payment_methods,
        F.data == "10rub_vpn_deposit_bonus:pay",
    )
    
    from .stars import start_stars_payment
    dp.callback_query.register(
        start_stars_payment,
        F.data == "topup_stars"
    )
    
    from .yookassa import start_yookassa_payment
    dp.callback_query.register(
        start_yookassa_payment,
        F.data == "topup_yookassa"
    )
    
    from .yookassa import start_yookassa_sbp_payment
    dp.callback_query.register(
        start_yookassa_sbp_payment,
        F.data == "topup_yookassa_sbp"
    )

    from .mulenpay import start_mulenpay_payment
    dp.callback_query.register(
        start_mulenpay_payment,
        F.data == "topup_mulenpay"
    )

    from .wata import start_wata_payment
    dp.callback_query.register(
        start_wata_payment,
        F.data == "topup_wata"
    )

    from .pal24 import start_pal24_payment
    dp.callback_query.register(
        start_pal24_payment,
        F.data == "topup_pal24"
    )
    from .pal24 import handle_pal24_method_selection
    dp.callback_query.register(
        handle_pal24_method_selection,
        F.data.startswith("pal24_method_"),
    )

    from .platega import (
        start_platega_payment,
        handle_platega_method_selection,
        start_platega_universal_payment,
    )
    dp.callback_query.register(
        start_platega_payment,
        F.data == "topup_platega"
    )
    dp.callback_query.register(
        handle_platega_method_selection,
        F.data.startswith("platega_method_"),
    )
    dp.callback_query.register(
        start_platega_universal_payment,
        F.data == "topup_platega_universal",
    )

    from .yookassa import check_yookassa_payment_status
    dp.callback_query.register(
        check_yookassa_payment_status,
        F.data.startswith("check_yookassa_")
    )

    from .tribute import start_tribute_payment
    dp.callback_query.register(
        start_tribute_payment,
        F.data == "topup_tribute"
    )
    
    dp.callback_query.register(
        request_support_topup,
        F.data == "topup_support"
    )
    
    from .yookassa import check_yookassa_payment_status
    dp.callback_query.register(
        check_yookassa_payment_status,
        F.data.startswith("check_yookassa_")
    )
    
    dp.message.register(
        process_topup_amount,
        BalanceStates.waiting_for_amount,
        F.successful_payment.is_(None),
    )

    from .cryptobot import start_cryptobot_payment
    dp.callback_query.register(
        start_cryptobot_payment,
        F.data == "topup_cryptobot"
    )
    
    from .cryptobot import check_cryptobot_payment_status
    dp.callback_query.register(
        check_cryptobot_payment_status,
        F.data.startswith("check_cryptobot_")
    )

    from .heleket import start_heleket_payment, check_heleket_payment_status
    dp.callback_query.register(
        start_heleket_payment,
        F.data == "topup_heleket"
    )
    dp.callback_query.register(
        check_heleket_payment_status,
        F.data.startswith("check_heleket_")
    )

    from .cloudpayments import start_cloudpayments_payment, handle_cloudpayments_quick_amount
    dp.callback_query.register(
        start_cloudpayments_payment,
        F.data == "topup_cloudpayments"
    )
    dp.callback_query.register(
        handle_cloudpayments_quick_amount,
        F.data.startswith("topup_amount|cloudpayments|")
    )

    from .mulenpay import check_mulenpay_payment_status
    dp.callback_query.register(
        check_mulenpay_payment_status,
        F.data.startswith("check_mulenpay_")
    )

    from .wata import check_wata_payment_status
    dp.callback_query.register(
        check_wata_payment_status,
        F.data.startswith("check_wata_")
    )

    from .pal24 import check_pal24_payment_status
    dp.callback_query.register(
        check_pal24_payment_status,
        F.data.startswith("check_pal24_")
    )

    from .platega import check_platega_payment_status
    dp.callback_query.register(
        check_platega_payment_status,
        F.data.startswith("check_platega_")
    )

    dp.callback_query.register(
        handle_payment_methods_unavailable,
        F.data == "payment_methods_unavailable"
    )
    
    # Регистрируем обработчик для кнопок быстрого выбора суммы
    dp.callback_query.register(
        handle_quick_amount_selection,
        F.data.startswith("quick_amount_")
    )

    dp.callback_query.register(
        handle_topup_amount_callback,
        F.data.startswith("topup_amount|")
    )
