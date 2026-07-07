import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from aiogram.enums import ChatType

logger = logging.getLogger(__name__)


class PrivateChatOnlyMiddleware(BaseMiddleware):
    """Игнорирует все события не из личных чатов (группы, супергруппы, каналы)."""

    # Колбэки, которые обрабатываются в групповом админ-чате (кнопки на
    # уведомлениях). Права проверяются в самих хендлерах (settings.is_admin).
    GROUP_CALLBACK_PREFIXES = ("admin_rays_claim_",)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message:
            if event.data and event.data.startswith(self.GROUP_CALLBACK_PREFIXES):
                return await handler(event, data)
            chat = event.message.chat

        if chat and chat.type != ChatType.PRIVATE:
            return None

        return await handler(event, data)
