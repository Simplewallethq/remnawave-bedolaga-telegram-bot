from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import start
from app.keyboards import inline


SUBSCRIPTION_LINK = "https://letovpn.com/sub/tPR0MX_oR78DHtga"


def _user(language="ru"):
    return SimpleNamespace(
        telegram_id=42,
        language=language,
        subscription=SimpleNamespace(id=7, subscription_url=SUBSCRIPTION_LINK),
    )


@pytest.fixture(autouse=True)
def binding_code(monkeypatch):
    import app.database.crud.device_binding_code as binding_module

    monkeypatch.setattr(
        binding_module,
        "get_or_create_binding_code",
        AsyncMock(
            return_value=SimpleNamespace(
                code="APPLETESTQWZX003",
                expires_at=datetime.utcnow() + timedelta(hours=24),
            )
        ),
    )


def test_onboarding_welcome_keyboard_opens_connect_platform_submenus():
    keyboard = inline.get_onboarding_welcome_keyboard("ru")

    assert [(row[0].text, row[0].callback_data) for row in keyboard.inline_keyboard] == [
        ("🤖 Android", "connect_platform_android"),
        ("🍎 iPhone/MacOS", "connect_platform_apple"),
        ("💻 Windows", "connect_platform_windows"),
        ("🏠 Основное меню", "main_menu"),
    ]


async def test_onboarding_welcome_text_has_copyable_happ_and_leto_keys(monkeypatch):
    text = await start._build_onboarding_welcome_text(AsyncMock(), _user())

    assert "Привет, тебе доступна бесплатная подписка на 3 дня" in text
    assert "Ключ доступа для Happ, Incy:" in text
    assert f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>" in text
    assert "Ключ доступа для Leto VPN на Android:" in text
    assert "<pre><code>APPLETESTQWZX003</code></pre>" in text


async def test_trial_activation_shows_onboarding_welcome(monkeypatch):
    user = _user()
    render = AsyncMock()
    monkeypatch.setattr(start, "activate_trial_for_user", AsyncMock(return_value=True))
    monkeypatch.setattr(start, "edit_or_answer_photo", render)

    success = await start._auto_activate_trial_and_show_device_selection(
        AsyncMock(),
        AsyncMock(),
        user,
        SimpleNamespace(),
        is_callback=True,
    )

    assert success is True
    assert SUBSCRIPTION_LINK in render.await_args.kwargs["caption"]
    assert render.await_args.kwargs["keyboard"].inline_keyboard[0][0].callback_data == (
        "connect_platform_android"
    )
    assert render.await_args.kwargs["photo_path"] == "images/connection.jpg"


async def test_partner_activation_shows_onboarding_welcome(monkeypatch):
    user = _user("en")
    render = AsyncMock()
    monkeypatch.setattr(
        start,
        "activate_partner_subscription_for_user",
        AsyncMock(return_value=(True, "activated")),
    )
    monkeypatch.setattr(start, "edit_or_answer_photo", render)

    success = await start._auto_activate_partner_and_show_device_selection(
        AsyncMock(),
        AsyncMock(),
        user,
        SimpleNamespace(),
        sub_until=SimpleNamespace(),
        jti="token-id",
        is_callback=True,
    )

    assert success is True
    assert "Hi, you have a free 3-day subscription" in render.await_args.kwargs["caption"]
    assert render.await_args.kwargs["keyboard"].inline_keyboard[-1][0].callback_data == "main_menu"
    assert render.await_args.kwargs["photo_path"] == "images/connection.jpg"


async def test_onboarding_welcome_sends_connection_image_for_new_registration():
    message = AsyncMock()

    success = await start._show_onboarding_welcome(
        AsyncMock(),
        _user(),
        message,
        is_callback=False,
    )

    assert success is True
    assert message.answer_photo.await_args.kwargs["caption"].startswith("Привет, тебе")
    assert message.answer_photo.await_args.args[0].path == "images/connection.jpg"


async def test_existing_device_link_sends_russian_confirmation_with_connection_image(
    monkeypatch,
):
    subscription = SimpleNamespace(id=7)
    existing_link = SimpleNamespace(subscription_id=7, revoked_at=SimpleNamespace())
    db = AsyncMock()
    message = AsyncMock()
    monkeypatch.setattr(start, "get_device_link", AsyncMock(return_value=existing_link))

    await start._link_device_to_subscription(db, subscription, "device-42", message)

    assert existing_link.revoked_at is None
    db.commit.assert_awaited_once()
    message.answer.assert_not_awaited()
    message.answer_photo.assert_awaited_once()
    assert message.answer_photo.await_args.args[0].path == "images/connection.jpg"
    assert message.answer_photo.await_args.kwargs["caption"] == (
        "Устройство подключено к вашей подписке, вернитесь обратно."
    )
