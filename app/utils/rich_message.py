"""Экраны на rich-разметке Bot API 10.1 (sendRichMessage / editMessageText).

RichScreen собирает экран из блоков и рендерит его в двух вариантах:
  - rich HTML (InputRichMessage.html) — параграфы, заголовки, разделители,
    списки, details, pullquote, баннер-картинка;
  - классический Telegram-HTML — фолбэк, если rich-сообщение не удалось
    отправить (старый Bot API server зеркального бота, ошибка Telegram и т.п.).

Инлайновая разметка внутри блоков (<b>, <i>, <a href>, <code>) передаётся
как есть — она валидна в обоих режимах. Значения из БД экранируйте
html.escape() на стороне вызывающего.
"""

import logging

from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputRichMessage

logger = logging.getLogger(__name__)


class RichScreen:
    def __init__(self):
        self._rich: list[str] = []
        self._plain: list[str] = []

    def banner(self, url: str | None) -> "RichScreen":
        """Картинка-баннер сверху экрана. Rich-медиа принимает только HTTP(S)
        URL, поэтому без настроенного URL блок просто не добавляется."""
        if url:
            self._rich.append(f'<img src="{url}"/>')
        return self

    def pullquote(self, html: str) -> "RichScreen":
        self._rich.append(f"<aside>{html}</aside>")
        self._plain.append(f"<blockquote>{html}</blockquote>")
        return self

    def paragraph(self, html: str) -> "RichScreen":
        self._rich.append(f"<p>{html}</p>")
        self._plain.append(html)
        return self

    def heading(self, html: str, size: int = 3) -> "RichScreen":
        size = min(max(size, 1), 6)
        self._rich.append(f"<h{size}>{html}</h{size}>")
        self._plain.append(f"<b>{html}</b>")
        return self

    def divider(self) -> "RichScreen":
        self._rich.append("<hr/>")
        self._plain.append("—————————")
        return self

    def bullet_list(self, items: list[str]) -> "RichScreen":
        lis = "".join(f"<li>{item}</li>" for item in items)
        self._rich.append(f"<ul>{lis}</ul>")
        self._plain.extend(f"• {item}" for item in items)
        return self

    def details(self, summary_html: str, items: list[str]) -> "RichScreen":
        """Разворачиваемый блок: в rich — <details> со списком, в фолбэке —
        expandable blockquote."""
        lis = "".join(f"<li>{item}</li>" for item in items)
        self._rich.append(
            f"<details><summary>{summary_html}</summary><ul>{lis}</ul></details>"
        )
        inner = "\n".join(f"• {item}" for item in items)
        self._plain.append(
            f"<blockquote expandable><b>{summary_html}</b>\n{inner}</blockquote>"
        )
        return self

    def to_rich_html(self) -> str:
        return "".join(self._rich)

    def to_plain_html(self) -> str:
        return "\n".join(self._plain)


async def _answer_plain(
    message: types.Message,
    screen: RichScreen,
    keyboard: types.InlineKeyboardMarkup | None,
) -> None:
    await message.answer(
        screen.to_plain_html(),
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def show_rich_screen(
    callback: types.CallbackQuery,
    screen: RichScreen,
    keyboard: types.InlineKeyboardMarkup | None = None,
) -> None:
    """Показывает rich-экран поверх текущего сообщения callback'а.

    Текстовое/rich-сообщение редактируется на месте; медиа-сообщение
    (старые фото-экраны) удаляется и заменяется новым. Любая ошибка
    rich-пути деградирует в обычное HTML-сообщение.
    """
    message = callback.message
    rich = InputRichMessage(html=screen.to_rich_html())

    if message.photo is None:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                rich_message=rich,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
            logger.debug("Не удалось отредактировать rich-сообщение: %s", error)

    try:
        await message.delete()
    except Exception:
        pass

    try:
        await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=rich,
            reply_markup=keyboard,
        )
    except Exception as error:
        logger.warning("Rich-сообщение не отправлено, фолбэк на HTML: %s", error)
        await _answer_plain(message, screen, keyboard)
