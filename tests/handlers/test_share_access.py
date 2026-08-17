from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
from urllib.parse import unquote

import pytest

from app.database.crud import share_token
from app.handlers.balance import platega as balance_platega
from app.handlers.subscription import purchase, tariffs
from app.keyboards import inline
from app.localization.texts import get_texts
from app.services import payment_service as payment_service_module


SUBSCRIPTION_LINK = "https://letovpn.com/sub/private-subscription-key"
LETO_ACCESS_CODE = "A2B3C4D5E6F7G8H9"
ACCESS_KEY_SECTION = (
    "<b>Ключ доступа:</b>\n"
    f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>\n\n"
    "Ключ для входа в Leto VPN\n"
    f"<pre><code>{LETO_ACCESS_CODE}</code></pre>"
)
FORWARDED_ACCESS_KEY_SECTION = (
    "Ключ доступа:\n"
    f"{SUBSCRIPTION_LINK}\n\n"
    "Ключ для входа в Leto VPN\n"
    f"{LETO_ACCESS_CODE}"
)
APP_LINKS = {
    "android": "https://play.google.com/store/apps/details?id=com.leto.split",
    "apple": "https://apps.apple.com/ru/app/incy/id6756943388",
    "windows": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
}


def test_share_access_text_uses_copyable_keys_and_application_links():
    text = purchase._build_share_access_text(
        get_texts("ru"), ACCESS_KEY_SECTION, APP_LINKS
    )

    assert f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>" in text
    assert f"<pre><code>{LETO_ACCESS_CODE}</code></pre>" in text
    assert "<b>🤖 Android</b>" in text
    assert "<b>🍎 iPhone/Mac</b>" in text
    assert "<b>💻 Windows</b>" in text
    for url in APP_LINKS.values():
        assert f"<pre><code>{url}</code></pre>" in text


def test_share_access_friend_message_uses_direct_links_and_plain_keys():
    text = purchase._build_share_access_friend_message(
        get_texts("ru"), FORWARDED_ACCESS_KEY_SECTION, APP_LINKS
    )

    assert SUBSCRIPTION_LINK in text
    assert "авторизуйся через ключ доступа" in text
    assert LETO_ACCESS_CODE in text
    assert "<pre><code>" not in text
    for url in APP_LINKS.values():
        assert url in text
        assert f"<pre><code>{url}</code></pre>" not in text


def test_share_access_uses_dynamic_links_in_every_supported_locale():
    for language in ("ru", "en", "ua", "zh"):
        texts = get_texts(language)
        screen_text = purchase._build_share_access_text(
            texts, ACCESS_KEY_SECTION, APP_LINKS
        )
        friend_text = purchase._build_share_access_friend_message(
            texts, FORWARDED_ACCESS_KEY_SECTION, APP_LINKS
        )

        assert SUBSCRIPTION_LINK in screen_text
        assert SUBSCRIPTION_LINK in friend_text
        for url in APP_LINKS.values():
            assert f"<pre><code>{url}</code></pre>" in screen_text
            assert url in friend_text


def test_subscription_management_keeps_platega_autopayment_but_hides_autorenewal_and_devices():
    keyboard = inline.get_subscription_menu_keyboard("ru", username="fake_me_x")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert keyboard.inline_keyboard[1][0].callback_data == "subscription_platega_autopay"
    assert keyboard.inline_keyboard[2][0].callback_data == "sub_add_days"
    assert keyboard.inline_keyboard[3][0].callback_data == "subscription_change_tariff"
    assert keyboard.inline_keyboard[-1][0].callback_data == "main_menu"
    assert "share_access" not in callbacks
    assert "subscription_autopay" not in callbacks
    assert "subscription_manage_devices" not in callbacks


def test_subscription_management_hides_platega_autopayment_for_non_test_user():
    keyboard = inline.get_subscription_menu_keyboard("ru", username="unrelated_user")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "subscription_platega_autopay" not in callbacks


def test_profile_keyboard_no_longer_contains_devices():
    keyboard = inline.get_profile_keyboard("ru", has_subscription=True)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "subscription_manage_devices" not in callbacks


def test_platega_autopay_keyboard_changes_action_for_subscription_status():
    inactive_keyboard = inline.get_platega_autopay_keyboard("ru")
    active_keyboard = inline.get_platega_autopay_keyboard(
        "ru", has_active_subscription=True
    )

    assert inactive_keyboard.inline_keyboard[0][0].text == "➕ Подключить автоплатеж"
    assert (
        inactive_keyboard.inline_keyboard[0][0].callback_data
        == "subscription_platega_autopay_connect"
    )
    assert active_keyboard.inline_keyboard[0][0].text == "Отменить автоплатеж"
    assert (
        active_keyboard.inline_keyboard[0][0].callback_data
        == "subscription_platega_autopay_cancel"
    )
    assert inactive_keyboard.inline_keyboard[-1][0].callback_data == "subscription"


def test_balance_topup_keyboard_contains_only_platega_options(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(inline.settings, "TELEGRAM_STARS_ENABLED", True)
    monkeypatch.setattr(inline.settings, "TRIBUTE_ENABLED", True)
    monkeypatch.setattr(
        type(inline.settings), "is_platega_enabled", lambda _settings: True
    )
    monkeypatch.setattr(
        type(inline.settings), "is_platega_universal_enabled", lambda _settings: True
    )

    keyboard = inline.get_balance_topup_payment_methods_keyboard(
        10_000, "ru", "@fake_me_x"
    )
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert callbacks == [
        "topup_amount|platega_subscription|10000",
        "topup_amount|platega_universal|10000",
        "balance_topup_reset",
    ]

    restricted_keyboard = inline.get_balance_topup_payment_methods_keyboard(
        10_000, "ru", "unrelated_user"
    )
    restricted_callbacks = [
        button.callback_data
        for row in restricted_keyboard.inline_keyboard
        for button in row
    ]
    assert restricted_callbacks == [
        "topup_amount|platega_universal|10000",
        "balance_topup_reset",
    ]


@pytest.mark.anyio("asyncio")
async def test_regular_payment_button_uses_created_subscription_amount(
    monkeypatch: pytest.MonkeyPatch,
):
    class StubPaymentService:
        def __init__(self, _bot):
            pass

        async def create_platega_subscription(self, *_args, **_kwargs):
            return {
                "redirect_url": "https://platega.example/subscription",
                "subscription": SimpleNamespace(id=71),
            }

    state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(return_value={}))
    message = SimpleNamespace(
        bot=SimpleNamespace(),
        delete=AsyncMock(),
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )
    user = SimpleNamespace(id=42, language="ru")

    monkeypatch.setattr(balance_platega, "PaymentService", StubPaymentService)
    monkeypatch.setattr(
        type(balance_platega.settings), "is_platega_enabled", lambda _settings: True
    )
    monkeypatch.setattr(
        balance_platega.settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 10_000
    )
    monkeypatch.setattr(
        balance_platega.settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 500_000
    )

    await balance_platega.process_platega_subscription_amount(
        message,
        user,
        SimpleNamespace(),
        10_000,
        state,
    )

    keyboard = message.answer_photo.await_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "✅ Автоплатеж — 100 руб/мес"
    assert button.url == "https://platega.example/subscription"
    cancel_button = keyboard.inline_keyboard[1][0]
    assert cancel_button.callback_data == "cancel_platega_subscription:71"


@pytest.mark.anyio("asyncio")
async def test_cancel_pending_regular_payment_releases_slot_and_returns_to_choices(
    monkeypatch: pytest.MonkeyPatch,
):
    subscription = SimpleNamespace(
        id=71,
        user_id=42,
        amount_kopeks=10_000,
        platega_subscription_id="subscription-001",
        status="PENDING",
    )
    update_mock = AsyncMock(return_value=subscription)
    render_mock = AsyncMock()
    service = SimpleNamespace(
        is_configured=True,
        cancel_subscription=AsyncMock(),
    )

    class StubPaymentService:
        def __init__(self, _bot):
            self.platega_service = service

    async def fake_get_subscription(*_args, **_kwargs):
        return subscription

    monkeypatch.setattr(balance_platega, "PaymentService", StubPaymentService)
    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_id_for_update",
        fake_get_subscription,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_mock,
        raising=False,
    )
    monkeypatch.setattr(
        "app.handlers.balance.main._render_payment_methods_with_amount",
        render_mock,
    )

    callback = SimpleNamespace(
        data="cancel_platega_subscription:71",
        bot=SimpleNamespace(),
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock())
    user = SimpleNamespace(id=42, language="ru", username="fake_me_x")

    await balance_platega.cancel_pending_platega_subscription(
        callback, user, SimpleNamespace(), state
    )

    update_mock.assert_awaited_once()
    assert update_mock.await_args.kwargs["status"] == "SUBSCRIPTION_CANCELLED"
    assert update_mock.await_args.kwargs["active_user_id"] is None
    service.cancel_subscription.assert_awaited_once_with("subscription-001")
    state.clear.assert_awaited_once()
    state.update_data.assert_awaited_once_with(topup_amount_kopeks=10_000)
    render_mock.assert_awaited_once_with(callback.message, user, 10_000)


@pytest.mark.anyio("asyncio")
async def test_cancel_pending_regular_payment_from_management_returns_to_autopay_menu(
    monkeypatch: pytest.MonkeyPatch,
):
    subscription = SimpleNamespace(
        id=71,
        user_id=42,
        amount_kopeks=10_000,
        platega_subscription_id="subscription-001",
        status="PENDING",
    )
    update_mock = AsyncMock(return_value=subscription)
    show_menu_mock = AsyncMock()
    service = SimpleNamespace(
        is_configured=True,
        cancel_subscription=AsyncMock(),
    )

    class StubPaymentService:
        def __init__(self, _bot):
            self.platega_service = service

    monkeypatch.setattr(balance_platega, "PaymentService", StubPaymentService)
    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_mock,
        raising=False,
    )
    monkeypatch.setattr(balance_platega, "show_platega_autopay_menu", show_menu_mock)

    callback = SimpleNamespace(
        data="cancel_platega_subscription:71:management",
        bot=SimpleNamespace(),
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock())
    user = SimpleNamespace(id=42, language="ru")

    await balance_platega.cancel_pending_platega_subscription(
        callback, user, SimpleNamespace(), state
    )

    service.cancel_subscription.assert_awaited_once_with("subscription-001")
    state.clear.assert_awaited_once()
    state.update_data.assert_not_awaited()
    show_menu_mock.assert_awaited_once_with(callback, user, ANY)


def test_renew_periods_do_not_include_tariff_change():
    keyboard = inline.get_renew_periods_keyboard(
        plan_id=1,
        period_prices={30: 10000, 90: 25000},
        language="ru",
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "subscription_tariffs" not in callbacks
    assert callbacks[-1] == "subscription"


def test_tariff_catalog_can_return_to_subscription_management():
    keyboard = inline.get_tariffs_keyboard(
        [(SimpleNamespace(id=1, code="solo", display_name="Solo"), 10000)],
        language="ru",
        back_callback="subscription",
    )

    assert keyboard.inline_keyboard[-1][0].callback_data == "subscription"


async def test_handle_share_access_uses_subscription_url_without_share_token(monkeypatch):
    import app.database.crud.device_binding_code as binding_module

    callback = SimpleNamespace(
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )
    user = SimpleNamespace(
        language="ru",
        subscription=SimpleNamespace(id=10, subscription_url=SUBSCRIPTION_LINK),
    )
    get_or_create = AsyncMock()
    monkeypatch.setattr(share_token, "get_or_create_share_token", get_or_create)
    monkeypatch.setattr(
        binding_module,
        "get_or_create_binding_code",
        AsyncMock(
            return_value=SimpleNamespace(
                code=LETO_ACCESS_CODE,
                expires_at=datetime.utcnow() + timedelta(hours=24),
            )
        ),
    )
    render = AsyncMock()
    monkeypatch.setattr(purchase, "edit_or_answer_photo", render)

    await purchase.handle_share_access(callback, user, AsyncMock())

    rendered_text = render.await_args.args[1]
    keyboard = render.await_args.args[2]
    assert SUBSCRIPTION_LINK in rendered_text
    assert LETO_ACCESS_CODE in rendered_text
    assert "<b>Ключ доступа (для Happ, Incy):</b>" in rendered_text
    assert "<b>Ключ для входа в Leto VPN</b> (действует 23 ч):" in rendered_text
    assert render.await_args.kwargs["photo_path"] == "images/connection.jpg"
    assert keyboard.inline_keyboard[0][0].text == "📤 Переслать друзьям"
    share_url = keyboard.inline_keyboard[0][0].url
    assert share_url.startswith("https://t.me/share/url?url=")
    assert keyboard.inline_keyboard[1][0].callback_data == "subscription"
    forwarded_text = unquote(share_url)
    assert SUBSCRIPTION_LINK in forwarded_text
    assert LETO_ACCESS_CODE in forwarded_text
    assert "Ключ доступа (для Happ, Incy):" in forwarded_text
    assert "Ключ для входа в Leto VPN (действует 23 ч):" in forwarded_text
    assert "<b>Ключ для входа в Leto VPN</b>" not in forwarded_text
    assert "<pre><code>" not in forwarded_text
    get_or_create.assert_not_awaited()
    callback.answer.assert_awaited_once()


async def test_change_tariff_uses_subscription_management_return(monkeypatch):
    callback = SimpleNamespace()
    user = SimpleNamespace()
    db = AsyncMock()
    show_tariffs = AsyncMock()
    monkeypatch.setattr(tariffs, "show_tariffs_page", show_tariffs)

    await tariffs.show_change_tariff_page(callback, user, db)

    show_tariffs.assert_awaited_once_with(
        callback,
        user,
        db,
        back_callback="subscription",
    )


async def test_extend_tariff_catalog_uses_subscription_management_return(monkeypatch):
    callback = SimpleNamespace()
    user = SimpleNamespace(subscription=SimpleNamespace(plan_id=None, is_trial=True))
    db = AsyncMock()
    show_tariffs = AsyncMock()
    monkeypatch.setattr(purchase, "user_uses_tariffs", lambda _user: True)
    monkeypatch.setattr(tariffs, "show_tariffs_page", show_tariffs)

    await purchase.handle_sub_add_days(callback, user, db)

    show_tariffs.assert_awaited_once_with(
        callback,
        user,
        db,
        back_callback="subscription",
    )


async def test_connect_share_access_returns_to_connect_menu(monkeypatch):
    handler = AsyncMock()
    callback = SimpleNamespace()
    user = SimpleNamespace()
    db = AsyncMock()
    monkeypatch.setattr(purchase, "handle_share_access", handler)

    await purchase.handle_connect_share_access(callback, user, db)

    handler.assert_awaited_once_with(
        callback,
        user,
        db,
        back_callback="howto",
    )
