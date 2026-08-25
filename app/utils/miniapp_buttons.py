from typing import Optional

from aiogram import types
from aiogram.types import InlineKeyboardButton
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import settings


DEFAULT_UNAVAILABLE_CALLBACK = "menu_profile_unavailable"
_CONNECT_ACTIONS = {"happ", "incy"}


def build_miniapp_or_callback_button(
    text: str,
    *,
    callback_data: str,
    unavailable_callback: str = DEFAULT_UNAVAILABLE_CALLBACK,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    """Create a button that opens the miniapp in text menu mode.

    When the simplified text menu mode is enabled we should avoid exposing
    deep bot flows and redirect the user to the configured miniapp instead.
    If the miniapp URL is missing we fall back to a safe callback that shows
    an alert about the unavailable profile rather than opening disabled
    sections of the bot.
    """

    if settings.is_text_main_menu_mode():
        miniapp_url = settings.get_main_menu_miniapp_url()
        if miniapp_url:
            return InlineKeyboardButton(
                text=text,
                web_app=types.WebAppInfo(url=miniapp_url),
                style=style,
            )
        safe_callback = unavailable_callback or DEFAULT_UNAVAILABLE_CALLBACK
        return InlineKeyboardButton(
            text=text, callback_data=safe_callback, style=style
        )

    return InlineKeyboardButton(
        text=text, callback_data=callback_data, style=style
    )


def build_miniapp_connect_button(
    text: str,
    action: str,
) -> InlineKeyboardButton | None:
    """Build a Mini App button that resolves the user's key after Telegram auth."""
    if action not in _CONNECT_ACTIONS:
        raise ValueError(f"Unsupported Mini App connect action: {action}")

    base_url = (settings.MINIAPP_CUSTOM_URL or "").strip()
    if not base_url:
        return None

    parsed = urlsplit(base_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["connect"] = action
    url = urlunsplit(parsed._replace(query=urlencode(params)))

    return InlineKeyboardButton(
        text=text,
        web_app=types.WebAppInfo(url=url),
    )
