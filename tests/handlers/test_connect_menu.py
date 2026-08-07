from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers import menu
from app.keyboards import inline


SUBSCRIPTION_LINK = "https://letovpn.com/sub/tPR0MX_oR78DHtga"


def _button_rows(keyboard):
    return [row for row in keyboard.inline_keyboard]


def test_connect_platform_keyboard_has_requested_order():
    keyboard = inline.get_connection_platform_keyboard("ru")

    assert [(row[0].text, row[0].callback_data) for row in _button_rows(keyboard)] == [
        ("🤖 Android", "connect_platform_android"),
        ("🍎 iPhone/MacOS", "connect_platform_apple"),
        ("💻 Windows", "connect_platform_windows"),
        ("🔗 Поделиться доступом", "connect_share_access"),
        ("🏠 Основное меню", "main_menu"),
    ]


def test_connect_menu_has_english_text_and_buttons():
    user = SimpleNamespace(
        language="en",
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    keyboard = inline.get_connection_platform_keyboard("en")
    text = menu._build_connect_platform_selection_text(user)

    assert "To connect your primary or additional device" in text
    assert "Access key link (for Leto, Happ, Incy)" in text
    assert [(row[0].text, row[0].callback_data) for row in _button_rows(keyboard)] == [
        ("🤖 Android", "connect_platform_android"),
        ("🍎 iPhone/MacOS", "connect_platform_apple"),
        ("💻 Windows", "connect_platform_windows"),
        ("🔗 Share access", "connect_share_access"),
        ("🏠 Main menu", "main_menu"),
    ]


def test_connect_android_keyboard_only_has_leto_and_back(monkeypatch):
    monkeypatch.setattr(
        inline,
        "build_personal_play_link",
        lambda _url, _telegram_id: "https://play.google.com/store/apps/details?id=leto",
    )

    keyboard = inline.get_connect_android_keyboard("ru", telegram_id=42)
    rows = _button_rows(keyboard)

    assert [(row[0].text, row[0].url, row[0].callback_data) for row in rows] == [
        ("☀️ Скачать Leto VPN", "https://play.google.com/store/apps/details?id=leto", None),
        ("⬅️ Назад", None, "howto"),
    ]


def test_connect_apple_and_windows_keyboards_have_only_requested_actions():
    apple_rows = _button_rows(inline.get_connect_apple_keyboard("ru"))
    windows_rows = _button_rows(inline.get_connect_windows_keyboard("ru"))

    assert [(row[0].text, row[0].callback_data) for row in apple_rows] == [
        ("🍏 Скачать Incy (RU App Store)", None),
        ("🍎 Скачать Happ (Int. App Store)", None),
        ("⬅️ Назад", "howto"),
    ]
    assert apple_rows[1][0].url == "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
    assert [(row[0].text, row[0].callback_data) for row in windows_rows] == [
        ("💻 Скачать Happ", None),
        ("⬅️ Назад", "howto"),
    ]
    assert windows_rows[0][0].url == (
        "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"
    )


def test_connection_key_is_a_copyable_code_block():
    assert menu._format_connection_key(SUBSCRIPTION_LINK) == (
        f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>"
    )


async def test_connect_platform_handler_shows_raw_subscription_link(monkeypatch):
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    render = AsyncMock()
    monkeypatch.setattr(menu, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(menu, "edit_or_answer_photo", render)
    monkeypatch.setattr(
        inline,
        "build_personal_play_link",
        lambda _url, _telegram_id: "https://play.google.com/store/apps/details?id=leto",
    )

    await menu.handle_connect_platform_android(callback, AsyncMock())

    rendered_text = render.await_args.args[1]
    rendered_keyboard = render.await_args.args[2]
    assert "авторизуйся через ключ-ссылку" in rendered_text
    assert SUBSCRIPTION_LINK in rendered_text
    assert rendered_keyboard.inline_keyboard[0][0].text == "☀️ Скачать Leto VPN"
    assert render.await_args.kwargs["photo_path"] == "images/connection.jpg"
    callback.answer.assert_awaited_once()


async def test_connect_menu_main_screen_shows_universal_raw_key(monkeypatch):
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    render = AsyncMock()
    monkeypatch.setattr(menu, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(menu, "edit_or_answer_photo", render)

    await menu.handle_howto(callback, AsyncMock(), AsyncMock())

    rendered_text = render.await_args.args[1]
    rendered_keyboard = render.await_args.args[2]
    assert "Для подключения основного или дополнительного устройства" in rendered_text
    assert SUBSCRIPTION_LINK in rendered_text
    assert rendered_keyboard.inline_keyboard[0][0].callback_data == "connect_platform_android"
    assert render.await_args.kwargs["photo_path"] == "images/connection.jpg"
    callback.answer.assert_awaited_once()


async def test_connect_platform_submenus_use_connection_image(monkeypatch):
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    render = AsyncMock()
    monkeypatch.setattr(menu, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(menu, "edit_or_answer_photo", render)
    monkeypatch.setattr(
        inline,
        "build_personal_play_link",
        lambda _url, _telegram_id: "https://play.google.com/store/apps/details?id=leto",
    )

    for handler in (
        menu.handle_connect_platform_android,
        menu.handle_connect_platform_apple,
        menu.handle_connect_platform_windows,
    ):
        await handler(callback, AsyncMock())

    assert render.await_count == 3
    for call in render.await_args_list:
        assert call.kwargs["photo_path"] == "images/connection.jpg"
