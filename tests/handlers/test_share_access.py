from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest

from app.database.crud import share_token
from app.handlers.balance import platega as balance_platega
from app.handlers.subscription import purchase, tariffs
from app.keyboards import inline
from app.localization.texts import get_texts
from app.services import payment_service as payment_service_module


SUBSCRIPTION_LINK = "https://letovpn.com/sub/private-subscription-key"


def test_share_access_text_uses_a_copyable_direct_link_and_platform_links():
    text = purchase._build_share_access_text(get_texts("ru"), SUBSCRIPTION_LINK)

    assert f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>" in text
    assert "<b>🤖 Android</b>" in text
    assert "<b>🍎 iPhone/Mac</b>" in text
    assert "<b>💻 Windows</b>" in text
    assert "https://play.google.com/store/apps/details?id=com.leto.split" in text
    assert "https://apps.apple.com/ru/app/incy/id6756943388" in text
    assert "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe" in text


def test_share_access_friend_message_uses_direct_link_and_platform_links():
    text = purchase._build_share_access_friend_message(get_texts("ru"), SUBSCRIPTION_LINK)

    assert SUBSCRIPTION_LINK in text
    assert "авторизуйся через ссылку доступа" in text
    assert "https://play.google.com/store/apps/details?id=com.leto.split" in text
    assert "https://apps.apple.com/ru/app/incy/id6756943388" in text
    assert "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe" in text


def test_share_access_has_english_text_and_action_button():
    texts = get_texts("en")
    screen_text = purchase._build_share_access_text(texts, SUBSCRIPTION_LINK)
    friend_text = purchase._build_share_access_friend_message(texts, SUBSCRIPTION_LINK)

    assert "To share access with friends" in screen_text
    assert f"<pre><code>{SUBSCRIPTION_LINK}</code></pre>" in screen_text
    assert "I'm sharing access to Leto VPN" in friend_text
    assert texts.t("SHARE_ACCESS_SEND_BUTTON") == "📤 Share access"


def test_subscription_management_has_devices_and_tariff_change_but_no_share_button():
    keyboard = inline.get_subscription_menu_keyboard("ru")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert [button.callback_data for button in keyboard.inline_keyboard[2]] == [
        "sub_add_days",
        "subscription_manage_devices",
    ]
    assert keyboard.inline_keyboard[3][0].callback_data == "subscription_change_tariff"
    assert keyboard.inline_keyboard[-1][0].callback_data == "main_menu"
    assert "share_access" not in callbacks


def test_profile_keyboard_no_longer_contains_devices():
    keyboard = inline.get_profile_keyboard("ru", has_subscription=True)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "subscription_manage_devices" not in callbacks


def test_profile_shows_regular_payment_cancellation_only_when_active():
    active_keyboard = inline.get_profile_keyboard(
        "ru", has_active_platega_subscription=True
    )
    inactive_keyboard = inline.get_profile_keyboard(
        "ru", has_active_platega_subscription=False
    )

    active_callbacks = [
        button.callback_data
        for row in active_keyboard.inline_keyboard
        for button in row
    ]
    inactive_callbacks = [
        button.callback_data
        for row in inactive_keyboard.inline_keyboard
        for button in row
    ]

    assert active_callbacks.count("profile_cancel_platega_subscription") == 1
    assert "profile_cancel_platega_subscription" not in inactive_callbacks


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

    state = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(
        bot=SimpleNamespace(),
        delete=AsyncMock(),
        answer=AsyncMock(),
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

    keyboard = message.answer.await_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Автоплатеж — 100 руб/мес"
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
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_caption=AsyncMock()),
        answer=AsyncMock(),
    )
    user = SimpleNamespace(
        language="ru",
        subscription=SimpleNamespace(id=10, subscription_url=SUBSCRIPTION_LINK),
    )
    get_or_create = AsyncMock()
    monkeypatch.setattr(share_token, "get_or_create_share_token", get_or_create)

    await purchase.handle_share_access(callback, user, AsyncMock())

    rendered_text = callback.message.edit_caption.await_args.kwargs["caption"]
    keyboard = callback.message.edit_caption.await_args.kwargs["reply_markup"]
    share_url = keyboard.inline_keyboard[0][0].url
    assert SUBSCRIPTION_LINK in rendered_text
    assert keyboard.inline_keyboard[0][0].text == "📤 Поделиться доступом"
    assert keyboard.inline_keyboard[1][0].callback_data == "subscription"
    assert SUBSCRIPTION_LINK in unquote(share_url)
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
