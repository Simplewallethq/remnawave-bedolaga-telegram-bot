from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers import start
from app.keyboards import inline


SUBSCRIPTION_LINK = "https://letovpn.com/sub/tPR0MX_oR78DHtga"


def _user(language="ru"):
    return SimpleNamespace(
        telegram_id=42,
        language=language,
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )


def test_onboarding_welcome_keyboard_opens_connect_platform_submenus():
    keyboard = inline.get_onboarding_welcome_keyboard("ru")

    assert [(row[0].text, row[0].callback_data) for row in keyboard.inline_keyboard] == [
        ("🤖 Android", "connect_platform_android"),
        ("🍎 iPhone/MacOS", "connect_platform_apple"),
        ("💻 Windows", "connect_platform_windows"),
        ("🏠 Основное меню", "main_menu"),
    ]


def test_onboarding_welcome_text_has_a_copyable_raw_subscription_key():
    text = start._build_onboarding_welcome_text(_user())

    assert "Привет, тебе доступна бесплатная подписка на 3 дня" in text
    assert "Ключ-ссылка доступа (для Happ, Incy)" in text
    assert f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>" in text


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
        _user(),
        message,
        is_callback=False,
    )

    assert success is True
    assert message.answer_photo.await_args.kwargs["caption"].startswith("Привет, тебе")
    assert message.answer_photo.await_args.args[0].path == "images/connection.jpg"
