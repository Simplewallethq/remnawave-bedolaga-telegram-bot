from datetime import datetime, timedelta
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
        ("🔗 Переслать друзьям", "connect_share_access"),
        ("🏠 Основное меню", "main_menu"),
    ]


async def test_connect_menu_has_english_text_and_buttons():
    user = SimpleNamespace(
        language="en",
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    keyboard = inline.get_connection_platform_keyboard("en")
    text = await menu._build_connect_platform_selection_text(AsyncMock(), user)

    assert "Choose a platform to connect:" in text
    assert "Your access key" in text
    assert [(row[0].text, row[0].callback_data) for row in _button_rows(keyboard)] == [
        ("🤖 Android", "connect_platform_android"),
        ("🍎 iPhone/MacOS", "connect_platform_apple"),
        ("💻 Windows", "connect_platform_windows"),
        ("🔗 Send to friends", "connect_share_access"),
        ("🏠 Main menu", "main_menu"),
    ]


def test_connection_copy_is_available_in_every_supported_locale():
    expected_happ_hints = {
        "ru": "Если у тебя есть Happ",
        "en": "If you use Happ",
        "ua": "Якщо користуєшся Happ",
        "zh": "如果您使用 Happ",
    }

    for language, hint in expected_happ_hints.items():
        texts = menu.get_texts(language)

        assert hint in texts.t("CONNECT_ANDROID_HAPP_HINT")
        assert "{ttl_hours}" in texts.t("CONNECT_LETO_CODE_LABEL")


def test_connect_android_keyboard_omits_transfer_without_url(monkeypatch):
    monkeypatch.setattr(inline.settings, "MINIAPP_CUSTOM_URL", "")
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


def test_connect_apple_and_windows_keyboards_omit_transfers_without_urls(monkeypatch):
    monkeypatch.setattr(inline.settings, "MINIAPP_CUSTOM_URL", "")
    apple_rows = _button_rows(inline.get_connect_apple_keyboard("ru"))
    windows_rows = _button_rows(inline.get_connect_windows_keyboard("ru"))

    assert [(row[0].text, row[0].callback_data) for row in apple_rows] == [
        ("🍏 Скачать Incy", None),
        ("🍎 Скачать Happ", None),
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


def test_connect_platform_keyboards_include_transfer_buttons(monkeypatch):
    monkeypatch.setattr(
        inline,
        "build_personal_play_link",
        lambda _url, _telegram_id: "https://play.google.com/store/apps/details?id=leto",
    )
    happ_url = "https://redirect.example/happ"
    incy_url = "https://redirect.example/incy"

    android_rows = _button_rows(
        inline.get_connect_android_keyboard("ru", telegram_id=42, happ_transfer_url=happ_url)
    )
    apple_rows = _button_rows(
        inline.get_connect_apple_keyboard(
            "ru",
            incy_transfer_url=incy_url,
            happ_transfer_url=happ_url,
        )
    )
    windows_rows = _button_rows(
        inline.get_connect_windows_keyboard("ru", happ_transfer_url=happ_url)
    )

    assert [(row[0].text, row[0].url) for row in android_rows] == [
        ("☀️ Скачать Leto VPN", "https://play.google.com/store/apps/details?id=leto"),
        ("➡️ Ключ в Happ", happ_url),
        ("⬅️ Назад", None),
    ]
    assert [[(button.text, button.url) for button in row] for row in apple_rows] == [
        [
            ("🍏 Скачать Incy", inline.settings.get_incy_download_link()),
            ("➡️ Ключ в Incy", incy_url),
        ],
        [
            ("🍎 Скачать Happ", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
            ("➡️ Ключ в Happ", happ_url),
        ],
        [("⬅️ Назад", None)],
    ]
    assert [(row[0].text, row[0].url) for row in windows_rows] == [
        ("💻 Скачать Happ", "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"),
        ("➡️ Ключ в Happ", happ_url),
        ("⬅️ Назад", None),
    ]


async def test_connect_menu_shows_16_char_binding_code(monkeypatch):
    """В меню подключения показываем тот же 16-значный код, что и «Привязать
    устройство»: им авторизуются в Leto и в личном кабинете."""
    import app.database.crud.device_binding_code as binding_module

    record = SimpleNamespace(
        code="A2B3C4D5E6F7G8H9",
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    create = AsyncMock(return_value=record)
    monkeypatch.setattr(binding_module, "get_or_create_binding_code", create)
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        subscription=SimpleNamespace(id=7, subscription_url=SUBSCRIPTION_LINK),
    )

    text = await menu._build_connect_platform_selection_text(AsyncMock(), user)

    assert "<b>Ключ для входа в Leto VPN</b> (действует 23 ч):" in text
    assert "<pre><code>A2B3C4D5E6F7G8H9</code></pre>" in text
    assert create.await_args.args[1] == 7


async def test_connect_menu_hides_code_block_when_generation_fails(monkeypatch):
    """Код не выдался — экран подключения всё равно открывается с ключом-ссылкой."""
    import app.database.crud.device_binding_code as binding_module

    monkeypatch.setattr(
        binding_module,
        "get_or_create_binding_code",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        subscription=SimpleNamespace(id=7, subscription_url=SUBSCRIPTION_LINK),
    )

    text = await menu._build_connect_platform_selection_text(AsyncMock(), user)

    assert "Ключ для входа в Leto" not in text
    assert SUBSCRIPTION_LINK in text


def test_connection_key_is_a_copyable_code_block():
    assert menu._format_connection_key(SUBSCRIPTION_LINK) == (
        f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>"
    )


async def test_connect_platform_handler_shows_raw_subscription_link(monkeypatch):
    monkeypatch.setattr(inline.settings, "MINIAPP_CUSTOM_URL", "")
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
    assert "авторизуйся через Telegram или с помощью ключа доступа" in rendered_text
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
    assert "<b>Твой ключ доступа</b> (для приложений Leto, Happ, Incy)" in rendered_text
    assert "Выбери платформу для подключения:" in rendered_text
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
    monkeypatch.setattr(menu, "get_happ_cryptolink_redirect_link", lambda _link: "https://redirect.example/happ")
    monkeypatch.setattr(menu, "get_incy_button_url", lambda _link: "https://redirect.example/incy")

    for handler in (
        menu.handle_connect_platform_android,
        menu.handle_connect_platform_apple,
        menu.handle_connect_platform_windows,
    ):
        await handler(callback, AsyncMock())

    assert render.await_count == 3
    for call in render.await_args_list:
        assert call.kwargs["photo_path"] == "images/connection.jpg"

    assert render.await_args_list[0].args[2].inline_keyboard[1][0].url == "https://redirect.example/happ"
    assert render.await_args_list[1].args[2].inline_keyboard[0][1].url == "https://redirect.example/incy"
    assert render.await_args_list[1].args[2].inline_keyboard[1][1].url == "https://redirect.example/happ"
    assert render.await_args_list[2].args[2].inline_keyboard[1][0].url == "https://redirect.example/happ"


async def test_connect_menu_detects_first_vpn_connection(monkeypatch):
    """Меню «Подключиться» работает на локальном ключе и панель не трогает,
    поэтому факт первого подключения тянем здесь — иначе has_connected_to_vpn
    не выставится ни у кого."""
    import app.services.subscription_service as subscription_module

    sync = AsyncMock(return_value=True)
    monkeypatch.setattr(
        subscription_module,
        "SubscriptionService",
        lambda: SimpleNamespace(is_configured=True, sync_subscription_usage=sync),
    )
    callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    subscription = SimpleNamespace(subscription_url=SUBSCRIPTION_LINK)
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        has_connected_to_vpn=False,
        subscription=subscription,
    )
    db = AsyncMock()
    monkeypatch.setattr(menu, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(menu, "edit_or_answer_photo", AsyncMock())

    await menu.handle_howto(callback, AsyncMock(), db)

    sync.assert_awaited_once()
    assert sync.await_args.args[1] is subscription


async def test_connect_menu_skips_detection_for_connected_user(monkeypatch):
    import app.services.subscription_service as subscription_module

    sync = AsyncMock(return_value=True)
    monkeypatch.setattr(
        subscription_module,
        "SubscriptionService",
        lambda: SimpleNamespace(is_configured=True, sync_subscription_usage=sync),
    )
    callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        has_connected_to_vpn=True,
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    monkeypatch.setattr(menu, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(menu, "edit_or_answer_photo", AsyncMock())

    await menu.handle_howto(callback, AsyncMock(), AsyncMock())

    sync.assert_not_awaited()


async def test_connect_menu_survives_panel_failure(monkeypatch):
    """Панель недоступна — экран подключения всё равно должен открыться."""
    import app.services.subscription_service as subscription_module

    monkeypatch.setattr(
        subscription_module,
        "SubscriptionService",
        lambda: SimpleNamespace(
            is_configured=True,
            sync_subscription_usage=AsyncMock(side_effect=RuntimeError("panel down")),
        ),
    )
    callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    user = SimpleNamespace(
        language="ru",
        telegram_id=42,
        has_connected_to_vpn=False,
        subscription=SimpleNamespace(subscription_url=SUBSCRIPTION_LINK),
    )
    render = AsyncMock()
    monkeypatch.setattr(menu, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(menu, "edit_or_answer_photo", render)

    await menu.handle_howto(callback, AsyncMock(), AsyncMock())

    render.assert_awaited_once()
    callback.answer.assert_awaited_once()
