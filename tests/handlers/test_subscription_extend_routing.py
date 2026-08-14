from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.handlers.subscription import purchase, tariffs
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
