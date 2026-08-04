import logging
from datetime import datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.localization.language import resolve_telegram_language
from app.services.blacklist_service import blacklist_service
from app.services.payment_service import PaymentService
from app.utils.decorators import error_handler
from app.states import BalanceStates
from .vpn_deposit_bonus import merge_vpn_deposit_bonus_metadata, should_bypass_minimum

logger = logging.getLogger(__name__)


@error_handler
async def start_yookassa_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    texts = get_texts(db_user.language)
    
    if not settings.is_yookassa_enabled():
        await callback.answer(texts.t("PAYMENT_UNAVAILABLE_YOOKASSA"), show_alert=True)
        return
    
    min_amount_rub = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
    max_amount_rub = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
    
    # Формируем текст сообщения в зависимости от настройки
    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        message_text = (
            f"💳 <b>Оплата банковской картой</b>\n\n"
            f"Выберите сумму пополнения или введите вручную сумму "
            f"от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:"
        )
    else:
        message_text = (
            f"💳 <b>Оплата банковской картой</b>\n\n"
            f"Введите сумму для пополнения от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:"
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
    await state.update_data(payment_method="yookassa")
    await state.update_data(
        yookassa_prompt_message_id=callback.message.message_id,
        yookassa_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def start_yookassa_sbp_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    texts = get_texts(db_user.language)
    
    if not settings.is_yookassa_enabled() or not settings.YOOKASSA_SBP_ENABLED:
        await callback.answer(texts.t("PAYMENT_UNAVAILABLE_SBP"), show_alert=True)
        return
    
    min_amount_rub = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
    max_amount_rub = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
    
    # Формируем текст сообщения в зависимости от настройки
    if settings.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not settings.DISABLE_TOPUP_BUTTONS:
        message_text = (
            f"🏦 <b>Оплата через СБП</b>\n\n"
            f"Выберите сумму пополнения или введите вручную сумму "
            f"от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:"
        )
    else:
        message_text = (
            f"🏦 <b>Оплата через СБП</b>\n\n"
            f"Введите сумму для пополнения от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:"
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
    await state.update_data(payment_method="yookassa_sbp")
    await state.update_data(
        yookassa_prompt_message_id=callback.message.message_id,
        yookassa_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_yookassa_payment_amount(
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
                get_texts(db_user.language).t("PAYMENT_BLOCKED").format(
                    reason=blacklist_reason,
                )
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения о блокировке: {e}")
        return

    texts = get_texts(db_user.language)
    state_data = await state.get_data()
    bypass_minimum = should_bypass_minimum(state_data, amount_kopeks)

    if not settings.is_yookassa_enabled():
        await message.answer(texts.t("PAYMENT_UNAVAILABLE_YOOKASSA"))
        return
    
    if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS and not bypass_minimum:
        min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t("PAYMENT_MIN_AMOUNT").format(amount=f"{min_rubles:.0f} ₽")
        )
        return
    
    if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
        max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t("PAYMENT_MAX_AMOUNT").format(
                amount=f"{max_rubles:,.0f} ₽".replace(",", " ")
            )
        )
        return
    
    try:
        payment_service = PaymentService(message.bot)
        
        payment_result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            receipt_email=None,
            receipt_phone=None,
            metadata=merge_vpn_deposit_bonus_metadata({
                "user_telegram_id": str(db_user.telegram_id),
                "user_username": db_user.username or "",
                "purpose": "balance_topup"
            }, db_user, state_data)
        )
        
        if not payment_result:
            await message.answer(texts.t("PAYMENT_CREATE_ERROR"))
            await state.clear()
            return
        
        confirmation_url = payment_result.get("confirmation_url")
        if not confirmation_url:
            await message.answer(texts.t("PAYMENT_LINK_ERROR"))
            await state.clear()
            return
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t("PAYMENT_PAY_BY_CARD"), url=confirmation_url)],
            [types.InlineKeyboardButton(text=texts.t("PAYMENT_CHECK_STATUS"), callback_data=f"check_yookassa_{payment_result['local_payment_id']}")],
            [types.InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data="balance_topup")]
        ])
        
        state_data = await state.get_data()
        prompt_message_id = state_data.get("yookassa_prompt_message_id")
        prompt_chat_id = state_data.get("yookassa_prompt_chat_id", message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning("Не удалось удалить сообщение с суммой YooKassa: %s", delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning(
                    "Не удалось удалить сообщение с запросом суммы YooKassa: %s",
                    delete_error,
                )

        invoice_message = await message.answer(
            texts.t("PAYMENT_YOOKASSA_CARD_INSTRUCTIONS").format(
                amount=settings.format_price(amount_kopeks),
                payment_id=payment_result["yookassa_payment_id"][:8],
            ),
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        try:
            from app.services import payment_service as payment_module

            payment = await payment_module.get_yookassa_payment_by_local_id(
                db, payment_result["local_payment_id"]
            )
            if payment:
                metadata = dict(getattr(payment, "metadata_json", {}) or {})
                metadata["invoice_message"] = {
                    "chat_id": invoice_message.chat.id,
                    "message_id": invoice_message.message_id,
                }
                await db.execute(
                    update(payment.__class__)
                    .where(payment.__class__.id == payment.id)
                    .values(metadata_json=metadata, updated_at=datetime.utcnow())
                )
                await db.commit()
        except Exception as error:  # pragma: no cover - диагностический лог
            logger.warning("Не удалось сохранить сообщение YooKassa: %s", error)

        await state.update_data(
            yookassa_invoice_message_id=invoice_message.message_id,
            yookassa_invoice_chat_id=invoice_message.chat.id,
        )

        from .main import clear_state_preserve_topup_amount
        await clear_state_preserve_topup_amount(state)
        logger.info(f"Создан платеж YooKassa для пользователя {db_user.telegram_id}: "
                   f"{amount_kopeks//100}₽, ID: {payment_result['yookassa_payment_id']}")
        
    except Exception as e:
        logger.error(f"Ошибка создания YooKassa платежа: {e}")
        await message.answer(texts.t("PAYMENT_CREATE_ERROR"))
        await state.clear()


@error_handler
async def process_yookassa_sbp_payment_amount(
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
                get_texts(db_user.language).t("PAYMENT_BLOCKED").format(
                    reason=blacklist_reason,
                )
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения о блокировке: {e}")
        return

    texts = get_texts(db_user.language)
    state_data = await state.get_data()
    bypass_minimum = should_bypass_minimum(state_data, amount_kopeks)

    if not settings.is_yookassa_enabled() or not settings.YOOKASSA_SBP_ENABLED:
        await message.answer(texts.t("PAYMENT_SBP_UNAVAILABLE"))
        return
    
    if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS and not bypass_minimum:
        min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t("PAYMENT_MIN_AMOUNT").format(amount=f"{min_rubles:.0f} ₽")
        )
        return
    
    if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
        max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t("PAYMENT_MAX_AMOUNT").format(
                amount=f"{max_rubles:,.0f} ₽".replace(",", " ")
            )
        )
        return
    
    try:
        payment_service = PaymentService(message.bot)
        
        payment_result = await payment_service.create_yookassa_sbp_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            receipt_email=None,
            receipt_phone=None,
            metadata=merge_vpn_deposit_bonus_metadata({
                "user_telegram_id": str(db_user.telegram_id),
                "user_username": db_user.username or "",
                "purpose": "balance_topup_sbp"
            }, db_user, state_data)
        )
        
        if not payment_result:
            await message.answer(texts.t("PAYMENT_SBP_CREATE_ERROR"))
            await state.clear()
            return
        
        confirmation_url = payment_result.get("confirmation_url")
        qr_confirmation_data = payment_result.get("qr_confirmation_data")
        
        if not confirmation_url and not qr_confirmation_data:
            await message.answer(texts.t("PAYMENT_SBP_DETAILS_ERROR"))
            await state.clear()
            return
        
        # Подготовим QR-код для вставки в основное сообщение
        qr_photo = None
        if qr_confirmation_data:
            try:
                # Импортируем необходимые модули для генерации QR-кода
                import base64
                from io import BytesIO
                import qrcode
                from aiogram.types import BufferedInputFile
                
                # Создаем QR-код из полученных данных
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(qr_confirmation_data)
                qr.make(fit=True)
                
                img = qr.make_image(fill_color="black", back_color="white")
                
                # Сохраняем изображение в байты
                img_bytes = BytesIO()
                img.save(img_bytes, format='PNG')
                img_bytes.seek(0)
                
                qr_photo = BufferedInputFile(img_bytes.getvalue(), filename="qrcode.png")
            except ImportError:
                logger.warning("qrcode библиотека не установлена, QR-код не будет сгенерирован")
            except Exception as e:
                logger.error(f"Ошибка генерации QR-кода: {e}")
        
        # Если нет QR-данных из YooKassa, но есть URL, генерируем QR-код из URL
        if not qr_photo and confirmation_url:
            try:
                # Импортируем необходимые модули для генерации QR-кода
                import base64
                from io import BytesIO
                import qrcode
                from aiogram.types import BufferedInputFile
                
                # Создаем QR-код из URL
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(confirmation_url)
                qr.make(fit=True)
                
                img = qr.make_image(fill_color="black", back_color="white")
                
                # Сохраняем изображение в байты
                img_bytes = BytesIO()
                img.save(img_bytes, format='PNG')
                img_bytes.seek(0)
                
                qr_photo = BufferedInputFile(img_bytes.getvalue(), filename="qrcode.png")
            except ImportError:
                logger.warning("qrcode библиотека не установлена, QR-код не будет сгенерирован")
            except Exception as e:
                logger.error(f"Ошибка генерации QR-кода из URL: {e}")
        
        # Создаем клавиатуру с кнопками для оплаты по ссылке и проверки статуса
        keyboard_buttons = []

        # Добавляем кнопку оплаты, если доступна ссылка
        if confirmation_url:
            keyboard_buttons.append([
                types.InlineKeyboardButton(text=texts.t("PAYMENT_SBP_GO_TO"), url=confirmation_url)
            ])
        else:
            # Если ссылка недоступна, предлагаем оплатить через ID платежа в приложении банка
            keyboard_buttons.append([
                types.InlineKeyboardButton(
                    text=texts.t("PAYMENT_SBP_PAY_IN_BANK_APP"),
                    callback_data="temp_disabled",
                )
            ])

        # Добавляем общие кнопки
        keyboard_buttons.append([
            types.InlineKeyboardButton(
                text=texts.t("PAYMENT_CHECK_STATUS"),
                callback_data=f"check_yookassa_{payment_result['local_payment_id']}",
            )
        ])
        keyboard_buttons.append([types.InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")])
        keyboard_buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data="balance_topup")])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        state_data = await state.get_data()
        prompt_message_id = state_data.get("yookassa_prompt_message_id")
        prompt_chat_id = state_data.get("yookassa_prompt_chat_id", message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning("Не удалось удалить сообщение с суммой YooKassa (СБП): %s", delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning(
                    "Не удалось удалить сообщение с запросом суммы YooKassa (СБП): %s",
                    delete_error,
                )

        # Подготавливаем текст сообщения
        message_text = texts.t("PAYMENT_SBP_INSTRUCTIONS").format(
            amount=settings.format_price(amount_kopeks),
            payment_id=payment_result["yookassa_payment_id"][:8],
        )

        # Добавляем инструкции в зависимости от доступных способов оплаты
        if not confirmation_url:
            message_text += texts.t("PAYMENT_SBP_MANUAL_INSTRUCTIONS").format(
                payment_id=payment_result["yookassa_payment_id"],
            )

        # Отправляем сообщение с инструкциями и клавиатурой
        # Если есть QR-код, отправляем его как медиа-сообщение
        if qr_photo:
            # Используем метод отправки медиа-группы или фото с описанием
            invoice_message = await message.answer_photo(
                photo=qr_photo,
                caption=message_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            # Если QR-код недоступен, отправляем обычное текстовое сообщение
            invoice_message = await message.answer(
                message_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        try:
            from app.services import payment_service as payment_module

            payment = await payment_module.get_yookassa_payment_by_local_id(
                db, payment_result["local_payment_id"]
            )
            if payment:
                metadata = dict(getattr(payment, "metadata_json", {}) or {})
                metadata["invoice_message"] = {
                    "chat_id": invoice_message.chat.id,
                    "message_id": invoice_message.message_id,
                }
                await db.execute(
                    update(payment.__class__)
                    .where(payment.__class__.id == payment.id)
                    .values(metadata_json=metadata, updated_at=datetime.utcnow())
                )
                await db.commit()
        except Exception as error:  # pragma: no cover - диагностический лог
            logger.warning("Не удалось сохранить сообщение YooKassa (СБП): %s", error)

        await state.update_data(
            yookassa_invoice_message_id=invoice_message.message_id,
            yookassa_invoice_chat_id=invoice_message.chat.id,
        )

        from .main import clear_state_preserve_topup_amount
        await clear_state_preserve_topup_amount(state)
        logger.info(f"Создан платеж YooKassa СБП для пользователя {db_user.telegram_id}: "
                   f"{amount_kopeks//100}₽, ID: {payment_result['yookassa_payment_id']}")
        
    except Exception as e:
        logger.error(f"Ошибка создания YooKassa СБП платежа: {e}")
        await message.answer(texts.t("PAYMENT_SBP_CREATE_ERROR"))
        await state.clear()





@error_handler
async def check_yookassa_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession
):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
        
        from app.database.crud.yookassa import get_yookassa_payment_by_local_id
        payment = await get_yookassa_payment_by_local_id(db, local_payment_id)
        
        if not payment:
            await callback.answer(
                get_texts(resolve_telegram_language(callback.from_user.language_code)).t(
                    "PAYMENT_NOT_FOUND"
                ),
                show_alert=True,
            )
            return

        texts = get_texts(getattr(getattr(payment, "user", None), "language", None))
        
        status_emoji = {
            "pending": "⏳",
            "waiting_for_capture": "⌛",
            "succeeded": "✅",
            "canceled": "❌",
            "failed": "❌"
        }
        
        status_text = {
            "pending": texts.t("PAYMENT_STATUS_PENDING"),
            "waiting_for_capture": texts.t("PAYMENT_STATUS_CONFIRMING"),
            "succeeded": texts.t("PAYMENT_STATUS_PAID"),
            "canceled": texts.t("PAYMENT_STATUS_CANCELLED"),
            "failed": texts.t("PAYMENT_STATUS_FAILED"),
        }
        
        emoji = status_emoji.get(payment.status, "❓")
        status = status_text.get(payment.status, texts.t("PAYMENT_STATUS_UNKNOWN"))
        
        message_text = texts.t("PAYMENT_STATUS_DETAILS").format(
            payment_id=payment.yookassa_payment_id[:8],
            amount=settings.format_price(payment.amount_kopeks),
            emoji=emoji,
            status=status,
            created_at=payment.created_at.strftime("%d.%m.%Y %H:%M"),
        )
        
        if payment.is_succeeded:
            message_text += texts.t("PAYMENT_STATUS_SUCCEEDED")
        elif payment.is_pending:
            message_text += texts.t("PAYMENT_STATUS_PENDING_HINT")
        elif payment.is_failed:
            message_text += texts.t("PAYMENT_STATUS_FAILED_HINT").format(
                support_contact=settings.get_support_contact_display(),
            )
        
        await callback.answer(message_text, show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка проверки статуса платежа: {e}")
        await callback.answer(
            get_texts(resolve_telegram_language(callback.from_user.language_code)).t(
                "PAYMENT_STATUS_ERROR"
            ),
            show_alert=True,
        )
