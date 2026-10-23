"""Интерфейс бота-конструктора: ОДНО обновляемое rich-сообщение.

У каждого пользователя в чате ровно одно сообщение бота (session["msg_id"]),
любой экран — его редактирование. Новое сообщение отправляется, только если
старое нельзя отредактировать (удалено, /start), и тогда старое удаляется.

Сессия (id сообщения, текущий экран, черновик мастера) лежит в FSM-хранилище
бота (Redis) под ключом конструктора.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
    RichBlockPhoto,
    WebAppInfo,
)

from .emoji import ch, icon_id

logger = logging.getLogger(__name__)

ASSETS = Path("assets/constructor")
SUCCESS, PRIMARY, DANGER = "success", "primary", "danger"
_TG_EMOJI = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')


@dataclass
class Btn:
    text: str
    cb: Optional[str] = None
    url: Optional[str] = None
    web_app: Optional[str] = None
    style: Optional[str] = None
    icon: Optional[str] = None

    def build(self, plain: bool = False) -> InlineKeyboardButton:
        custom_id = None if plain else icon_id(self.icon)
        text = self.text if (custom_id or not self.icon) else f"{ch(self.icon)} {self.text}"
        kwargs: dict = {}
        if self.web_app:
            kwargs["web_app"] = WebAppInfo(url=self.web_app)
        elif self.url:
            kwargs["url"] = self.url
        else:
            kwargs["callback_data"] = self.cb
        return InlineKeyboardButton(text=text, icon_custom_emoji_id=custom_id, style=self.style, **kwargs)


@dataclass
class Screen:
    body: str
    kb: list[list[Optional[Btn]]] = field(default_factory=list)
    # Имя баннера из assets/constructor или "file:<путь>" — обложка бота владельца.
    cover: str = "main"

    def markup(self, plain: bool = False) -> Optional[InlineKeyboardMarkup]:
        rows = [[button.build(plain) for button in row if button] for row in self.kb]
        rows = [row for row in rows if row]
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def new_session() -> dict:
    return {"msg_id": None, "route": "main", "draft": None}


class Sessions:
    """Сессии пользователей в FSM-хранилище под id бота-конструктора."""

    def __init__(self, storage: BaseStorage, bot_id: int):
        self._storage = storage
        self._bot_id = bot_id

    def _key(self, uid: int) -> StorageKey:
        return StorageKey(bot_id=self._bot_id, chat_id=uid, user_id=uid)

    async def load(self, uid: int) -> dict:
        data = await self._storage.get_data(self._key(uid))
        return {**new_session(), **(data or {})}

    async def save(self, uid: int, session: dict) -> None:
        await self._storage.set_data(self._key(uid), session)


def _first_photo_id(message: Message) -> Optional[str]:
    rich = getattr(message, "rich_message", None)
    for block in rich.blocks if rich else []:
        if isinstance(block, RichBlockPhoto) and block.photo:
            return block.photo[-1].file_id
    return None


class UI:
    def __init__(self, bot: Bot, sessions: Sessions):
        self.bot = bot
        self.sessions = sessions
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # file_id загруженных обложек: путь@mtime → file_id (перезаливаются после рестарта)
        self._file_ids: dict[str, str] = {}

    def lock(self, uid: int) -> asyncio.Lock:
        """Действия пользователя обрабатываются по очереди."""
        return self._locks[uid]

    def _cover(self, cover: str):
        path = Path(cover[5:]) if cover.startswith("file:") else ASSETS / f"{cover}.png"
        if not path.exists():
            path = ASSETS / "main.png"
        key = f"{path}@{int(path.stat().st_mtime)}"
        if file_id := self._file_ids.get(key):
            return file_id, None
        return FSInputFile(path), key

    async def show(self, uid: int, session: dict, screen: Screen, *, fresh: bool = False) -> None:
        try:
            await self._show(uid, session, screen, fresh, plain=False)
        except TelegramBadRequest as error:
            # Telegram отклонил премиум-эмодзи — экран важнее, показываем обычные.
            logger.warning("Конструктор: премиум-эмодзи отклонены (%s)", error.message)
            await self._show(uid, session, screen, fresh, plain=True)

    async def _show(self, uid: int, session: dict, screen: Screen, fresh: bool, plain: bool) -> None:
        media, upload_key = self._cover(screen.cover)
        body = _TG_EMOJI.sub(r"\1", screen.body) if plain else screen.body
        rich = InputRichMessage(
            html=f'<img src="tg://photo?id=cover"/>{body}',
            skip_entity_detection=True,
            media=[InputRichMessageMedia(id="cover", media=InputMediaPhoto(media=media))],
        )
        markup = screen.markup(plain)

        message = None
        if session.get("msg_id") and not fresh:
            try:
                message = await self.bot.edit_message_text(
                    chat_id=uid, message_id=session["msg_id"], rich_message=rich, reply_markup=markup
                )
            except TelegramBadRequest as error:
                if "not modified" in error.message:
                    return
                logger.info("Конструктор: сообщение %s не отредактировать (%s)", session["msg_id"], error.message)

        if message is None:
            message = await self.bot.send_rich_message(chat_id=uid, rich_message=rich, reply_markup=markup)
            old, session["msg_id"] = session.get("msg_id"), message.message_id
            if old and old != message.message_id:
                await self.delete(uid, old)

        if upload_key and isinstance(message, Message) and (file_id := _first_photo_id(message)):
            self._file_ids[upload_key] = file_id

    async def delete(self, uid: int, message_id: int) -> None:
        try:
            await self.bot.delete_message(chat_id=uid, message_id=message_id)
        except TelegramAPIError as error:  # удаление — по возможности, экран важнее
            logger.debug("Конструктор: сообщение %s не удалить: %s", message_id, error)
