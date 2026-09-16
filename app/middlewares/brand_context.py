from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.branding.context import brand_scope


class BrandContextMiddleware(BaseMiddleware):
    """Выставляет бренд по боту, принявшему апдейт, на время его обработки."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        bot = data.get("bot")
        with brand_scope(getattr(bot, "id", None)):
            return await handler(event, data)
