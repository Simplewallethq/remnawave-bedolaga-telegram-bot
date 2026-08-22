import logging
import time
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

logger = logging.getLogger(__name__)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Slice Telegram entity text using UTF-16 code-unit offsets."""
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le")


def _custom_emoji_label(text: str, offset: int) -> str:
    """Return the annotation written before a custom emoji on its line."""
    encoded = text.encode("utf-16-le")
    prefix = encoded[:offset * 2].decode("utf-16-le")
    line_prefix = prefix.rsplit("\n", 1)[-1].strip()
    return line_prefix.rstrip(" -—–:").strip() or "без подписи"


class LoggingMiddleware(BaseMiddleware):
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        
        start_time = time.time()
        
        try:
            if isinstance(event, Message):
                user_info = f"@{event.from_user.username}" if event.from_user.username else f"ID:{event.from_user.id}"
                text = event.text or event.caption or "[медиа]"
                logger.info(f"📩 Сообщение от {user_info}: {text}")

                entities = event.entities if event.text else event.caption_entities
                for entity in entities or []:
                    entity_type = getattr(entity.type, "value", entity.type)
                    if entity_type != "custom_emoji" or not entity.custom_emoji_id:
                        continue
                    emoji = _utf16_slice(text, entity.offset, entity.length)
                    label = _custom_emoji_label(text, entity.offset)
                    logger.info(
                        "✨ Premium emoji от %s: назначение=%r emoji=%r custom_emoji_id=%s",
                        user_info,
                        label,
                        emoji,
                        entity.custom_emoji_id,
                    )
                
            elif isinstance(event, CallbackQuery):
                user_info = f"@{event.from_user.username}" if event.from_user.username else f"ID:{event.from_user.id}"
                logger.info(f"🔘 Callback от {user_info}: {event.data}")
            
            result = await handler(event, data)
            
            execution_time = time.time() - start_time
            if execution_time > 1.0:  
                logger.warning(f"⏱️ Медленная операция: {execution_time:.2f}s")
            
            return result
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"❌ Ошибка при обработке события за {execution_time:.2f}s: {e}")
            raise
