"""Доставка одного сообщения админской рассылки из нужного бота.

Две особенности, из-за которых простого send_photo(file_id) не хватает:

- file_id медиа привязан к боту, который его получил (обычно основному). Зеркало
  или копикет по чужому file_id фото не отправит. Файл один раз скачиваем у
  основного бота и загружаем в каждый другой бот при первой отправке; полученный
  file_id запоминаем для остальных получателей этого бота.
- Премиум-эмодзи (`<tg-emoji>`) Telegram разрешает боту, только если у его
  владельца есть Telegram Premium. Если бот получил отказ с эмодзи, а без них
  сообщение прошло, дальше шлём из этого бота обычные эмодзи.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

_CUSTOM_EMOJI_RE = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", re.DOTALL)

_DEFAULT_FILENAMES = {
    "photo": "image.jpg",
    "video": "video.mp4",
    "document": "file",
}


def strip_custom_emoji(html_text: str) -> str:
    """Премиум-эмодзи → их обычные эмодзи-заменители."""
    return _CUSTOM_EMOJI_RE.sub(r"\1", html_text or "")


def message_html(message: Message) -> str:
    """Текст или подпись сообщения админа в HTML для parse_mode="HTML".

    С форматированием Telegram (жирный, цитата, премиум-эмодзи) — собираем HTML из
    сущностей. Без сущностей — берём текст как есть: админ мог набрать теги руками.
    """
    entities = message.entities or message.caption_entities
    if entities:
        return message.html_text
    return message.text or message.caption or ""


def extract_media(message: Message) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(media_type, file_id, file_name) медиа из сообщения админа или (None, None, None)."""
    if message.photo:
        return "photo", message.photo[-1].file_id, None
    if message.video:
        return "video", message.video.file_id, message.video.file_name
    if message.document:
        return "document", message.document.file_id, message.document.file_name
    return None, None, None


def _sent_file_id(message: Any, media_type: str) -> Optional[str]:
    if media_type == "photo" and getattr(message, "photo", None):
        return message.photo[-1].file_id
    media = getattr(message, media_type, None)
    return getattr(media, "file_id", None)


class BroadcastSender:
    def __init__(
        self,
        main_bot: Bot,
        *,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        media_type: Optional[str] = None,
        media_file_id: Optional[str] = None,
        media_file_name: Optional[str] = None,
    ) -> None:
        self.main_bot = main_bot
        self.text = text or ""
        self.reply_markup = reply_markup
        self.media_type = media_type if media_file_id else None
        self.media_file_name = media_file_name or _DEFAULT_FILENAMES.get(media_type or "", "file")
        self._file_ids: dict[int, str] = {}
        if self.media_type:
            self._file_ids[main_bot.id] = media_file_id
        self._source_file_id = media_file_id
        self._upload_locks: dict[int, asyncio.Lock] = {}
        self._download_lock = asyncio.Lock()
        self._source_bytes: Optional[bytes] = None
        self._source_failed = False
        self._plain_emoji_bots: set[int] = set()

    async def send(self, bot: Bot, chat_id: int) -> Message:
        """Отправить сообщение рассылки. Ошибки Telegram пробрасываются наверх."""
        if not self.media_type:
            return await self._with_emoji_fallback(bot, lambda text: self._send_text(bot, chat_id, text))

        file_id = self._file_ids.get(bot.id)
        if file_id:
            return await self._send_media_with_fallback(bot, chat_id, file_id)

        lock = self._upload_locks.setdefault(bot.id, asyncio.Lock())
        async with lock:
            file_id = self._file_ids.get(bot.id)
            if file_id:
                return await self._send_media_with_fallback(bot, chat_id, file_id)
            data = await self._load_source_bytes()
            if data is None:
                return await self._with_emoji_fallback(bot, lambda text: self._send_text(bot, chat_id, text))
            upload = BufferedInputFile(data, filename=self.media_file_name)
            sent = await self._send_media_with_fallback(bot, chat_id, upload)
            new_file_id = _sent_file_id(sent, self.media_type)
            if new_file_id:
                self._file_ids[bot.id] = new_file_id
            return sent

    async def _send_media_with_fallback(self, bot: Bot, chat_id: int, media: Any) -> Message:
        return await self._with_emoji_fallback(bot, lambda text: self._send_media(bot, chat_id, media, text))

    async def _with_emoji_fallback(self, bot: Bot, send):
        if bot.id in self._plain_emoji_bots:
            return await send(strip_custom_emoji(self.text))
        try:
            return await send(self.text)
        except TelegramBadRequest:
            plain = strip_custom_emoji(self.text)
            if plain == self.text:
                raise
            sent = await send(plain)
            self._plain_emoji_bots.add(bot.id)
            logger.warning("Рассылка: бот %s не может слать премиум-эмодзи, шлём обычные", bot.id)
            return sent

    async def _send_text(self, bot: Bot, chat_id: int, text: str) -> Message:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self.reply_markup,
        )

    async def _send_media(self, bot: Bot, chat_id: int, media: Any, caption: str) -> Message:
        field = self.media_type
        send = getattr(bot, f"send_{field}")
        return await send(
            chat_id=chat_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=self.reply_markup,
            **{field: media},
        )

    async def _load_source_bytes(self) -> Optional[bytes]:
        if self._source_bytes is not None or self._source_failed:
            return self._source_bytes
        async with self._download_lock:
            if self._source_bytes is not None or self._source_failed:
                return self._source_bytes
            try:
                buffer = await self.main_bot.download(self._source_file_id)
                self._source_bytes = buffer.read() if buffer is not None else None
            except Exception as error:
                logger.error("Рассылка: не удалось скачать медиа у основного бота: %s", error)
                self._source_bytes = None
            if self._source_bytes is None:
                self._source_failed = True
            return self._source_bytes
