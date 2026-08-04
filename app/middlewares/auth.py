import asyncio
import logging
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject, User as TgUser
from aiogram.fsm.context import FSMContext

from app.config import settings
from app.database.database import get_db
from app.database.crud.user import get_user_by_telegram_id, create_user
from app.services.remnawave_service import RemnaWaveService
from app.states import RegistrationStates
from app.localization.language import resolve_telegram_language
from app.localization.texts import get_texts
from app.utils.check_reg_process import is_registration_process
from app.utils.validators import sanitize_telegram_name

logger = logging.getLogger(__name__)


async def _refresh_remnawave_description(
    remnawave_uuid: str,
    description: str,
    telegram_id: int
) -> None:
    try:
        remnawave_service = RemnaWaveService()
        async with remnawave_service.get_api_client() as api:
            await api.update_user(uuid=remnawave_uuid, description=description)
        logger.info(
            f"✅ [Middleware] Описание пользователя {telegram_id} обновлено в RemnaWave"
        )
    except Exception as remnawave_error:
        logger.error(
            f"❌ [Middleware] Ошибка обновления RemnaWave для {telegram_id}: {remnawave_error}"
        )


class AuthMiddleware(BaseMiddleware):
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:

        user: TgUser = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user
        
        if not user:
            return await handler(event, data)
        
        if user.is_bot:
            return await handler(event, data)
        
        async for db in get_db():
            try:
                db_user = await get_user_by_telegram_id(db, user.id)
                
                if not db_user:
                    texts = get_texts(resolve_telegram_language(getattr(user, "language_code", None)))
                    state: FSMContext = data.get('state')
                    current_state = None
                    
                    if state:
                        current_state = await state.get_state()

                    is_reg_process = is_registration_process(event, current_state)
                    
                    is_channel_check = (isinstance(event, CallbackQuery) 
                                       and event.data == "sub_channel_check")
                    
                    is_start_command = (isinstance(event, Message) 
                                       and event.text 
                                       and event.text.startswith('/start'))
                    
                    if is_reg_process or is_channel_check or is_start_command:
                        if is_start_command:
                            logger.info(f"🚀 Пропускаем команду /start от пользователя {user.id}")
                        elif is_channel_check:
                            logger.info(f"🔍 Пропускаем незарегистрированного пользователя {user.id} для проверки канала")
                        else:
                            logger.info(f"🔍 Пропускаем пользователя {user.id} в процессе регистрации")
                        data['db'] = db
                        data['db_user'] = None
                        data['is_admin'] = False
                        return await handler(event, data)
                    else:
                        if isinstance(event, Message):
                            await event.answer(
                                texts.t("AUTH_START_REQUIRED", "▶️ Для начала работы необходимо выполнить команду /start")
                            )
                        elif isinstance(event, CallbackQuery):
                            await event.answer(
                                texts.t("AUTH_START_REQUIRED_ALERT", "▶️ Необходимо начать с команды /start"),
                                show_alert=True
                            )
                        logger.info(f"🚫 Заблокирован незарегистрированный пользователь {user.id}")
                        return
                else:
                    texts = get_texts(db_user.language)
                    from app.database.models import UserStatus
                    
                    if db_user.status == UserStatus.BLOCKED.value:
                        if isinstance(event, Message):
                            await event.answer(texts.t("AUTH_ACCOUNT_BLOCKED", "🚫 Ваш аккаунт заблокирован администратором."))
                        elif isinstance(event, CallbackQuery):
                            await event.answer(
                                texts.t("AUTH_ACCOUNT_BLOCKED", "🚫 Ваш аккаунт заблокирован администратором."),
                                show_alert=True,
                            )
                        logger.info(f"🚫 Заблокированный пользователь {user.id} попытался использовать бота")
                        return
                    
                    if db_user.status == UserStatus.DELETED.value:
                        state: FSMContext = data.get('state')
                        current_state = None
                        
                        if state:
                            current_state = await state.get_state()
                        
                        registration_states = [
                            RegistrationStates.waiting_for_rules_accept.state,
                            RegistrationStates.waiting_for_privacy_policy_accept.state,
                            RegistrationStates.waiting_for_referral_code.state
                        ]

                        is_start_or_registration = (
                            (isinstance(event, Message) and event.text and event.text.startswith('/start'))
                            or (current_state in registration_states)
                            or (
                                isinstance(event, CallbackQuery)
                                and event.data
                                and (
                                    event.data in ['rules_accept', 'rules_decline', 'privacy_policy_accept', 'privacy_policy_decline', 'referral_skip']
                                )
                            )
                        )
                        
                        if is_start_or_registration:
                            logger.info(f"🔄 Удаленный пользователь {user.id} начинает повторную регистрацию")
                            data['db'] = db
                            data['db_user'] = None 
                            data['is_admin'] = False
                            return await handler(event, data)
                        else:
                            if isinstance(event, Message):
                                await event.answer(
                                    texts.t(
                                        "AUTH_ACCOUNT_DELETED",
                                        "❌ Ваш аккаунт был удален.\n"
                                        "🔄 Для повторной регистрации выполните команду /start",
                                    )
                                )
                            elif isinstance(event, CallbackQuery):
                                await event.answer(
                                    texts.t(
                                        "AUTH_ACCOUNT_DELETED_ALERT",
                                        "❌ Ваш аккаунт был удален. Для повторной регистрации выполните /start",
                                    ),
                                    show_alert=True
                                )
                            logger.info(f"❌ Удаленный пользователь {user.id} попытался использовать бота без /start")
                            return
                    
                    
                    profile_updated = False
                    
                    if db_user.username != user.username:
                        old_username = db_user.username
                        db_user.username = user.username
                        logger.info(f"🔄 [Middleware] Username обновлен для {user.id}: '{old_username}' → '{db_user.username}'")
                        profile_updated = True
                    
                    safe_first = sanitize_telegram_name(user.first_name)
                    safe_last = sanitize_telegram_name(user.last_name)
                    if db_user.first_name != safe_first:
                        old_first_name = db_user.first_name
                        db_user.first_name = safe_first
                        logger.info(f"🔄 [Middleware] Имя обновлено для {user.id}: '{old_first_name}' → '{db_user.first_name}'")
                        profile_updated = True
                    
                    if db_user.last_name != safe_last:
                        old_last_name = db_user.last_name
                        db_user.last_name = safe_last
                        logger.info(f"🔄 [Middleware] Фамилия обновлена для {user.id}: '{old_last_name}' → '{db_user.last_name}'")
                        profile_updated = True
                    
                    db_user.last_activity = datetime.utcnow()

                    if profile_updated:
                        db_user.updated_at = datetime.utcnow()
                        logger.info(f"💾 [Middleware] Профиль пользователя {user.id} обновлен в middleware")

                        if db_user.remnawave_uuid:
                            description = settings.format_remnawave_user_description(
                                full_name=db_user.full_name,
                                username=db_user.username,
                                telegram_id=db_user.telegram_id
                            )
                            asyncio.create_task(
                                _refresh_remnawave_description(
                                    remnawave_uuid=db_user.remnawave_uuid,
                                    description=description,
                                    telegram_id=db_user.telegram_id
                                )
                            )

                    await db.commit()

                data['db'] = db
                data['db_user'] = db_user
                data['is_admin'] = settings.is_admin(user.id)

                return await handler(event, data)
                
            except Exception as e:
                logger.error(f"Ошибка в AuthMiddleware: {e}")
                logger.error(f"Event type: {type(event)}")
                if hasattr(event, 'data'):
                    logger.error(f"Callback data: {event.data}")
                await db.rollback()
                raise
