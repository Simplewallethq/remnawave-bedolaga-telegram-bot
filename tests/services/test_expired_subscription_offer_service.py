from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.handlers.subscription.tariffs as tariffs_module
import app.services.expired_subscription_offer_service as expired_module
import app.services.interactive_notification_service as interactive_module
from app.database.models import DiscountOffer, Subscription, SubscriptionPlan, User
from app.services.expired_subscription_offer_service import (
    ExpiredSubscriptionCandidate,
    expired_subscription_offer_service,
)
from app.services.hot_invoice_offer_service import hot_invoice_offer_service
from app.services.interactive_notification_service import (
    InteractiveNotificationService,
    InteractiveNotificationSlot,
)


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _subscription(ended_at: datetime, subscription_id: int = 1) -> Subscription:
    plan = SubscriptionPlan(id=7, code="solo", display_name="Solo", is_active=True)
    subscription = Subscription(
        id=subscription_id,
        user_id=10,
        status="expired",
        is_trial=False,
        is_partner=False,
        end_date=ended_at,
        plan_id=plan.id,
        plan_period_days=30,
    )
    subscription.plan = plan
    return subscription


def test_first_touch_window_is_two_hours_to_two_hours_five_minutes() -> None:
    now = datetime(2026, 7, 13, 9, 0)

    assert expired_subscription_offer_service.is_touch_due(
        _subscription(now - timedelta(hours=2)),
        expired_subscription_offer_service.FIRST_SLOT_KEY,
        now,
    )
    assert expired_subscription_offer_service.is_touch_due(
        _subscription(now - timedelta(hours=2, minutes=4, seconds=59)),
        expired_subscription_offer_service.FIRST_SLOT_KEY,
        now,
    )
    assert not expired_subscription_offer_service.is_touch_due(
        _subscription(now - timedelta(hours=1, minutes=59, seconds=59)),
        expired_subscription_offer_service.FIRST_SLOT_KEY,
        now,
    )
    assert not expired_subscription_offer_service.is_touch_due(
        _subscription(now - timedelta(hours=2, minutes=5)),
        expired_subscription_offer_service.FIRST_SLOT_KEY,
        now,
    )


def test_daily_touches_are_based_on_trigger_date() -> None:
    subscription = _subscription(datetime(2026, 7, 10, 6, 0))  # trigger 10.07 10:00 MSK

    assert expired_subscription_offer_service.is_touch_due(
        subscription,
        expired_subscription_offer_service.SECOND_SLOT_KEY,
        datetime(2026, 7, 11, 7, 0),
    )
    assert expired_subscription_offer_service.is_touch_due(
        subscription,
        expired_subscription_offer_service.THIRD_SLOT_KEY,
        datetime(2026, 7, 13, 18, 0),
    )
    assert expired_subscription_offer_service.is_touch_due(
        subscription,
        expired_subscription_offer_service.FOURTH_EVENING_SLOT_KEY,
        datetime(2026, 7, 16, 18, 0),
    )


def test_discount_expires_at_2200_msk_on_day_six() -> None:
    subscription = _subscription(datetime(2026, 7, 10, 6, 0))

    assert expired_subscription_offer_service.campaign_expires_at(subscription) == datetime(
        2026, 7, 16, 19, 0
    )


async def test_campaign_is_not_eligible_when_segment_b_is_active(monkeypatch) -> None:
    candidate = ExpiredSubscriptionCandidate(
        subscription=_subscription(datetime(2026, 7, 13, 6, 0)),
        user=User(
            id=10,
            telegram_id=1000,
            status="active",
            has_had_paid_subscription=True,
        ),
    )

    monkeypatch.setattr(
        expired_subscription_offer_service,
        "has_subscription_payment_after",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "has_unpaid_invoice_since",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "get_active_campaign_payment_id",
        AsyncMock(return_value=77),
    )

    assert not await expired_subscription_offer_service.is_campaign_eligible(
        AsyncMock(),
        candidate,
        now_utc=datetime(2026, 7, 13, 9, 0),
    )


async def test_price_override_applies_only_to_previous_plan_and_activates(monkeypatch) -> None:
    offer = DiscountOffer(
        id=5,
        user_id=10,
        notification_type=expired_subscription_offer_service.NOTIFICATION_TYPE,
        effect_type=expired_subscription_offer_service.EFFECT_TYPE,
        discount_percent=20,
        is_active=True,
        expires_at=datetime(2099, 1, 1),
        extra_data={"plan_code": "solo", "activated_at": None},
    )
    monkeypatch.setattr(
        expired_subscription_offer_service,
        "get_available_offer",
        AsyncMock(return_value=offer),
    )
    activate = AsyncMock(return_value=offer)
    monkeypatch.setattr(expired_subscription_offer_service, "activate_offer", activate)

    price, resolved_offer = await expired_subscription_offer_service.get_price_override(
        AsyncMock(),
        10,
        plan_code="solo",
        period_days=30,
        base_price_kopeks=10_000,
        activate=True,
    )
    wrong_plan_price, _ = await expired_subscription_offer_service.get_price_override(
        AsyncMock(),
        10,
        plan_code="plus",
        period_days=30,
        base_price_kopeks=10_000,
    )

    assert price == 8_000
    assert resolved_offer is offer
    assert wrong_plan_price is None
    activate.assert_awaited_once()


async def test_day_six_message_routes_to_previous_plan_periods() -> None:
    candidate = ExpiredSubscriptionCandidate(
        subscription=_subscription(datetime(2026, 7, 10, 6, 0)),
        user=User(id=10, telegram_id=1000, status="active"),
    )
    service = InteractiveNotificationService()
    service.bot = AsyncMock()
    service.bot.send_message.return_value = SimpleNamespace(message_id=42)

    message_id = await service._send_expired_subscription_message(
        candidate,
        expired_subscription_offer_service.FOURTH_MORNING_SLOT_KEY,
        offer_id=55,
    )

    assert message_id == 42
    keyboard = service.bot.send_message.await_args.kwargs["reply_markup"]
    assert (
        keyboard.inline_keyboard[0][0].callback_data
        == "expired_subscription_offer:claim:55:solo"
    )
    assert "−20%" in keyboard.inline_keyboard[0][0].text


async def test_expired_subscription_offer_claim_shows_alert(monkeypatch) -> None:
    callback = SimpleNamespace(
        data="expired_subscription_offer:claim:55:solo",
        answer=AsyncMock(),
    )
    show_periods = AsyncMock()
    monkeypatch.setattr(
        expired_subscription_offer_service,
        "get_available_offer",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(tariffs_module, "show_tariff_periods", show_periods)

    await tariffs_module.claim_expired_subscription_offer(
        callback,
        SimpleNamespace(id=10),
        AsyncMock(),
    )

    callback.answer.assert_awaited_once_with(
        "Предложение недоступно или истекло",
        show_alert=True,
    )
    show_periods.assert_not_awaited()


async def test_expired_paid_tiered_subscription_is_renewable() -> None:
    user = SimpleNamespace(
        subscription=_subscription(datetime(2026, 7, 13, 6, 0)),
    )

    resolved = await tariffs_module._resolve_renewable_subscription(user)

    assert resolved is user.subscription


async def test_debug_sequence_sends_all_segment_c_touches(monkeypatch) -> None:
    monkeypatch.setattr(expired_module, "IS_ARTEM_DEBUG", True)
    monkeypatch.setattr(expired_module, "ARTEM_DEBUG_TOUCH_INTERVAL", timedelta(seconds=0))

    candidate = ExpiredSubscriptionCandidate(
        subscription=_subscription(datetime(2026, 7, 10, 6, 0)),
        user=User(id=18835, telegram_id=18835, status="active"),
    )
    offer = SimpleNamespace(id=55)
    service = InteractiveNotificationService()
    service.bot = AsyncMock()
    send_message = AsyncMock(return_value=42)
    record_log = AsyncMock()

    monkeypatch.setattr(
        interactive_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(AsyncMock()),
    )
    monkeypatch.setattr(
        expired_subscription_offer_service,
        "get_debug_candidate",
        AsyncMock(return_value=candidate),
    )
    monkeypatch.setattr(
        expired_subscription_offer_service,
        "ensure_discount_offer",
        AsyncMock(return_value=offer),
    )
    monkeypatch.setattr(service, "_send_expired_subscription_message", send_message)
    monkeypatch.setattr(service, "_record_log", record_log)

    await service._run_expired_subscription_debug_sequence()

    assert [call.args[1] for call in send_message.await_args_list] == list(
        expired_subscription_offer_service.SLOT_KEYS
    )
    assert record_log.await_count == len(expired_subscription_offer_service.SLOT_KEYS)
    assert expired_subscription_offer_service.ensure_discount_offer.await_count == 2


async def test_regular_segment_c_processing_is_suppressed_in_debug(monkeypatch) -> None:
    monkeypatch.setattr(expired_module, "IS_ARTEM_DEBUG", True)
    service = InteractiveNotificationService()

    result = await service._send_expired_subscription_touch(
        InteractiveNotificationSlot(expired_subscription_offer_service.FIRST_SLOT_KEY)
    )

    assert result.status == "processed"
    assert result.payload == {"sent": 0, "failed": 0, "skipped": 0, "debug": True}
