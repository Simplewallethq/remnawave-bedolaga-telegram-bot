from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, User

from app.handlers.subscription import purchase, tariffs
from app.handlers.balance import platega as balance_platega
from app.keyboards.inline import get_extend_subscription_keyboard_with_prices
from app.services.user_service import UserService


def _callback():
    callback = AsyncMock()
    callback.message = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _tariff_subscription(*, expired: bool = False):
    now = datetime.now()
    return SimpleNamespace(
        plan_id=123,
        is_trial=False,
        status="expired" if expired else "active",
        end_date=now - timedelta(days=1) if expired else now + timedelta(days=5),
    )


def test_legacy_extend_back_returns_subscription_management():
    keyboard = get_extend_subscription_keyboard_with_prices(
        language="ru",
        prices={30: 10000},
    )

    assert keyboard.inline_keyboard[-1][0].callback_data == "subscription"


async def test_active_tariff_extend_routes_to_current_plan_renewal(monkeypatch):
    callback = _callback()
    db = AsyncMock()
    user = SimpleNamespace(language="ru", subscription=_tariff_subscription())

    show_renew_current = AsyncMock()
    legacy_price_calc = AsyncMock()
    monkeypatch.setattr(tariffs, "show_renew_current", show_renew_current)
    monkeypatch.setattr(purchase, "calculate_topup_price_kopeks", legacy_price_calc)

    await purchase.handle_sub_add_days(callback, user, db)

    show_renew_current.assert_awaited_once_with(callback, user, db)
    legacy_price_calc.assert_not_called()


async def test_expired_tariff_extend_restores_same_plan(monkeypatch):
    callback = _callback()
    db = AsyncMock()
    user = SimpleNamespace(language="ru", subscription=_tariff_subscription(expired=True))

    show_renew_current = AsyncMock()
    show_tariffs_page = AsyncMock()
    legacy_price_calc = AsyncMock()
    monkeypatch.setattr(tariffs, "show_renew_current", show_renew_current)
    monkeypatch.setattr(tariffs, "show_tariffs_page", show_tariffs_page)
    monkeypatch.setattr(purchase, "calculate_topup_price_kopeks", legacy_price_calc)

    await purchase.handle_sub_add_days(callback, user, db)

    show_renew_current.assert_awaited_once_with(callback, user, db)
    show_tariffs_page.assert_not_called()
    legacy_price_calc.assert_not_called()


async def test_legacy_extend_keeps_legacy_calculator(monkeypatch):
    callback = _callback()
    db = AsyncMock()
    subscription = SimpleNamespace(
        plan_id=None,
        is_trial=False,
        days_left=5,
        connected_squads=[],
        device_limit=1,
        traffic_limit_gb=0,
    )
    user = SimpleNamespace(
        language="ru",
        created_at=purchase.settings.get_tariffs_legacy_cutoff() - timedelta(days=1),
        subscription=subscription,
        promo_group_id=None,
        get_promo_discount=MagicMock(return_value=0),
        promo_offer_discount_percent=0,
        promo_offer_discount_expires_at=None,
    )

    class Texts:
        def format_price(self, amount: int) -> str:
            return f"{amount / 100:.0f} ₽"

    class PriceInfo:
        def __init__(self, base_price: int, final_price: int, discount_percent: int = 0):
            self.base_price = base_price
            self.final_price = final_price
            self.discount_percent = discount_percent

    subscription_service = SimpleNamespace(
        get_countries_price_by_uuids=AsyncMock(return_value=(0, [])),
    )
    legacy_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="30 дней", callback_data="extend_period_30")]],
    )
    show_renew_current = AsyncMock()
    show_tariffs_page = AsyncMock()
    keyboard_builder = MagicMock(return_value=legacy_keyboard)

    monkeypatch.setattr(purchase.settings, "TARIFFS_ENABLED", False)
    monkeypatch.setattr(purchase, "get_texts", lambda language: Texts())
    settings_cls = type(purchase.settings)
    monkeypatch.setattr(settings_cls, "get_available_renewal_periods", lambda self: [30])
    monkeypatch.setattr(settings_cls, "is_devices_selection_enabled", lambda self: False)
    monkeypatch.setattr(settings_cls, "get_disabled_mode_device_limit", lambda self: None)
    monkeypatch.setattr(settings_cls, "get_traffic_price", lambda self, traffic_gb: 0)
    monkeypatch.setattr(purchase, "calculate_months_from_days", lambda days: 1)
    monkeypatch.setattr(
        purchase,
        "calculate_user_price",
        lambda db_user, base_price, days, category: PriceInfo(base_price, base_price),
    )
    monkeypatch.setattr(purchase, "PriceInfo", PriceInfo)
    monkeypatch.setattr(purchase, "SubscriptionService", lambda: subscription_service)
    monkeypatch.setattr(purchase, "_get_promo_offer_discount_percent", lambda db_user: 0)
    monkeypatch.setattr(
        purchase,
        "_apply_promo_offer_discount",
        lambda db_user, total: {"discounted": total, "discount": 0, "percent": 0},
    )
    monkeypatch.setattr(purchase, "format_period_description", lambda days, language: "30 дней")
    monkeypatch.setattr(
        purchase,
        "format_price_text",
        lambda period_label, price_info, format_price_func: (
            f"{period_label}: {format_price_func(price_info.final_price)}"
        ),
    )
    monkeypatch.setattr(purchase, "_build_promo_group_discount_text", AsyncMock(return_value=""))
    monkeypatch.setattr(purchase, "_get_promo_offer_hint", AsyncMock(return_value=""))
    monkeypatch.setattr(purchase, "get_extend_subscription_keyboard_with_prices", keyboard_builder)
    monkeypatch.setattr(tariffs, "show_renew_current", show_renew_current)
    monkeypatch.setattr(tariffs, "show_tariffs_page", show_tariffs_page)

    await purchase.handle_extend_subscription(callback, user, db)

    callback.message.edit_text.assert_awaited_once()
    keyboard_builder.assert_called_once()
    show_renew_current.assert_not_called()
    show_tariffs_page.assert_not_called()


async def test_trial_extend_routes_to_tariff_catalog_without_state(monkeypatch):
    callback = _callback()
    db = AsyncMock()
    subscription = SimpleNamespace(plan_id=None, is_trial=True)
    user = SimpleNamespace(language="ru", subscription=subscription)

    show_tariffs_page = AsyncMock()
    start_subscription_purchase = AsyncMock()
    monkeypatch.setattr(tariffs, "show_tariffs_page", show_tariffs_page)
    monkeypatch.setattr(purchase, "start_subscription_purchase", start_subscription_purchase)

    await purchase.handle_extend_subscription(callback, user, db)

    show_tariffs_page.assert_awaited_once_with(callback, user, db)
    start_subscription_purchase.assert_not_called()


async def test_tariff_user_without_subscription_extend_routes_to_catalog(monkeypatch):
    callback = _callback()
    db = AsyncMock()
    cutoff = purchase.settings.get_tariffs_legacy_cutoff()
    user = SimpleNamespace(
        language="ru",
        created_at=cutoff,
        subscription=None,
    )

    show_tariffs_page = AsyncMock()
    show_renew_current = AsyncMock()
    monkeypatch.setattr(purchase.settings, "TARIFFS_ENABLED", False)
    monkeypatch.setattr(tariffs, "show_tariffs_page", show_tariffs_page)
    monkeypatch.setattr(tariffs, "show_renew_current", show_renew_current)

    await purchase.handle_extend_subscription(callback, user, db)

    show_tariffs_page.assert_awaited_once_with(callback, user, db)
    show_renew_current.assert_not_called()
    callback.answer.assert_not_called()


async def test_insufficient_tariff_purchase_starts_direct_platega_checkout(monkeypatch):
    callback = _callback()
    callback.data = "tariff_buy:solo:30"
    state = AsyncMock()
    db = AsyncMock()
    user = SimpleNamespace(language="ru", balance_kopeks=200, subscription=None)
    plan = SimpleNamespace(id=1, code="solo", is_active=True)
    checkout = AsyncMock()

    monkeypatch.setattr(tariffs, "get_plan_by_code", AsyncMock(return_value=plan))
    monkeypatch.setattr(
        tariffs,
        "_resolve_tariff_purchase_price",
        AsyncMock(return_value=(39000, 39000, None, None, None)),
    )
    monkeypatch.setattr(tariffs, "_save_tariff_intent_cart", AsyncMock())
    monkeypatch.setattr(tariffs, "_start_tariff_platega_checkout", checkout)

    await tariffs.start_tariff_purchase(callback, user, db, state)

    checkout.assert_awaited_once_with(
        callback,
        user,
        state,
        total_kopeks=39000,
        back_callback="tariff_select:solo",
        balance_callback="tariff_balance:purchase:solo:30",
    )


async def test_covered_tariff_purchase_shows_platega_and_balance_payment(monkeypatch):
    callback = _callback()
    callback.data = "tariff_buy:solo:30"
    state = AsyncMock()
    db = AsyncMock()
    user = SimpleNamespace(language="ru", balance_kopeks=39000, subscription=None)
    plan = SimpleNamespace(id=1, code="solo", is_active=True)
    checkout = AsyncMock()
    finalize = AsyncMock()

    monkeypatch.setattr(tariffs, "get_plan_by_code", AsyncMock(return_value=plan))
    monkeypatch.setattr(
        tariffs,
        "_resolve_tariff_purchase_price",
        AsyncMock(return_value=(39000, 39000, None, None, None)),
    )
    monkeypatch.setattr(tariffs, "_save_tariff_intent_cart", AsyncMock())
    monkeypatch.setattr(tariffs, "_start_tariff_platega_checkout", checkout)
    monkeypatch.setattr(tariffs, "finalize_tariff_purchase", finalize)

    await tariffs.start_tariff_purchase(callback, user, db, state)

    checkout.assert_awaited_once_with(
        callback,
        user,
        state,
        total_kopeks=39000,
        back_callback="tariff_select:solo",
        balance_callback="tariff_balance:purchase:solo:30",
    )
    finalize.assert_not_awaited()


async def test_tariff_platega_checkout_preserves_purchase_context(monkeypatch):
    callback = _callback()
    state = AsyncMock()
    user = SimpleNamespace(language="ru", balance_kopeks=200)
    start_payment = AsyncMock()

    monkeypatch.setattr(type(tariffs.settings), "is_platega_universal_enabled", lambda self: True)
    monkeypatch.setattr(balance_platega, "start_platega_universal_payment", start_payment)

    await tariffs._start_tariff_platega_checkout(
        callback,
        user,
        state,
        total_kopeks=39000,
        back_callback="tariff_select:solo",
    )

    state.update_data.assert_awaited_once_with(
        platega_pending_amount=38800,
        platega_back_callback="tariff_select:solo",
        tariff_checkout_summary={
            "total_kopeks": 39000,
            "balance_kopeks": 200,
            "missing_kopeks": 38800,
            "balance_callback": None,
        },
    )
    start_payment.assert_awaited_once_with(callback, user, state)


async def test_balance_renewal_uses_copied_callback_data(monkeypatch):
    callback = CallbackQuery(
        id="callback-id",
        from_user=User(id=42, is_bot=False, first_name="Test"),
        chat_instance="chat-instance",
        data="tariff_balance:renew:3:30",
    )
    renewal = AsyncMock()
    monkeypatch.setattr(tariffs, "apply_renewal", renewal)

    await tariffs.pay_tariff_from_balance(
        callback,
        SimpleNamespace(),
        AsyncMock(),
        AsyncMock(),
    )

    forwarded_callback = renewal.await_args.args[0]
    assert callback.data == "tariff_balance:renew:3:30"
    assert forwarded_callback.data == "tariff_renew:3:30"
    assert forwarded_callback is not callback
    assert renewal.await_args.kwargs["pay_from_balance"] is True


async def test_balance_upgrade_renders_common_success_card(monkeypatch):
    callback = _callback()
    callback.data = "tariff_upgrade_confirm:pro"
    state = AsyncMock()
    db = AsyncMock()
    active_sub = SimpleNamespace(
        id=1,
        plan_id=1,
        plan_period_days=30,
        end_date=datetime.now() + timedelta(days=20),
    )
    user = SimpleNamespace(language="ru", balance_kopeks=50000)
    plan = SimpleNamespace(id=3, code="pro", display_name="Pro", is_active=True)

    monkeypatch.setattr(tariffs, "get_plan_by_code", AsyncMock(return_value=plan))
    monkeypatch.setattr(tariffs, "_resolve_active_subscription", AsyncMock(return_value=active_sub))
    monkeypatch.setattr(tariffs, "get_plan_price", AsyncMock(return_value=50000))
    monkeypatch.setattr(tariffs, "get_current_plan_price_for_period", AsyncMock(return_value=0))
    monkeypatch.setattr(tariffs, "_get_hot_invoice_tariff_offer", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(tariffs, "calculate_upgrade_delta", lambda *args: 50000)
    monkeypatch.setattr(tariffs, "finalize_tier_switch", AsyncMock(return_value=active_sub))
    monkeypatch.setattr(tariffs, "_delete_tariff_intent_cart", AsyncMock())

    await tariffs.confirm_tier_upgrade(callback, user, db, state, pay_from_balance=True)

    message_text = callback.message.edit_text.await_args.args[0]
    assert "✅ <b>Подписка активирована</b>" in message_text
    assert "Тариф Pro" in message_text
    callback.answer.assert_awaited_once_with()


async def test_user_service_balance_notification_uses_subscription_extend_callback():
    bot = AsyncMock()
    user = SimpleNamespace(
        telegram_id=42,
        language="ru",
        balance_kopeks=1000,
        subscription=SimpleNamespace(status="active"),
    )

    sent = await UserService()._send_balance_notification(bot, user, 500, "admin")

    assert sent is True
    reply_markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert reply_markup.inline_keyboard[0][0].callback_data == "subscription_extend"
