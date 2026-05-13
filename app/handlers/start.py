import logging
from datetime import datetime
from typing import Optional
import os
from aiogram import Dispatcher, types, F, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.states import RegistrationStates
from app.database.crud.user import (
    get_user_by_telegram_id,
    create_user,
    get_user_by_referral_code,
    resolve_referrer_by_link_payload,
)
from app.database.crud.campaign import (
    get_campaign_by_start_parameter,
    get_campaign_by_id,
)
from app.database.models import PinnedMessage, SubscriptionStatus, UserStatus
from app.keyboards.inline import (
    get_rules_keyboard,
    get_privacy_policy_keyboard,
    get_main_menu_keyboard,
    get_main_menu_keyboard_async,
    get_post_registration_keyboard,
    get_language_selection_keyboard,
    get_welcome_keyboard,
    get_new_main_menu_keyboard,
    get_onboarding_device_selection_keyboard,
    get_onboarding_welcome_keyboard,
)
from app.database.crud.subscription import create_trial_subscription
from app.services.trial_activation_service import (
    charge_trial_activation_if_required,
    preview_trial_activation_charge,
)
from app.utils.subscription_utils import get_display_subscription_link
from app.handlers.menu import get_main_menu_text
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts, get_rules, get_privacy_policy
from app.services.referral_service import process_referral_registration
from app.services.campaign_service import AdvertisingCampaignService
from app.services.admin_notification_service import AdminNotificationService
from app.services.subscription_service import SubscriptionService
from app.services.support_settings_service import SupportSettingsService
from app.services.main_menu_button_service import MainMenuButtonService
from app.services.privacy_policy_service import PrivacyPolicyService
from app.services.pinned_message_service import (
    deliver_pinned_message_to_user,
    get_active_pinned_message,
)
from app.utils.user_utils import generate_unique_referral_code
from app.utils.photo_message import edit_or_answer_photo
from app.database.crud.subscription import decrement_subscription_server_counts
from app.services.blacklist_service import blacklist_service
from app.database.crud.device_link import get_device_link, create_device_link


logger = logging.getLogger(__name__)


async def _link_device_to_subscription(
    db: AsyncSession,
    subscription,
    device_id: str,
    target,
) -> None:
    """Link device_id to subscription. Device limit is intentionally not enforced here."""
    try:
        existing = await get_device_link(db, device_id)
        if existing and existing.subscription_id == subscription.id:
            await target.answer("Device already linked to your subscription.")
            return
        if existing:
            existing.subscription_id = subscription.id
            await db.commit()
            await target.answer("Device re-linked to your subscription.")
            return

        await create_device_link(db, subscription.id, device_id)
        await target.answer("Device linked to your subscription.")
    except Exception as e:
        logger.error("Error linking device %s to subscription %s: %s", device_id, subscription.id, e)
        # Per RESEARCH anti-pattern: don't block registration on device linking failure


def _calculate_subscription_flags(subscription):
    if not subscription:
        return False, False

    actual_status = getattr(subscription, "actual_status", None)
    has_active_subscription = actual_status in {"active", "trial"}
    subscription_is_active = bool(getattr(subscription, "is_active", False))

    return has_active_subscription, subscription_is_active


async def activate_trial_for_user(
    bot,
    db: AsyncSession,
    user,
    *,
    description: str = "Активация триала при регистрации",
) -> bool:
    """Activate a trial subscription for a user without any UI side effects.

    Creates the trial subscription, applies trial charge if configured, creates
    the corresponding RemnaWave panel user, refreshes the local user, and emits
    an admin notification. Returns True on success, False if the trial creation
    itself failed. Auxiliary steps (charge/remnawave/notification) are best-effort
    and never abort the activation.
    """
    logger.info(f"🎁 AUTO-TRIAL: Автоматическая активация триала для {user.telegram_id}")

    try:
        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        subscription = await create_trial_subscription(
            db,
            user.id,
            device_limit=forced_devices,
        )

        await db.refresh(user)

        try:
            charged_amount = await charge_trial_activation_if_required(
                db,
                user,
                description=description,
            )
        except Exception as charge_err:
            logger.warning("Ошибка списания за триал (продолжаем): %s", charge_err)
            charged_amount = 0

        subscription_service = SubscriptionService()
        try:
            await subscription_service.create_remnawave_user(
                db,
                subscription,
            )
        except Exception as rw_err:
            logger.error("Ошибка создания RemnaWave пользователя (продолжаем): %s", rw_err)

        await db.refresh(user, ['subscription'])

        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_trial_activation_notification(
                db,
                user,
                subscription,
                charged_amount_kopeks=charged_amount,
            )
        except Exception as notify_err:
            logger.error(f"Ошибка отправки уведомления о триале: {notify_err}")

        logger.info(f"✅ Триал автоматически активирован для {user.telegram_id}")
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка авто-активации триала для {user.telegram_id}: {e}")
        return False


async def _auto_activate_trial_and_show_device_selection(
    bot,
    db: AsyncSession,
    user,
    message_or_callback,
    is_callback: bool = False,
    language: str | None = None,
):
    """
    Auto-activate trial subscription during registration and show device selection.
    Used by both complete_registration (message) and complete_registration_from_callback (callback).
    """
    ok = await activate_trial_for_user(bot, db, user)
    if not ok:
        # Fallback: show main menu if trial activation fails
        return False

    # Show device selection directly (Screen 2)
    lang = language or getattr(user, "language", None) or DEFAULT_LANGUAGE
    texts = get_texts(lang)
    base_text = texts.t(
        "ONBOARDING_DEVICE_SELECTION_TEXT",
        "Выбери устройство для подключения:",
    )

    subscription_link = None
    if getattr(user, "subscription", None):
        subscription_link = get_display_subscription_link(user.subscription)

    if subscription_link:
        manual_intro = texts.t(
            "ONBOARDING_MANUAL_LINK_TEXT",
            "Для ручного подключения скопируй ключ и добавь его в Happ\n\n",
        ).rstrip()
        device_selection_text = (
            f"{base_text}\n\n{manual_intro}\n"
            f"<blockquote expandable><code>{subscription_link}</code></blockquote>"
        )
    else:
        device_selection_text = base_text

    image_path = os.path.join("images", "device_selection_screen.png")
    if not os.path.exists(image_path):
        image_path = None

    keyboard = get_onboarding_device_selection_keyboard(lang, share_link=subscription_link)

    if is_callback:
        await edit_or_answer_photo(
            callback=message_or_callback,
            caption=device_selection_text,
            keyboard=keyboard,
            photo_path=image_path,
            parse_mode="HTML",
        )
    else:
        if image_path:
            await message_or_callback.answer_photo(
                photo=types.FSInputFile(image_path),
                caption=device_selection_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message_or_callback.answer(
                device_selection_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )

    return True


async def _send_pinned_message(
    bot: Bot,
    db: AsyncSession,
    user,
    pinned_message: Optional[PinnedMessage] = None,
) -> None:
    try:
        await deliver_pinned_message_to_user(bot, db, user, pinned_message)
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Не удалось отправить закрепленное сообщение пользователю %s: %s",
            getattr(user, "telegram_id", "unknown"),
            error,
        )


async def _apply_campaign_bonus_if_needed(
    db: AsyncSession,
    user,
    state_data: dict,
    texts,
):
    campaign_id = state_data.get("campaign_id") if state_data else None
    if not campaign_id:
        return None

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign or not campaign.is_active:
        return None

    service = AdvertisingCampaignService()
    result = await service.apply_campaign_bonus(db, user, campaign)
    if not result.success:
        return None

    if result.bonus_type == "balance":
        amount_text = texts.format_price(result.balance_kopeks)
        return texts.CAMPAIGN_BONUS_BALANCE.format(
            amount=amount_text,
            name=campaign.name,
        )

    if result.bonus_type == "subscription":
        traffic_text = texts.format_traffic(result.subscription_traffic_gb or 0)
        return texts.CAMPAIGN_BONUS_SUBSCRIPTION.format(
            name=campaign.name,
            days=result.subscription_days,
            traffic=traffic_text,
            devices=result.subscription_device_limit,
        )

    return None


async def handle_potential_referral_code(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession
):
    current_state = await state.get_state()
    logger.info(f"🔍 REFERRAL/PROMO CHECK: Проверка сообщения '{message.text}' в состоянии {current_state}")

    if current_state not in [
        RegistrationStates.waiting_for_rules_accept.state,
        RegistrationStates.waiting_for_privacy_policy_accept.state,
        RegistrationStates.waiting_for_referral_code.state,
        None
    ]:
        return False

    user = await get_user_by_telegram_id(db, message.from_user.id)
    if user and user.status == UserStatus.ACTIVE.value:
        return False

    data = await state.get_data() or {}
    language = (
        data.get("language")
        or (getattr(user, "language", None) if user else None)
        or DEFAULT_LANGUAGE
    )
    texts = get_texts(language)

    potential_code = message.text.strip()
    if len(potential_code) < 4 or len(potential_code) > 20:
        return False

    # Сначала проверяем реферальный код
    referrer = await resolve_referrer_by_link_payload(db, potential_code)
    if referrer:
        data['referral_code'] = potential_code
        data['referrer_id'] = referrer.id
        await state.set_data(data)

        await message.answer(texts.t("REFERRAL_CODE_ACCEPTED", "✅ Реферальный код принят!"))
        logger.info(f"✅ Реферальный код {potential_code} применен для пользователя {message.from_user.id}")

        if current_state != RegistrationStates.waiting_for_referral_code.state:
            language = data.get('language', DEFAULT_LANGUAGE)
            texts = get_texts(language)

            rules_text = await get_rules(language)
            await message.answer(
                rules_text,
                reply_markup=get_rules_keyboard(language)
            )
            await state.set_state(RegistrationStates.waiting_for_rules_accept)
            logger.info("📋 Правила отправлены после ввода реферального кода")
        else:
            await complete_registration(message, state, db)

        return True

    # Если реферальный код не найден, проверяем промокод
    from app.database.crud.promocode import check_promocode_validity

    promocode_check = await check_promocode_validity(db, potential_code)

    if promocode_check["valid"]:
        # Промокод валиден - сохраняем его в state для активации после создания пользователя
        data['promocode'] = potential_code
        await state.set_data(data)

        await message.answer(
            texts.t(
                "PROMOCODE_ACCEPTED_WILL_ACTIVATE",
                "✅ Промокод принят! Он будет активирован после завершения регистрации."
            )
        )
        logger.info(f"✅ Промокод {potential_code} сохранен для активации для пользователя {message.from_user.id}")

        if current_state != RegistrationStates.waiting_for_referral_code.state:
            language = data.get('language', DEFAULT_LANGUAGE)
            texts = get_texts(language)

            rules_text = await get_rules(language)
            await message.answer(
                rules_text,
                reply_markup=get_rules_keyboard(language)
            )
            await state.set_state(RegistrationStates.waiting_for_rules_accept)
            logger.info("📋 Правила отправлены после принятия промокода")
        else:
            await complete_registration(message, state, db)

        return True

    # Ни реферальный код, ни промокод не найдены
    await message.answer(texts.t(
        "REFERRAL_OR_PROMO_CODE_INVALID_HELP",
        "❌ Неверный реферальный код или промокод.\n\n"
        "💡 Если у вас есть реферальный код или промокод, убедитесь что он введен правильно.\n"
        "⏭️ Для продолжения регистрации без кода используйте команду /start",
    ))
    return True


def _get_language_prompt_text() -> str:
    return "🌐 Выберите язык / Choose your language:"


async def _prompt_language_selection(message: types.Message, state: FSMContext) -> None:
    logger.info(f"🌐 LANGUAGE: Запрос выбора языка для пользователя {message.from_user.id}")

    await state.set_state(RegistrationStates.waiting_for_language)
    await message.answer(
        _get_language_prompt_text(),
        reply_markup=get_language_selection_keyboard(),
    )


async def _continue_registration_after_language(
    *,
    message: types.Message | None,
    callback: types.CallbackQuery | None,
    state: FSMContext,
    db: AsyncSession,
) -> None:
    data = await state.get_data() or {}
    language = data.get('language', DEFAULT_LANGUAGE)

    # Process referral code if present in state data (from /start payload)
    if data.get('referral_code'):
        referrer = await resolve_referrer_by_link_payload(db, data['referral_code'])
        if referrer:
            data['referrer_id'] = referrer.id
            await state.set_data(data)
            logger.info(f"✅ LANGUAGE: Реферер найден: {referrer.id}")

    # Skip rules and referral prompt — go straight to registration + onboarding
    logger.info("⚙️ LANGUAGE: Пропускаем правила и реферальный код, переходим к регистрации")
    if callback:
        await complete_registration_from_callback(callback, state, db)
    else:
        await complete_registration(message, state, db)



async def cmd_start(message: types.Message, state: FSMContext, db: AsyncSession, db_user=None):
    logger.info(f"🚀 START: Обработка /start от {message.from_user.id}")

    data = await state.get_data() or {}
    had_pending_payload = "pending_start_payload" in data
    pending_start_payload = data.pop("pending_start_payload", None)
    had_campaign_notification_flag = "campaign_notification_sent" in data
    campaign_notification_sent = data.pop("campaign_notification_sent", False)
    state_needs_update = had_pending_payload or had_campaign_notification_flag

    referral_code = None
    campaign = None
    start_args = message.text.split()
    start_parameter = None

    if len(start_args) > 1:
        start_parameter = start_args[1]
    elif pending_start_payload:
        start_parameter = pending_start_payload
        logger.info(
            "📦 START: Используем сохраненный payload '%s'",
            pending_start_payload,
        )

    if state_needs_update:
        await state.set_data(data)

    if start_parameter:
        campaign = await get_campaign_by_start_parameter(
            db,
            start_parameter,
            only_active=True,
        )

        if campaign:
            logger.info(
                "📣 Найдена рекламная кампания %s (start=%s)",
                campaign.id,
                campaign.start_parameter,
            )
            await state.update_data(campaign_id=campaign.id)
        else:
            if start_parameter.startswith("d_"):
                raw_device_id = start_parameter[2:]
                await state.update_data(device_id=raw_device_id)
                logger.info("Device ID saved to FSM state: %s", raw_device_id)
            else:
                referral_code = start_parameter
                logger.info(f"🔎 Найден реферальный код: {referral_code}")

    if referral_code:
        await state.update_data(referral_code=referral_code)

    user = db_user if db_user else await get_user_by_telegram_id(db, message.from_user.id)

    if campaign and not campaign_notification_sent:
        try:
            notification_service = AdminNotificationService(message.bot)
            await notification_service.send_campaign_link_visit_notification(
                db,
                message.from_user,
                campaign,
                user,
            )
        except Exception as notify_error:
            logger.error(
                "Ошибка отправки админ уведомления о переходе по кампании %s: %s",
                campaign.id,
                notify_error,
            )

    if user and user.status != UserStatus.DELETED.value:
        logger.info(f"✅ Активный пользователь найден: {user.telegram_id}")

        profile_updated = False

        if user.username != message.from_user.username:
            old_username = user.username
            user.username = message.from_user.username
            logger.info(f"📝 Username обновлен: '{old_username}' → '{user.username}'")
            profile_updated = True

        if user.first_name != message.from_user.first_name:
            old_first_name = user.first_name
            user.first_name = message.from_user.first_name
            logger.info(f"📝 Имя обновлено: '{old_first_name}' → '{user.first_name}'")
            profile_updated = True

        if user.last_name != message.from_user.last_name:
            old_last_name = user.last_name
            user.last_name = message.from_user.last_name
            logger.info(f"📝 Фамилия обновлена: '{old_last_name}' → '{user.last_name}'")
            profile_updated = True

        user.last_activity = datetime.utcnow()

        if profile_updated:
            user.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(user)
            logger.info(f"💾 Профиль пользователя {user.telegram_id} обновлен")
        else:
            await db.commit()

        texts = get_texts(user.language)

        if referral_code and not user.referred_by_id:
            await message.answer(
                texts.t(
                    "ALREADY_REGISTERED_REFERRAL",
                    "ℹ️ Вы уже зарегистрированы в системе. Реферальная ссылка не может быть применена.",
                )
            )

        if campaign:
            try:
                await message.answer(
                    texts.t(
                        "CAMPAIGN_EXISTING_USERL",
                        "ℹ️ Эта рекламная ссылка доступна только новым пользователям.",
                    )
                )
            except Exception as e:
                logger.error(
                    f"Ошибка отправки уведомления о рекламной кампании: {e}"
                )

        # Device-link deeplink for existing user without any subscription:
        # auto-create trial (same as for new users) and link the device, instead of showing the menu.
        _device_link_data = await state.get_data() or {}
        _pending_device_id = _device_link_data.get("device_id")
        if _pending_device_id and not user.subscription:
            await _auto_activate_trial_and_show_device_selection(
                message.bot, db, user, message, is_callback=False, language=user.language
            )
            await db.refresh(user, ["subscription"])
            if user.subscription:
                await _link_device_to_subscription(
                    db, user.subscription, _pending_device_id, message
                )
            await state.clear()
            return

        has_active_subscription, subscription_is_active = _calculate_subscription_flags(
            user.subscription
        )

        pinned_message = await get_active_pinned_message(db)

        if pinned_message and pinned_message.send_before_menu:
            await _send_pinned_message(message.bot, db, user, pinned_message)

        menu_text = await get_main_menu_text(user, texts, db)

        # Determine status for keyboard
        subscription = user.subscription
        is_active = subscription and subscription.is_active
        is_trial = subscription and getattr(subscription, "is_trial", False)
        
        trial_active = bool(is_active and is_trial)
        has_active_sub = bool(is_active and not is_trial)
        trial_used = (subscription is not None)
        
        # Проверяем, является ли пользователь администратором
        is_admin = settings.is_admin(user.telegram_id)
    
        keyboard = get_new_main_menu_keyboard(
            balance_rub=user.balance_kopeks / 100,
            trial_used=trial_used,
            trial_active=trial_active,
            has_active_subscription=has_active_sub,
            is_admin=is_admin,
            language=user.language,
        )

        image_path = os.path.join("images", "main_menu.png")
        force_text = settings.is_text_main_menu_mode()

        if not force_text and (settings.ENABLE_LOGO_MODE or os.path.exists(image_path)):
            from aiogram.types import FSInputFile
            from app.utils.bot_registry import get_logo_for_bot

            if os.path.exists(image_path):
                photo = FSInputFile(image_path)
            else:
                logo_path = get_logo_for_bot(message.bot.id if message.bot else None)
                photo = FSInputFile(logo_path)

            await message.answer_photo(
                photo=photo,
                caption=menu_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message.answer(
                menu_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )

        if pinned_message and not pinned_message.send_before_menu:
            await _send_pinned_message(message.bot, db, user, pinned_message)

        # Device linking for existing users — link regardless of subscription active state.
        data = await state.get_data() or {}
        device_id_from_state = data.get("device_id")
        if device_id_from_state and user.subscription:
            await _link_device_to_subscription(db, user.subscription, device_id_from_state, message)

        await state.clear()
        return

    if user and user.status == UserStatus.DELETED.value:
        logger.info(f"🔄 Удаленный пользователь {user.telegram_id} начинает повторную регистрацию")

        try:
            from app.services.user_service import UserService
            from app.database.models import (
                Subscription, Transaction, PromoCodeUse,
                ReferralEarning, SubscriptionServer
            )
            from sqlalchemy import delete

            if user.subscription:
                await decrement_subscription_server_counts(db, user.subscription)
                await db.execute(
                    delete(SubscriptionServer).where(
                        SubscriptionServer.subscription_id == user.subscription.id
                    )
                )
                logger.info(f"🗑️ Удалены записи SubscriptionServer")

            if user.subscription:
                await db.delete(user.subscription)
                logger.info(f"🗑️ Удалена подписка пользователя")

            await db.execute(
                delete(PromoCodeUse).where(PromoCodeUse.user_id == user.id)
            )

            await db.execute(
                delete(ReferralEarning).where(ReferralEarning.user_id == user.id)
            )
            await db.execute(
                delete(ReferralEarning).where(ReferralEarning.referral_id == user.id)
            )

            await db.execute(
                delete(Transaction).where(Transaction.user_id == user.id)
            )

            user.status = UserStatus.ACTIVE.value
            user.balance_kopeks = 0
            user.remnawave_uuid = None
            user.has_had_paid_subscription = False
            user.referred_by_id = None

            user.username = message.from_user.username
            user.first_name = message.from_user.first_name
            user.last_name = message.from_user.last_name
            user.updated_at = datetime.utcnow()
            user.last_activity = datetime.utcnow()

            from app.utils.user_utils import generate_unique_referral_code
            user.referral_code = await generate_unique_referral_code(db, user.telegram_id)

            await db.commit()

            logger.info(f"✅ Пользователь {user.telegram_id} подготовлен к восстановлению")

        except Exception as e:
            logger.error(f"❌ Ошибка подготовки к восстановлению: {e}")
            await db.rollback()
    else:
        logger.info(f"🆕 Новый пользователь, начинаем регистрацию")

    data = await state.get_data() or {}
    if not data.get('language'):
        if settings.is_language_selection_enabled():
            await _prompt_language_selection(message, state)
            return

        default_language = (
            (settings.DEFAULT_LANGUAGE or DEFAULT_LANGUAGE)
            if isinstance(settings.DEFAULT_LANGUAGE, str)
            else DEFAULT_LANGUAGE
        )
        normalized_default = default_language.split("-")[0].lower()
        data['language'] = normalized_default
        await state.set_data(data)
        logger.info(
            "🌐 LANGUAGE: выбор языка отключен, устанавливаем язык по умолчанию '%s'",
            normalized_default,
        )

    await _continue_registration_after_language(
        message=message,
        callback=None,
        state=state,
        db=db,
    )


async def process_language_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    logger.info(
        f"🌐 LANGUAGE: Пользователь {callback.from_user.id} выбрал язык ({callback.data})"
    )

    if not settings.is_language_selection_enabled():
        data = await state.get_data() or {}
        default_language = (
            (settings.DEFAULT_LANGUAGE or DEFAULT_LANGUAGE)
            if isinstance(settings.DEFAULT_LANGUAGE, str)
            else DEFAULT_LANGUAGE
        )
        normalized_default = default_language.split("-")[0].lower()
        data['language'] = normalized_default
        await state.set_data(data)

        texts = get_texts(normalized_default)

        try:
            await callback.message.edit_text(
                texts.t(
                    "LANGUAGE_SELECTION_DISABLED",
                    "⚙️ Выбор языка временно недоступен. Используем язык по умолчанию.",
                )
            )
        except Exception:
            await callback.message.answer(
                texts.t(
                    "LANGUAGE_SELECTION_DISABLED",
                    "⚙️ Выбор языка временно недоступен. Используем язык по умолчанию.",
                )
            )

        await callback.answer()

        await _continue_registration_after_language(
            message=None,
            callback=callback,
            state=state,
            db=db,
        )
        return

    selected_raw = (callback.data or "").split(":", 1)[-1]
    normalized_selected = selected_raw.strip().lower()

    available_map = {
        lang.strip().lower(): lang.strip()
        for lang in settings.get_available_languages()
        if isinstance(lang, str) and lang.strip()
    }

    if normalized_selected not in available_map:
        logger.warning(
            f"⚠️ LANGUAGE: Выбран недоступный язык '{normalized_selected}' пользователем {callback.from_user.id}"
        )
        await callback.answer("❌ Unsupported language", show_alert=True)
        return

    resolved_language = available_map[normalized_selected].lower()

    data = await state.get_data() or {}
    data['language'] = resolved_language
    await state.set_data(data)

    texts = get_texts(resolved_language)

    try:
        await callback.message.edit_text(
            texts.t("LANGUAGE_SELECTED", "🌐 Язык интерфейса обновлен."),
        )
    except Exception as error:
        logger.warning(
            f"⚠️ LANGUAGE: Не удалось обновить сообщение выбора языка: {error}")
        await callback.message.answer(
            texts.t("LANGUAGE_SELECTED", "🌐 Язык интерфейса обновлен."),
        )

    await callback.answer()

    await _continue_registration_after_language(
        message=None,
        callback=callback,
        state=state,
        db=db,
    )


async def _show_privacy_policy_after_rules(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    language: str,
) -> bool:
    """
    Показывает политику конфиденциальности после принятия правил.
    Возвращает True, если политика была показана, False если её нет или произошла ошибка.
    """
    policy = await PrivacyPolicyService.get_policy(db, language, fallback=True)

    if not policy or not policy.is_enabled:
        logger.info("⚠️ Политика конфиденциальности не включена, пропускаем её показ")
        return False

    if not policy.content or not policy.content.strip():
        privacy_policy_text = get_privacy_policy(language)
        if not privacy_policy_text or not privacy_policy_text.strip():
            logger.info("⚠️ Политика конфиденциальности включена, но дефолтный текст пустой, пропускаем показ")
            return False
        logger.info(f"🔒 Используется дефолтный текст политики конфиденциальности из локализации для языка {language}")
    else:
        privacy_policy_text = policy.content
        logger.info(f"🔒 Используется политика конфиденциальности из БД для языка {language}")

    try:
        await callback.message.edit_text(
            privacy_policy_text,
            reply_markup=get_privacy_policy_keyboard(language)
        )
        await state.set_state(RegistrationStates.waiting_for_privacy_policy_accept)
        logger.info(f"🔒 Политика конфиденциальности отправлена пользователю {callback.from_user.id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка при показе политики конфиденциальности: {e}", exc_info=True)
        try:
            await callback.message.answer(
                privacy_policy_text,
                reply_markup=get_privacy_policy_keyboard(language)
            )
            await state.set_state(RegistrationStates.waiting_for_privacy_policy_accept)
            logger.info(f"🔒 Политика конфиденциальности отправлена новым сообщением пользователю {callback.from_user.id}")
            return True
        except Exception as e2:
            logger.error(f"Критическая ошибка при отправке политики конфиденциальности: {e2}", exc_info=True)
            return False


async def _continue_registration_after_rules(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    language: str,
) -> None:
    """
    Продолжает регистрацию после принятия правил (реферальный код или завершение).
    """
    data = await state.get_data() or {}
    texts = get_texts(language)

    if data.get('referral_code'):
        logger.info(f"🎫 Найден реферальный код из deep link: {data['referral_code']}")

        referrer = await resolve_referrer_by_link_payload(db, data['referral_code'])
        if referrer:
            data['referrer_id'] = referrer.id
            await state.set_data(data)
            logger.info(f"✅ Реферер найден: {referrer.id}")

        await complete_registration_from_callback(callback, state, db)
    else:
        if settings.SKIP_REFERRAL_CODE:
            logger.info("⚙️ SKIP_REFERRAL_CODE включен - пропускаем запрос реферального кода")
            await complete_registration_from_callback(callback, state, db)
        else:
            try:
                await callback.message.edit_text(
                    texts.t(
                        "REFERRAL_CODE_QUESTION",
                        "У вас есть реферальный код? Введите его или нажмите 'Пропустить'",
                    ),
                    reply_markup=get_referral_code_keyboard(language)
                )
                await state.set_state(RegistrationStates.waiting_for_referral_code)
                logger.info(f"🔍 Ожидание ввода реферального кода")
            except Exception as e:
                logger.error(f"Ошибка при показе вопроса о реферальном коде: {e}")
                await complete_registration_from_callback(callback, state, db)


async def process_rules_accept(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обрабатывает принятие или отклонение правил пользователем.
    """
    logger.info(f"📋 RULES: Начало обработки правил")
    logger.info(f"📊 Callback data: {callback.data}")
    logger.info(f"👤 User: {callback.from_user.id}")

    current_state = await state.get_state()
    logger.info(f"📊 Текущее состояние: {current_state}")

    language = DEFAULT_LANGUAGE
    texts = get_texts(language)

    try:
        await callback.answer()

        data = await state.get_data() or {}
        language = data.get('language', language)
        texts = get_texts(language)

        if callback.data == 'rules_accept':
            logger.info(f"✅ Правила приняты пользователем {callback.from_user.id}")

            # Пытаемся показать политику конфиденциальности
            policy_shown = await _show_privacy_policy_after_rules(
                callback, state, db, language
            )

            # Если политика не была показана, продолжаем регистрацию
            if not policy_shown:
                await _continue_registration_after_rules(
                    callback, state, db, language
                )

        else:
            logger.info(f"❌ Правила отклонены пользователем {callback.from_user.id}")

            rules_required_text = texts.t(
                "RULES_REQUIRED",
                "Для использования бота необходимо принять правила сервиса.",
            )

            try:
                await callback.message.edit_text(
                    rules_required_text,
                    reply_markup=get_rules_keyboard(language)
                )
            except Exception as e:
                logger.error(f"Ошибка при показе сообщения об отклонении правил: {e}")
                try:
                    await callback.message.edit_text(
                        rules_required_text,
                        reply_markup=get_rules_keyboard(language)
                    )
                except:
                    pass

        logger.info(f"✅ Правила обработаны для пользователя {callback.from_user.id}")

    except Exception as e:
        logger.error(f"❌ Ошибка обработки правил: {e}", exc_info=True)
        await callback.answer(
            texts.t("ERROR_TRY_AGAIN", "❌ Произошла ошибка. Попробуйте еще раз."),
            show_alert=True,
        )

        try:
            data = await state.get_data() or {}
            language = data.get('language', language)
            texts = get_texts(language)
            await callback.message.answer(
                texts.t(
                    "ERROR_RULES_RETRY",
                    "Произошла ошибка. Попробуйте принять правила еще раз:",
                ),
                reply_markup=get_rules_keyboard(language)
            )
            await state.set_state(RegistrationStates.waiting_for_rules_accept)
        except:
            pass


async def process_privacy_policy_accept(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession
):

    logger.info(f"🔒 PRIVACY POLICY: Начало обработки политики конфиденциальности")
    logger.info(f"📊 Callback data: {callback.data}")
    logger.info(f"👤 User: {callback.from_user.id}")

    current_state = await state.get_state()
    logger.info(f"📊 Текущее состояние: {current_state}")

    language = DEFAULT_LANGUAGE
    texts = get_texts(language)

    try:
        await callback.answer()

        data = await state.get_data() or {}
        language = data.get('language', language)
        texts = get_texts(language)

        if callback.data == 'privacy_policy_accept':
            logger.info(f"✅ Политика конфиденциальности принята пользователем {callback.from_user.id}")

            try:
                await callback.message.delete()
                logger.info(f"🗑️ Сообщение с политикой конфиденциальности удалено")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось удалить сообщение с политикой конфиденциальности: {e}")
                try:
                    await callback.message.edit_text(
                        texts.t(
                            "PRIVACY_POLICY_ACCEPTED_PROCESSING",
                            "✅ Политика конфиденциальности принята! Продолжаем регистрацию...",
                        ),
                        reply_markup=None
                    )
                except Exception:
                    pass

            if data.get('referral_code'):
                logger.info(f"🎫 Найден реферальный код из deep link: {data['referral_code']}")

                referrer = await resolve_referrer_by_link_payload(db, data['referral_code'])
                if referrer:
                    data['referrer_id'] = referrer.id
                    await state.set_data(data)
                    logger.info(f"✅ Реферер найден: {referrer.id}")

                await complete_registration_from_callback(callback, state, db)
            else:
                if settings.SKIP_REFERRAL_CODE:
                    logger.info("⚙️ SKIP_REFERRAL_CODE включен - пропускаем запрос реферального кода")
                    await complete_registration_from_callback(callback, state, db)
                else:
                    try:
                        await state.set_data(data)
                        await state.set_state(RegistrationStates.waiting_for_referral_code)

                        await callback.bot.send_message(
                            chat_id=callback.from_user.id,
                            text=texts.t(
                                "REFERRAL_CODE_QUESTION",
                                "У вас есть реферальный код? Введите его или нажмите 'Пропустить'",
                            ),
                            reply_markup=get_referral_code_keyboard(language)
                        )
                        logger.info(f"🔍 Ожидание ввода реферального кода")
                    except Exception as e:
                        logger.error(f"Ошибка при показе вопроса о реферальном коде: {e}")
                        await complete_registration_from_callback(callback, state, db)

        else:
            logger.info(f"❌ Политика конфиденциальности отклонена пользователем {callback.from_user.id}")

            privacy_policy_required_text = texts.t(
                "PRIVACY_POLICY_REQUIRED",
                "Для использования бота необходимо принять политику конфиденциальности.",
            )

            try:
                await callback.message.edit_text(
                    privacy_policy_required_text,
                    reply_markup=get_privacy_policy_keyboard(language)
                )
            except Exception as e:
                logger.error(f"Ошибка при показе сообщения об отклонении политики конфиденциальности: {e}")
                try:
                    await callback.message.edit_text(
                        privacy_policy_required_text,
                        reply_markup=get_privacy_policy_keyboard(language)
                    )
                except:
                    pass

        logger.info(f"✅ Политика конфиденциальности обработана для пользователя {callback.from_user.id}")

    except Exception as e:
        logger.error(f"❌ Ошибка обработки политики конфиденциальности: {e}", exc_info=True)
        await callback.answer(
            texts.t("ERROR_TRY_AGAIN", "❌ Произошла ошибка. Попробуйте еще раз."),
            show_alert=True,
        )

        try:
            data = await state.get_data() or {}
            language = data.get('language', language)
            texts = get_texts(language)
            await callback.message.answer(
                texts.t(
                    "ERROR_PRIVACY_POLICY_RETRY",
                    "Произошла ошибка. Попробуйте принять политику конфиденциальности еще раз:",
                ),
                reply_markup=get_privacy_policy_keyboard(language)
            )
            await state.set_state(RegistrationStates.waiting_for_privacy_policy_accept)
        except:
            pass


async def process_referral_code_input(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession
):

    logger.info(f"🎫 REFERRAL/PROMO: Обработка кода: {message.text}")

    data = await state.get_data() or {}
    language = data.get('language', DEFAULT_LANGUAGE)
    texts = get_texts(language)

    code = message.text.strip()

    # Сначала проверяем, является ли это реферальным кодом
    referrer = await resolve_referrer_by_link_payload(db, code)
    if referrer:
        data['referrer_id'] = referrer.id
        await state.set_data(data)
        await message.answer(texts.t("REFERRAL_CODE_ACCEPTED", "✅ Реферальный код принят!"))
        logger.info(f"✅ Реферальный код применен: {code}")
        await complete_registration(message, state, db)
        return

    # Если реферальный код не найден, проверяем промокод
    from app.database.crud.promocode import check_promocode_validity

    promocode_check = await check_promocode_validity(db, code)

    if promocode_check["valid"]:
        # Промокод валиден - сохраняем его в state для активации после создания пользователя
        data['promocode'] = code
        await state.set_data(data)
        await message.answer(
            texts.t(
                "PROMOCODE_ACCEPTED_WILL_ACTIVATE",
                "✅ Промокод принят! Он будет активирован после завершения регистрации."
            )
        )
        logger.info(f"✅ Промокод сохранен для активации: {code}")
        await complete_registration(message, state, db)
        return

    # Ни реферальный код, ни промокод не найдены
    await message.answer(
        texts.t(
            "REFERRAL_OR_PROMO_CODE_INVALID",
            "❌ Неверный реферальный код или промокод"
        )
    )
    logger.info(f"❌ Неверный код (ни реферальный, ни промокод): {code}")
    return


async def process_referral_code_skip(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession
):

    logger.info(f"⭐️ SKIP: Пропуск реферального кода от пользователя {callback.from_user.id}")
    await callback.answer()

    data = await state.get_data() or {}
    language = data.get('language', DEFAULT_LANGUAGE)
    texts = get_texts(language)

    try:
        await callback.message.delete()
        logger.info(f"🗑️ Сообщение с вопросом о реферальном коде удалено")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить сообщение с вопросом о реферальном коде: {e}")
        try:
            await callback.message.edit_text(
                texts.t("REGISTRATION_COMPLETING", "✅ Завершаем регистрацию..."),
                reply_markup=None
            )
        except:
            pass

    await complete_registration_from_callback(callback, state, db)



async def complete_registration_from_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession
):
    logger.info(f"🎯 COMPLETE: Завершение регистрации для пользователя {callback.from_user.id}")

    # Проверяем, находится ли пользователь в черном списке
    is_blacklisted, blacklist_reason = await blacklist_service.is_user_blacklisted(
        callback.from_user.id,
        callback.from_user.username
    )

    if is_blacklisted:
        logger.warning(f"🚫 Пользователь {callback.from_user.id} находится в черном списке: {blacklist_reason}")
        try:
            await callback.message.answer(
                f"🚫 Регистрация невозможна\n\n"
                f"Причина: {blacklist_reason}\n\n"
                f"Если вы считаете, что это ошибка, обратитесь в поддержку."
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения о блокировке: {e}")

        await state.clear()
        return

    from sqlalchemy.orm import selectinload

    existing_user = await get_user_by_telegram_id(db, callback.from_user.id)

    if existing_user and existing_user.status == UserStatus.ACTIVE.value:
        logger.warning(f"⚠️ Пользователь {callback.from_user.id} уже активен! Показываем главное меню.")
        texts = get_texts(existing_user.language)

        data = await state.get_data() or {}
        if data.get('referral_code') and not existing_user.referred_by_id:
            await callback.message.answer(
                texts.t(
                    "ALREADY_REGISTERED_REFERRAL",
                    "ℹ️ Вы уже зарегистрированы в системе. Реферальная ссылка не может быть применена.",
                )
            )

        await db.refresh(existing_user, ['subscription'])

        has_active_subscription, subscription_is_active = _calculate_subscription_flags(
            existing_user.subscription
        )

        menu_text = await get_main_menu_text(existing_user, texts, db)

        is_admin = settings.is_admin(existing_user.telegram_id)
        is_moderator = (
            (not is_admin)
            and SupportSettingsService.is_moderator(existing_user.telegram_id)
        )

        custom_buttons = []
        if not settings.is_text_main_menu_mode():
            custom_buttons = await MainMenuButtonService.get_buttons_for_user(
                db,
                is_admin=is_admin,
                has_active_subscription=has_active_subscription,
                subscription_is_active=subscription_is_active,
            )

        try:
            keyboard = await get_main_menu_keyboard_async(
                db=db,
                user=existing_user,
                language=existing_user.language,
                is_admin=is_admin,
                has_had_paid_subscription=existing_user.has_had_paid_subscription,
                has_active_subscription=has_active_subscription,
                subscription_is_active=subscription_is_active,
                balance_kopeks=existing_user.balance_kopeks,
                subscription=existing_user.subscription,
                is_moderator=is_moderator,
                custom_buttons=custom_buttons,
            )
            await callback.message.answer(
                menu_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            await _send_pinned_message(callback.bot, db, existing_user)
        except Exception as e:
            logger.error(f"Ошибка при показе главного меню существующему пользователю: {e}")
            await callback.message.answer(
                texts.t(
                    "WELCOME_FALLBACK",
                    "Добро пожаловать, {user_name}!",
                ).format(user_name=existing_user.full_name)
            )

        await state.clear()
        return

    data = await state.get_data() or {}
    language = data.get('language', DEFAULT_LANGUAGE)
    texts = get_texts(language)

    campaign_id = data.get('campaign_id')
    is_new_user_registration = (
        existing_user is None
        or (
            existing_user
            and existing_user.status == UserStatus.DELETED.value
        )
    )

    referrer_id = data.get('referrer_id')
    if not referrer_id and data.get('referral_code'):
        referrer = await resolve_referrer_by_link_payload(db, data['referral_code'])
        if referrer:
            referrer_id = referrer.id

    if existing_user and existing_user.status == UserStatus.DELETED.value:
        logger.info(f"🔄 Восстанавливаем удаленного пользователя {callback.from_user.id}")

        existing_user.username = callback.from_user.username
        existing_user.first_name = callback.from_user.first_name
        existing_user.last_name = callback.from_user.last_name
        existing_user.language = language
        existing_user.referred_by_id = referrer_id
        existing_user.status = UserStatus.ACTIVE.value
        existing_user.balance_kopeks = 0
        existing_user.has_had_paid_subscription = False

        from datetime import datetime
        existing_user.updated_at = datetime.utcnow()
        existing_user.last_activity = datetime.utcnow()

        await db.commit()
        await db.refresh(existing_user, ['subscription'])

        user = existing_user
        logger.info(f"✅ Пользователь {callback.from_user.id} восстановлен")

    elif not existing_user:
        logger.info(f"🆕 Создаем нового пользователя {callback.from_user.id}")

        referral_code = await generate_unique_referral_code(db, callback.from_user.id)

        user = await create_user(
            db=db,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            language=language,
            referred_by_id=referrer_id,
            referral_code=referral_code,
            bot_id=callback.bot.id if callback.bot else None,
        )
        await db.refresh(user, ['subscription'])
    else:
        logger.info(f"🔄 Обновляем существующего пользователя {callback.from_user.id}")
        existing_user.status = UserStatus.ACTIVE.value
        existing_user.language = language
        if referrer_id and not existing_user.referred_by_id:
            existing_user.referred_by_id = referrer_id

        from datetime import datetime
        existing_user.updated_at = datetime.utcnow()
        existing_user.last_activity = datetime.utcnow()

        await db.commit()
        await db.refresh(existing_user, ['subscription'])
        user = existing_user

    if referrer_id:
        try:
            await process_referral_registration(db, user.id, referrer_id, callback.bot)
            logger.info(f"✅ Реферальная регистрация обработана для {user.id}")
        except Exception as e:
            logger.error(f"Ошибка при обработке реферальной регистрации: {e}")

    campaign_message = await _apply_campaign_bonus_if_needed(db, user, data, texts)

    try:
        await db.refresh(user)
    except Exception as refresh_error:
        logger.error(
            "Ошибка обновления данных пользователя %s после бонуса кампании: %s",
            user.telegram_id,
            refresh_error,
        )

    try:
        await db.refresh(user, ["subscription"])
    except Exception as refresh_subscription_error:
        logger.error(
            "Ошибка обновления подписки пользователя %s после бонуса кампании: %s",
            user.telegram_id,
            refresh_subscription_error,
        )

    # Read device_id BEFORE state.clear() per Pitfall 2
    _fsm_data = await state.get_data() or {}
    _device_id = _fsm_data.get("device_id")

    await state.clear()

    if campaign_message:
        try:
            await callback.message.answer(campaign_message)
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения о бонусе кампании: {e}")

    # Auto-activate trial and show device selection
    try:
        trial_success = await _auto_activate_trial_and_show_device_selection(
            callback.bot, db, user, callback, is_callback=True, language=language
        )
    except Exception as e:
        logger.error(f"Ошибка авто-активации триала при регистрации (callback): {e}")
        trial_success = False

    # Link device after trial creation per D-07
    if _device_id and user.subscription:
        await db.refresh(user, ["subscription"])
        if user.subscription:
            await _link_device_to_subscription(db, user.subscription, _device_id, callback.message or callback)

    if not trial_success:
        # Fallback: show welcome screen if trial failed
        texts = get_texts(language)
        welcome_text = texts.t(
            "WELCOME_TEXT",
            "⛱ Привет, тебе уже доступна бесплатная подписка на 3 дня!\n\n"
            "Всего 5 минут — и у тебя будет подключен самый быстрый VPN.\n"
            "Нажми «✨ Активировать» и начнём.",
        )
        try:
            image_path = os.path.join("images", "start_screen.png")
            if not os.path.exists(image_path):
                image_path = None
            await edit_or_answer_photo(
                callback=callback,
                caption=welcome_text,
                keyboard=get_welcome_keyboard(language=language),
                photo_path=image_path,
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке приветственного экрана из колбэка: {e}")

    await _send_pinned_message(callback.bot, db, user)

    logger.info(f"✅ Регистрация завершена для пользователя: {user.telegram_id}")


async def complete_registration(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession
):
    logger.info(f"🎯 COMPLETE: Завершение регистрации для пользователя {message.from_user.id}")

    # Проверяем, находится ли пользователь в черном списке
    is_blacklisted, blacklist_reason = await blacklist_service.is_user_blacklisted(
        message.from_user.id,
        message.from_user.username
    )

    if is_blacklisted:
        logger.warning(f"🚫 Пользователь {message.from_user.id} находится в черном списке: {blacklist_reason}")
        try:
            await message.answer(
                f"🚫 Регистрация невозможна\n\n"
                f"Причина: {blacklist_reason}\n\n"
                f"Если вы считаете, что это ошибка, обратитесь в поддержку."
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения о блокировке: {e}")

        await state.clear()
        return

    existing_user = await get_user_by_telegram_id(db, message.from_user.id)

    if existing_user and existing_user.status == UserStatus.ACTIVE.value:
        logger.warning(f"⚠️ Пользователь {message.from_user.id} уже активен! Показываем главное меню.")
        texts = get_texts(existing_user.language)

        data = await state.get_data() or {}
        if data.get('referral_code') and not existing_user.referred_by_id:
            await message.answer(
                texts.t(
                    "ALREADY_REGISTERED_REFERRAL",
                    "ℹ️ Вы уже зарегистрированы в системе. Реферальная ссылка не может быть применена.",
                )
            )

        await db.refresh(existing_user, ['subscription'])

        has_active_subscription, subscription_is_active = _calculate_subscription_flags(
            existing_user.subscription
        )

        menu_text = await get_main_menu_text(existing_user, texts, db)

        # Determine status for keyboard
        subscription = existing_user.subscription
        is_active = subscription and subscription.is_active
        is_trial = subscription and getattr(subscription, "is_trial", False)
        
        trial_active = bool(is_active and is_trial)
        has_active_sub = bool(is_active and not is_trial)
        trial_used = (subscription is not None)
        
        # Проверяем, является ли пользователь администратором
        is_admin = settings.is_admin(existing_user.telegram_id)

        try:
            keyboard = get_new_main_menu_keyboard(
                balance_rub=existing_user.balance_kopeks / 100,
                trial_used=trial_used,
                trial_active=trial_active,
                has_active_subscription=has_active_sub,
                is_admin=is_admin,
                language=existing_user.language,
            )
            await message.answer(
                menu_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            await _send_pinned_message(message.bot, db, existing_user)
        except Exception as e:
            logger.error(f"Ошибка при показе главного меню существующему пользователю: {e}")
            await message.answer(
                texts.t(
                    "WELCOME_FALLBACK",
                    "Добро пожаловать, {user_name}!",
                ).format(user_name=existing_user.full_name)
            )

        await state.clear()
        return

    data = await state.get_data() or {}
    language = data.get('language', DEFAULT_LANGUAGE)
    texts = get_texts(language)

    campaign_id = data.get('campaign_id')
    is_new_user_registration = (
        existing_user is None
        or (
            existing_user
            and existing_user.status == UserStatus.DELETED.value
        )
    )

    referrer_id = data.get('referrer_id')
    if not referrer_id and data.get('referral_code'):
        referrer = await resolve_referrer_by_link_payload(db, data['referral_code'])
        if referrer:
            referrer_id = referrer.id

    if existing_user and existing_user.status == UserStatus.DELETED.value:
        logger.info(f"🔄 Восстанавливаем удаленного пользователя {message.from_user.id}")

        existing_user.username = message.from_user.username
        existing_user.first_name = message.from_user.first_name
        existing_user.last_name = message.from_user.last_name
        existing_user.language = language
        existing_user.referred_by_id = referrer_id
        existing_user.status = UserStatus.ACTIVE.value
        existing_user.balance_kopeks = 0
        existing_user.has_had_paid_subscription = False

        from datetime import datetime
        existing_user.updated_at = datetime.utcnow()
        existing_user.last_activity = datetime.utcnow()

        await db.commit()
        await db.refresh(existing_user, ['subscription'])

        user = existing_user
        logger.info(f"✅ Пользователь {message.from_user.id} восстановлен")

    elif not existing_user:
        logger.info(f"🆕 Создаем нового пользователя {message.from_user.id}")

        referral_code = await generate_unique_referral_code(db, message.from_user.id)

        user = await create_user(
            db=db,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language=language,
            referred_by_id=referrer_id,
            referral_code=referral_code,
            bot_id=message.bot.id if message.bot else None,
        )
        await db.refresh(user, ['subscription'])
    else:
        logger.info(f"🔄 Обновляем существующего пользователя {message.from_user.id}")
        existing_user.status = UserStatus.ACTIVE.value
        existing_user.language = language
        if referrer_id and not existing_user.referred_by_id:
            existing_user.referred_by_id = referrer_id

        from datetime import datetime
        existing_user.updated_at = datetime.utcnow()
        existing_user.last_activity = datetime.utcnow()

        await db.commit()
        await db.refresh(existing_user, ['subscription'])
        user = existing_user

    if referrer_id:
        try:
            await process_referral_registration(db, user.id, referrer_id, message.bot)
            logger.info(f"✅ Реферальная регистрация обработана для {user.id}")
        except Exception as e:
            logger.error(f"Ошибка при обработке реферальной регистрации: {e}")

    # Активируем промокод если был сохранен в state
    promocode_to_activate = data.get('promocode')
    if promocode_to_activate:
        try:
            from app.handlers.promocode import activate_promocode_for_registration

            promocode_result = await activate_promocode_for_registration(
                db, user.id, promocode_to_activate, message.bot
            )

            if promocode_result["success"]:
                await message.answer(
                    texts.t(
                        "PROMOCODE_ACTIVATED_AT_REGISTRATION",
                        "✅ Промокод активирован!\n\n{description}"
                    ).format(description=promocode_result["description"])
                )
                logger.info(f"✅ Промокод {promocode_to_activate} активирован для пользователя {user.id}")
            else:
                logger.warning(f"⚠️ Не удалось активировать промокод {promocode_to_activate}: {promocode_result.get('error')}")
        except Exception as e:
            logger.error(f"❌ Ошибка при активации промокода {promocode_to_activate}: {e}")

    campaign_message = await _apply_campaign_bonus_if_needed(db, user, data, texts)

    try:
        await db.refresh(user)
    except Exception as refresh_error:
        logger.error(
            "Ошибка обновления данных пользователя %s после бонуса кампании: %s",
            user.telegram_id,
            refresh_error,
        )

    try:
        await db.refresh(user, ["subscription"])
    except Exception as refresh_subscription_error:
        logger.error(
            "Ошибка обновления подписки пользователя %s после бонуса кампании: %s",
            user.telegram_id,
            refresh_subscription_error,
        )

    # Read device_id BEFORE state.clear() per Pitfall 2
    _fsm_data = await state.get_data() or {}
    _device_id = _fsm_data.get("device_id")

    await state.clear()

    if campaign_message:
        try:
            await callback.message.answer(campaign_message)
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения о бонусе кампании: {e}")

    # Auto-activate trial and show device selection
    try:
        trial_success = await _auto_activate_trial_and_show_device_selection(
            message.bot, db, user, message, is_callback=False, language=language
        )
    except Exception as e:
        logger.error(f"Ошибка авто-активации триала при регистрации: {e}")
        trial_success = False

    # Link device after trial creation per D-07
    if _device_id and user.subscription:
        await db.refresh(user, ["subscription"])
        if user.subscription:
            await _link_device_to_subscription(db, user.subscription, _device_id, message)

    if not trial_success:
        # Fallback: show welcome screen if trial failed
        texts = get_texts(language)
        welcome_text = texts.t(
            "WELCOME_TEXT",
            "⛱ Привет, тебе уже доступна бесплатная подписка на 3 дня!\n\n"
            "Всего 5 минут — и у тебя будет подключен самый быстрый VPN.\n"
            "Нажми «✨ Активировать» и начнём.",
        )
        try:
            image_path = os.path.join("images", "start_screen.png")
            if os.path.exists(image_path):
                await message.answer_photo(
                    photo=types.FSInputFile(image_path),
                    caption=welcome_text,
                    reply_markup=get_welcome_keyboard(),
                )
            else:
                await message.answer(
                    welcome_text,
                    reply_markup=get_welcome_keyboard(),
                )
        except Exception as e:
            logger.error(f"Ошибка при отправке приветственного экрана: {e}")

    await _send_pinned_message(message.bot, db, user)


    logger.info(f"✅ Регистрация завершена для пользователя: {user.telegram_id}")


def get_referral_code_keyboard(language: str):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=texts.t("REFERRAL_CODE_SKIP", "⭐️ Пропустить"),
            callback_data="referral_skip"
        )]
    ])



async def required_sub_channel_check(
    query: types.CallbackQuery,
    bot: Bot,
    state: FSMContext,
    db: AsyncSession,
    db_user=None
):
    language = DEFAULT_LANGUAGE
    texts = get_texts(language)

    try:
        state_data = await state.get_data() or {}

        pending_start_payload = state_data.pop("pending_start_payload", None)
        state_updated = pending_start_payload is not None

        if pending_start_payload:
            logger.info(
                "📦 CHANNEL CHECK: Найден сохраненный payload '%s'",
                pending_start_payload,
            )

            if "campaign_id" not in state_data and "referral_code" not in state_data:
                campaign = await get_campaign_by_start_parameter(
                    db,
                    pending_start_payload,
                    only_active=True,
                )

                if campaign:
                    state_data["campaign_id"] = campaign.id
                    logger.info(
                        "📣 CHANNEL CHECK: Кампания %s восстановлена из payload",
                        campaign.id,
                    )
                else:
                    state_data["referral_code"] = pending_start_payload
                    logger.info(
                        "🎯 CHANNEL CHECK: Payload интерпретирован как реферальный код",
                    )
            else:
                logger.debug(
                    "ℹ️ CHANNEL CHECK: Payload уже обработан ранее, пропускаем восстановление",
                )

        if state_updated:
            await state.set_data(state_data)

        user = db_user
        if not user:
            user = await get_user_by_telegram_id(db, query.from_user.id)

        if user and getattr(user, "language", None):
            language = user.language
        elif state_data.get("language"):
            language = state_data["language"]

        texts = get_texts(language)

        chat_member = await bot.get_chat_member(
            chat_id=settings.CHANNEL_SUB_ID,
            user_id=query.from_user.id
        )

        if chat_member.status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
            return await query.answer(
                texts.t("CHANNEL_SUBSCRIBE_REQUIRED_ALERT", "❌ Вы не подписались на канал!"),
                show_alert=True,
            )

        if user and user.subscription:
            subscription = user.subscription
            if (
                subscription.is_trial
                and subscription.status == SubscriptionStatus.DISABLED.value
            ):
                subscription.status = SubscriptionStatus.ACTIVE.value
                subscription.updated_at = datetime.utcnow()
                await db.commit()
                await db.refresh(subscription)
                logger.info(
                    "✅ Триальная подписка пользователя %s восстановлена после подтверждения подписки на канал",
                    user.telegram_id,
                )

                try:
                    subscription_service = SubscriptionService()
                    if user.remnawave_uuid:
                        await subscription_service.update_remnawave_user(db, subscription)
                    else:
                        await subscription_service.create_remnawave_user(db, subscription)
                except Exception as api_error:
                    logger.error(
                        "❌ Ошибка обновления RemnaWave при восстановлении подписки пользователя %s: %s",
                        user.telegram_id if user else query.from_user.id,
                        api_error,
                    )

        await query.answer(
            texts.t("CHANNEL_SUBSCRIBE_THANKS", "✅ Спасибо за подписку"),
            show_alert=True,
        )

        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение: {e}")

        if user and user.status != UserStatus.DELETED.value:
            has_active_subscription, subscription_is_active = _calculate_subscription_flags(
                user.subscription
            )

            menu_text = await get_main_menu_text(user, texts, db)

            from app.utils.bot_registry import get_logo_for_bot
            from aiogram.types import FSInputFile

            is_admin = settings.is_admin(user.telegram_id)
            is_moderator = (
                (not is_admin)
                and SupportSettingsService.is_moderator(user.telegram_id)
            )

            custom_buttons = await MainMenuButtonService.get_buttons_for_user(
                db,
                is_admin=is_admin,
                has_active_subscription=has_active_subscription,
                subscription_is_active=subscription_is_active,
            )

            keyboard = await get_main_menu_keyboard_async(
                db=db,
                user=user,
                language=user.language,
                is_admin=is_admin,
                has_had_paid_subscription=user.has_had_paid_subscription,
                has_active_subscription=has_active_subscription,
                subscription_is_active=subscription_is_active,
                balance_kopeks=user.balance_kopeks,
                subscription=user.subscription,
                is_moderator=is_moderator,
                custom_buttons=custom_buttons,
            )

            if settings.ENABLE_LOGO_MODE:
                await bot.send_photo(
                    chat_id=query.from_user.id,
                    photo=FSInputFile(get_logo_for_bot(bot.id)),
                    caption=menu_text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=query.from_user.id,
                    text=menu_text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            await _send_pinned_message(bot, db, user)
        else:
            from app.keyboards.inline import get_rules_keyboard

            state_data['language'] = language
            await state.set_data(state_data)

            if settings.SKIP_RULES_ACCEPT:
                if settings.SKIP_REFERRAL_CODE:
                    from app.utils.user_utils import generate_unique_referral_code

                    referral_code = await generate_unique_referral_code(db, query.from_user.id)

                    user = await create_user(
                        db=db,
                        telegram_id=query.from_user.id,
                        username=query.from_user.username,
                        first_name=query.from_user.first_name,
                        last_name=query.from_user.last_name,
                        language=language,
                        referral_code=referral_code,
                        bot_id=query.bot.id if query.bot else None,
                    )

                    await bot.send_message(
                        chat_id=query.from_user.id,
                        text=texts.t("WELCOME_FALLBACK", "Добро пожаловать, {user_name}!").format(user_name=user.full_name),
                    )
                else:
                    await bot.send_message(
                        chat_id=query.from_user.id,
                        text=texts.t(
                            "REFERRAL_CODE_QUESTION",
                            "У вас есть реферальный код? Введите его или нажмите 'Пропустить'",
                        ),
                        reply_markup=get_referral_code_keyboard(language),
                    )
                    await state.set_state(RegistrationStates.waiting_for_referral_code)
            else:
                from app.utils.bot_registry import get_logo_for_bot
                from aiogram.types import FSInputFile

                rules_text = await get_rules(language)

                if settings.ENABLE_LOGO_MODE:
                    await bot.send_photo(
                        chat_id=query.from_user.id,
                        photo=FSInputFile(get_logo_for_bot(bot.id)),
                        caption=rules_text,
                        reply_markup=get_rules_keyboard(language),
                    )
                else:
                    await bot.send_message(
                        chat_id=query.from_user.id,
                        text=rules_text,
                        reply_markup=get_rules_keyboard(language),
                    )
                await state.set_state(RegistrationStates.waiting_for_rules_accept)

    except Exception as e:
        logger.error(f"Ошибка в required_sub_channel_check: {e}")
        await query.answer(f"{texts.ERROR}!", show_alert=True)

def register_handlers(dp: Dispatcher):

    logger.info("🔧 === НАЧАЛО регистрации обработчиков start.py ===")

    dp.message.register(
        cmd_start,
        Command("start")
    )
    logger.info("✅ Зарегистрирован cmd_start")

    dp.callback_query.register(
        process_rules_accept,
        F.data.in_(["rules_accept", "rules_decline"]),
        StateFilter(RegistrationStates.waiting_for_rules_accept)
    )
    logger.info("✅ Зарегистрирован process_rules_accept")

    dp.callback_query.register(
        process_privacy_policy_accept,
        F.data.in_(["privacy_policy_accept", "privacy_policy_decline"]),
        StateFilter(RegistrationStates.waiting_for_privacy_policy_accept)
    )
    logger.info("✅ Зарегистрирован process_privacy_policy_accept")

    dp.callback_query.register(
        process_language_selection,
        F.data.startswith("language_select:"),
        StateFilter(RegistrationStates.waiting_for_language)
    )
    logger.info("✅ Зарегистрирован process_language_selection")

    dp.callback_query.register(
        process_referral_code_skip,
        F.data == "referral_skip",
        StateFilter(RegistrationStates.waiting_for_referral_code)
    )
    logger.info("✅ Зарегистрирован process_referral_code_skip")

    dp.message.register(
        process_referral_code_input,
        StateFilter(RegistrationStates.waiting_for_referral_code)
    )
    logger.info("✅ Зарегистрирован process_referral_code_input")

    dp.message.register(
        handle_potential_referral_code,
        StateFilter(
            RegistrationStates.waiting_for_rules_accept,
            RegistrationStates.waiting_for_referral_code
        )
    )
    logger.info("✅ Зарегистрирован handle_potential_referral_code")

    dp.callback_query.register(
        required_sub_channel_check,
        F.data.in_(["sub_channel_check"])
    )
    logger.info("✅ Зарегистрирован required_sub_channel_check")

    logger.info("🔧 === КОНЕЦ регистрации обработчиков start.py ===")

