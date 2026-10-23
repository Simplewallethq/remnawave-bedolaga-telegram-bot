"""Конструктор снаружи: кнопки бота-платформы, маршруты веб-сервера, уведомление о выплате."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from app.config import settings
from app.constructor import constructor_runtime
from app.constructor.runtime import URL_PREFIX, webhook_path

PLATFORM_BOT = 4242


@pytest.fixture
def platform_bot(monkeypatch):
    monkeypatch.setattr(settings, "OWNER_PLATFORM_BOT_ID", PLATFORM_BOT, raising=False)
    monkeypatch.setattr(constructor_runtime, "username", "vpn_constructor_bot")


def _buttons(keyboard) -> list:
    return [button for row in keyboard.inline_keyboard for button in row]


def test_platform_buttons_stay_until_redirect_is_enabled(platform_bot, monkeypatch):
    from app.branding import brand_scope
    from app.keyboards.inline import get_new_main_menu_keyboard
    from app.keyboards.owner import owner_menu_keyboard, platform_choice_keyboard

    monkeypatch.setattr(settings, "OWNER_PLATFORM_REDIRECT_TO_CONSTRUCTOR", False, raising=False)
    with brand_scope(PLATFORM_BOT):
        assert _buttons(get_new_main_menu_keyboard(balance_rub=0))[0].callback_data == "wl_entry"
    assert _buttons(platform_choice_keyboard("ru"))[0].callback_data == "wl_choice_create"
    assert [b.callback_data for b in _buttons(owner_menu_keyboard("ru"))][:2] == ["wl_settings", "wl_earn"]

    monkeypatch.setattr(settings, "OWNER_PLATFORM_REDIRECT_TO_CONSTRUCTOR", True, raising=False)
    link = "https://t.me/vpn_constructor_bot"
    with brand_scope(PLATFORM_BOT):
        assert _buttons(get_new_main_menu_keyboard(balance_rub=0))[0].url == link
    assert _buttons(platform_choice_keyboard("ru"))[0].url == link
    owner_menu = _buttons(owner_menu_keyboard("ru"))
    assert owner_menu[0].url == link and owner_menu[1].callback_data == "wl_try_vpn"


def test_redirect_needs_a_running_constructor(platform_bot, monkeypatch):
    from app.keyboards.owner import platform_choice_keyboard

    monkeypatch.setattr(settings, "OWNER_PLATFORM_REDIRECT_TO_CONSTRUCTOR", True, raising=False)
    monkeypatch.setattr(constructor_runtime, "username", None)  # конструктор не поднялся
    assert _buttons(platform_choice_keyboard("ru"))[0].callback_data == "wl_choice_create"


def test_mount_registers_webhook_api_and_static(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OWNER_PLATFORM_BOT_ID", PLATFORM_BOT, raising=False)
    monkeypatch.setattr(settings, "CONSTRUCTOR_BOT_TOKEN", "555000:CCH" + "z" * 32, raising=False)
    (tmp_path / "index.html").write_text("<html></html>")
    monkeypatch.setattr(settings, "CONSTRUCTOR_WEBAPP_STATIC_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(constructor_runtime, "bot", SimpleNamespace(id=555000))
    monkeypatch.setattr(constructor_runtime, "dispatcher", SimpleNamespace())

    app = FastAPI()
    constructor_runtime.mount(app, enable_webhook=True)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert webhook_path() in paths
    assert f"{URL_PREFIX}/api/state" in paths and f"{URL_PREFIX}/api/bots" in paths
    assert URL_PREFIX in paths  # статика мини-аппа
    assert "/health/telegram-webhook" not in paths  # не дублируем health основного бота


def test_mount_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "CONSTRUCTOR_BOT_TOKEN", "", raising=False)
    app = FastAPI()
    constructor_runtime.mount(app, enable_webhook=True)
    assert not any(str(getattr(route, "path", "")).startswith(URL_PREFIX) for route in app.routes)


@pytest.mark.anyio
async def test_payout_notice_falls_back_to_platform_bot(platform_bot, monkeypatch):
    """Владелец не открывал конструктор — о выплате узнаёт в боте-платформе."""
    from decimal import Decimal

    from app.handlers.owner_platform import earnings
    from app.services import owner_platform_service as platform

    payout = SimpleNamespace(id=7, amount_usdt=Decimal("10"), fee_usdt=Decimal("3"))
    recipient = SimpleNamespace(telegram_id=111)

    class FakeSession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.database.database.AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(platform, "process_payout", AsyncMock(return_value=payout))
    monkeypatch.setattr(platform, "payout_recipient", AsyncMock(return_value=recipient))
    monkeypatch.setattr(type(settings), "is_admin", lambda self, user_id: True)

    constructor_bot = SimpleNamespace(id=555000, send_message=AsyncMock(side_effect=RuntimeError("bot was blocked")))
    platform_sender = SimpleNamespace(id=PLATFORM_BOT, send_message=AsyncMock())
    monkeypatch.setattr(constructor_runtime, "bot", constructor_bot)
    monkeypatch.setattr("app.utils.bot_registry.get_bot_instance", lambda bot_id: platform_sender)

    callback = SimpleNamespace(
        data="admin_owner_payout_paid_7",
        from_user=SimpleNamespace(id=1, username="admin"),
        message=SimpleNamespace(html_text="заявка", edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await earnings.admin_process_payout(callback)
    constructor_bot.send_message.assert_awaited_once()
    platform_sender.send_message.assert_awaited_once_with(111, "💸 Заявка №7 на вывод 7 USDT выплачена.")
