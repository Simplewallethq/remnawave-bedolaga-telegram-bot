"""Экран розыгрыша: отметки, зелёные кнопки, алерты и красная кнопка меню."""

from __future__ import annotations

import html
import itertools
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest

from app.config import settings
from app.handlers.giveaway import (
    CHECK_KEYS,
    CHECK_PREFIX,
    GIVEAWAY_IMAGE,
    _render,
    build_giveaway_keyboard,
    build_giveaway_text,
    check_result_text,
)
from app.keyboards.inline import get_giveaway_menu_row
from app.services.giveaway_service import GiveawayProgress, giveaway_service

ROOT_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


EMPTY = GiveawayProgress(channel_subscribed=False, invited_count=0, invited_paid_count=0, plan_code=None)
FULL = GiveawayProgress(channel_subscribed=True, invited_count=10, invited_paid_count=3, plan_code="pro")


def _buttons(progress: GiveawayProgress) -> dict:
    keyboard = build_giveaway_keyboard(progress)
    return {
        button.callback_data.removeprefix(CHECK_PREFIX): button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith(CHECK_PREFIX)
    }


def test_every_condition_has_check_button() -> None:
    assert set(_buttons(EMPTY)) == CHECK_KEYS


def test_done_conditions_are_green() -> None:
    buttons = _buttons(FULL)
    green = {key for key, button in buttons.items() if button.style == ButtonStyle.SUCCESS}
    assert green == CHECK_KEYS - {"plan_solo", "plan_plus"}
    assert all(button.style is None for button in _buttons(EMPTY).values())


def test_channel_row_offers_subscribe_link_until_done() -> None:
    def urls(progress):
        return [
            button.url
            for row in build_giveaway_keyboard(progress).inline_keyboard
            for button in row
            if button.url
        ]

    assert urls(EMPTY) == [settings.GIVEAWAY_CHANNEL_URL]
    assert urls(FULL) == []


def test_text_shows_total_and_marks() -> None:
    text = build_giveaway_text(FULL, "https://t.me/bot?start=abc")

    assert f"Ваши билеты: {FULL.total_tickets}" in text
    assert "https://t.me/bot?start=abc" in text
    assert text.count("✅") == 6  # канал, три порога друзей, Pro, оплата друзей
    assert "<blockquote expandable>" in text


@pytest.mark.parametrize("progress", [EMPTY, FULL, GiveawayProgress(None, 999, 999, "plus")])
def test_text_fits_photo_caption(progress) -> None:
    # Экран — фото с подписью; Telegram считает лимит 1024 по видимому тексту.
    text = build_giveaway_text(progress, "https://t.me/LetoVPN_official_bot?start=ref_ABCDEFGH12")
    visible = html.unescape(re.sub(r"<[^>]+>", "", text))

    assert len(visible) <= 1024, len(visible)


def test_missing_friends_alert_counts_what_is_left() -> None:
    progress = GiveawayProgress(True, 4, 0, None)

    assert "Пригласите ещё 2 друзей" in check_result_text("invite6", progress)
    assert "Пригласите ещё 5 друзей" in check_result_text("invite9", progress)
    assert "1 друга" in check_result_text("invite3", GiveawayProgress(True, 2, 0, None))
    assert check_result_text("invite3", progress).startswith("✅")


def test_other_plan_alert_names_current_plan() -> None:
    progress = GiveawayProgress(True, 0, 0, "plus")

    assert "У вас активна подписка Plus" in check_result_text("plan_pro", progress)
    assert check_result_text("plan_plus", progress).startswith("✅")
    assert "Пробный период не считается" in check_result_text("plan_pro", EMPTY)


def test_channel_alert_distinguishes_unknown() -> None:
    assert check_result_text("channel", FULL).startswith("✅")
    assert "не подписаны" in check_result_text("channel", EMPTY)
    unknown = GiveawayProgress(None, 0, 0, None)
    assert "Не удалось проверить" in check_result_text("channel", unknown)


@pytest.mark.parametrize(
    "progress",
    [
        GiveawayProgress(channel, invited, paid, plan)
        for channel, invited, paid, plan in itertools.product(
            (True, False, None), (0, 1, 4, 99), (0, 1, 21), (None, "solo", "pro")
        )
    ],
)
def test_alerts_fit_telegram_limit(progress) -> None:
    for key in CHECK_KEYS:
        assert len(check_result_text(key, progress)) <= 200, key


def _callback(*, photo: bool, edit_error: Exception | None = None):
    message = SimpleNamespace(
        photo=[object()] if photo else None,
        edit_caption=AsyncMock(side_effect=edit_error),
        edit_media=AsyncMock(side_effect=edit_error),
        delete=AsyncMock(),
    )
    bot = SimpleNamespace(send_photo=AsyncMock())
    return SimpleNamespace(message=message, bot=bot, from_user=SimpleNamespace(id=7))


def test_giveaway_image_is_shipped() -> None:
    assert Path(ROOT_DIR, GIVEAWAY_IMAGE).is_file()


@pytest.mark.anyio
async def test_check_on_screen_only_edits_caption() -> None:
    callback = _callback(photo=True)

    await _render(callback, "text", build_giveaway_keyboard(EMPTY), on_screen=True)

    callback.message.edit_caption.assert_awaited_once()
    callback.message.edit_media.assert_not_awaited()
    callback.bot.send_photo.assert_not_awaited()


@pytest.mark.anyio
async def test_open_from_photo_menu_swaps_picture() -> None:
    callback = _callback(photo=True)

    await _render(callback, "text", build_giveaway_keyboard(EMPTY), on_screen=False)

    media = callback.message.edit_media.await_args.args[0]
    assert str(media.media.path).endswith("giveaway.webp")
    assert media.caption == "text"


@pytest.mark.anyio
async def test_open_from_text_menu_sends_photo() -> None:
    callback = _callback(photo=False)

    await _render(callback, "text", build_giveaway_keyboard(EMPTY), on_screen=False)

    callback.message.delete.assert_awaited_once()
    assert callback.bot.send_photo.await_args.kwargs["caption"] == "text"


@pytest.mark.anyio
async def test_unchanged_screen_is_not_resent() -> None:
    error = TelegramBadRequest(method=None, message="Bad Request: message is not modified")
    callback = _callback(photo=True, edit_error=error)

    await _render(callback, "text", build_giveaway_keyboard(EMPTY), on_screen=True)

    callback.message.delete.assert_not_awaited()
    callback.bot.send_photo.assert_not_awaited()


def test_menu_row_is_red_while_running(monkeypatch) -> None:
    monkeypatch.setattr(giveaway_service.__class__, "is_running", lambda self, now_utc=None: True)

    row = get_giveaway_menu_row()

    assert row is not None
    assert row[0].style == ButtonStyle.DANGER
    assert row[0].callback_data == "giveaway_menu"


def test_menu_row_hidden_outside_window(monkeypatch) -> None:
    monkeypatch.setattr(giveaway_service.__class__, "is_running", lambda self, now_utc=None: False)

    assert get_giveaway_menu_row() is None
