import asyncio
import logging
import time
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject
from aiogram.fsm.context import FSMContext

from app.localization.language import resolve_telegram_language
from app.localization.texts import get_texts

logger = logging.getLogger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    
    def __init__(self, rate_limit: float = 0.5):
        self.rate_limit = rate_limit
        self.user_buckets: Dict[int, float] = {}
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
        
        if not user_id:
            return await handler(event, data)
        
        now = time.time()
        last_call = self.user_buckets.get(user_id, 0)
        
        if now - last_call < self.rate_limit:
            logger.warning(f"🚫 Throttling для пользователя {user_id}")
            texts = get_texts(
                resolve_telegram_language(getattr(event.from_user, "language_code", None))
            )

            # Для сообщений: молчим только если это состояние работы с тикетами; иначе показываем блок
            if isinstance(event, Message):
                try:
                    fsm: FSMContext = data.get("state")  # может отсутствовать
                    current = await fsm.get_state() if fsm else None
                except Exception:
                    current = None
                is_ticket_state = False
                if current:
                    # Молчим только в состояниях работы с тикетами (user/admin): waiting_for_message / waiting_for_reply
                    lowered = str(current)
                    is_ticket_state = (
                        (":waiting_for_message" in lowered or ":waiting_for_reply" in lowered) and
                        ("TicketStates" in lowered or "AdminTicketStates" in lowered)
                    )
                if is_ticket_state:
                    return
                # В остальных случаях — явный блок
                await event.answer(texts.t("THROTTLE_MESSAGE", "⏳ Пожалуйста, не отправляйте сообщения так часто!"))
                return
            # Для callback допустим краткое уведомление
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    texts.t("THROTTLE_ALERT", "⏳ Слишком быстро! Подождите немного."),
                    show_alert=True,
                )
                return
        
        self.user_buckets[user_id] = now
        
        cleanup_threshold = now - 60
        self.user_buckets = {
            uid: timestamp 
            for uid, timestamp in self.user_buckets.items() 
            if timestamp > cleanup_threshold
        }
        
        return await handler(event, data)
