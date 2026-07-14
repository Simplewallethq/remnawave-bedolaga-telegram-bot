from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.services.interactive_notification_service as interactive_module
import app.handlers.subscription.tariffs as tariffs_module
from app.database.models import DiscountOffer, PlategaPayment, User
from app.services.hot_invoice_offer_service import (
    HotInvoiceCandidate,
    hot_invoice_offer_service,
)
from app.services.interactive_notification_service import (
    InteractiveNotificationService,
    InteractiveNotificationSlot,
)


def _payment(created_at: datetime, payment_id: int = 1) -> PlategaPayment:
    return PlategaPayment(
        id=payment_id,
        user_id=10,
        correlation_id=f"correlation-{payment_id}",
        amount_kopeks=32_000,
        payment_method_code=2,
        status="PENDING",
        is_paid=False,
        redirect_url="https://example.com/pay",
        created_at=created_at,
    )


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


def test_first_touch_window_is_50_to_55_minutes() -> None:
    now = datetime(2026, 7, 13, 9, 0)

    assert hot_invoice_offer_service.is_touch_due(
        _payment(now - timedelta(minutes=50)),
        hot_invoice_offer_service.FIRST_SLOT_KEY,
        now,
    )
    assert hot_invoice_offer_service.is_touch_due(
        _payment(now - timedelta(minutes=54, seconds=59)),
        hot_invoice_offer_service.FIRST_SLOT_KEY,
        now,
    )
    assert not hot_invoice_offer_service.is_touch_due(
        _payment(now - timedelta(minutes=49, seconds=59)),
        hot_invoice_offer_service.FIRST_SLOT_KEY,
        now,
    )
    assert not hot_invoice_offer_service.is_touch_due(
        _payment(now - timedelta(minutes=55)),
        hot_invoice_offer_service.FIRST_SLOT_KEY,
        now,
    )


def test_later_touch_does_not_depend_on_first_touch() -> None:
    payment = _payment(datetime(2026, 7, 10, 6, 0))  # trigger 10.07 10:00 MSK

    assert hot_invoice_offer_service.is_touch_due(
        payment,
        hot_invoice_offer_service.SECOND_SLOT_KEY,
        datetime(2026, 7, 11, 7, 0),  # 10:00 MSK
    )
    assert hot_invoice_offer_service.is_touch_due(
        payment,
        hot_invoice_offer_service.THIRD_SLOT_KEY,
        datetime(2026, 7, 13, 18, 0),  # 21:00 MSK
    )
    assert hot_invoice_offer_service.is_touch_due(
        payment,
        hot_invoice_offer_service.FOURTH_EVENING_SLOT_KEY,
        datetime(2026, 7, 16, 18, 0),
    )


def test_discount_expires_at_2200_msk_on_day_six() -> None:
    payment = _payment(datetime(2026, 7, 10, 6, 0))

    assert hot_invoice_offer_service.campaign_expires_at(payment) == datetime(
        2026, 7, 16, 19, 0
    )


def test_invoice_minutes_left_uses_actual_expiration() -> None:
    now = datetime(2026, 7, 13, 9, 0)
    payment = _payment(now - timedelta(minutes=50))
    payment.expires_at = now + timedelta(minutes=7, seconds=1)

    assert hot_invoice_offer_service.invoice_minutes_left(payment, now) == 8


def test_invoice_minutes_left_falls_back_to_one_hour() -> None:
    now = datetime(2026, 7, 13, 9, 0)
    payment = _payment(now - timedelta(minutes=52))

    assert hot_invoice_offer_service.invoice_minutes_left(payment, now) == 8


async def test_first_touch_message_uses_calculated_minutes() -> None:
    now = datetime(2026, 7, 13, 9, 0)
    payment = _payment(now - timedelta(minutes=50))
    payment.expires_at = now + timedelta(minutes=6, seconds=1)
    candidate = HotInvoiceCandidate(
        payment=payment,
        user=User(id=10, telegram_id=1000, status="active"),
    )
    service = InteractiveNotificationService()
    service.bot = AsyncMock()
    service.bot.send_message.return_value = SimpleNamespace(message_id=42)

    message_id = await service._send_hot_invoice_message(
        candidate,
        hot_invoice_offer_service.FIRST_SLOT_KEY,
        now_utc=now,
    )

    assert message_id == 42
    assert "Счёт закроется через 7 мин" in service.bot.send_message.await_args.args[1]


async def test_detects_conflicting_active_offer() -> None:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = 123
    db.execute.return_value = result

    assert await hot_invoice_offer_service.has_conflicting_active_offer(db, 10)


async def test_price_override_applies_to_regular_tariff_selection(monkeypatch) -> None:
    offer = DiscountOffer(
        id=5,
        user_id=10,
        notification_type=hot_invoice_offer_service.NOTIFICATION_TYPE,
        effect_type=hot_invoice_offer_service.EFFECT_TYPE,
        discount_percent=20,
        is_active=True,
        expires_at=datetime(2099, 1, 1),
        extra_data={"activated_at": None},
    )
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "get_available_offer",
        AsyncMock(return_value=offer),
    )
    activate = AsyncMock(return_value=offer)
    monkeypatch.setattr(hot_invoice_offer_service, "activate_offer", activate)

    price, resolved_offer = await hot_invoice_offer_service.get_price_override(
        AsyncMock(),
        10,
        plan_code="solo",
        period_days=30,
        base_price_kopeks=32_000,
        activate=True,
    )

    assert price == 25_600
    assert resolved_offer is offer
    activate.assert_awaited_once()


async def test_hot_offer_precedes_fixed_offer_when_starting_purchase(monkeypatch) -> None:
    plan = SimpleNamespace(id=1, code="solo", is_active=True)
    hot_offer = SimpleNamespace(
        id=9,
        notification_type=hot_invoice_offer_service.NOTIFICATION_TYPE,
    )
    db_user = SimpleNamespace(
        id=10,
        language="ru",
        balance_kopeks=0,
        subscription=None,
        created_at=None,
    )
    callback = SimpleNamespace(
        data="tariff_buy:solo:30",
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    hot_lookup = AsyncMock(
        return_value=(25_600, hot_offer, hot_invoice_offer_service.NOTIFICATION_TYPE)
    )
    fixed_lookup = AsyncMock(return_value=(20_000, SimpleNamespace(id=8), "fixed"))
    save_cart = AsyncMock()

    monkeypatch.setattr(tariffs_module, "get_plan_by_code", AsyncMock(return_value=plan))
    monkeypatch.setattr(tariffs_module, "get_plan_price", AsyncMock(return_value=32_000))
    monkeypatch.setattr(tariffs_module, "_get_hot_invoice_tariff_offer", hot_lookup)
    monkeypatch.setattr(tariffs_module, "_get_fixed_price_tariff_offer", fixed_lookup)
    monkeypatch.setattr(tariffs_module, "_save_tariff_intent_cart", save_cart)

    await tariffs_module.start_tariff_purchase(callback, db_user, AsyncMock())

    hot_lookup.assert_awaited_once()
    fixed_lookup.assert_not_awaited()
    assert save_cart.await_args.kwargs["total_price"] == 25_600
    assert save_cart.await_args.kwargs["offer_id"] == hot_offer.id


async def test_regular_discount_accepts_any_plan_and_period(monkeypatch) -> None:
    offer = DiscountOffer(
        id=6,
        user_id=10,
        notification_type=hot_invoice_offer_service.NOTIFICATION_TYPE,
        effect_type=hot_invoice_offer_service.EFFECT_TYPE,
        discount_percent=20,
        is_active=True,
        expires_at=datetime(2099, 1, 1),
        extra_data={"activated_at": "2026-07-16T07:00:00"},
    )
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "get_available_offer",
        AsyncMock(return_value=offer),
    )
    db = AsyncMock()

    app_price, _ = await hot_invoice_offer_service.get_price_override(
        db,
        10,
        plan_code="app",
        period_days=30,
        base_price_kopeks=10_000,
    )
    six_month_price, _ = await hot_invoice_offer_service.get_price_override(
        db,
        10,
        plan_code="solo",
        period_days=180,
        base_price_kopeks=89_000,
    )

    assert app_price == 8_000
    assert six_month_price == 71_200


async def test_scheduler_returns_all_slots_with_same_time(monkeypatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 13, 6, 0, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(interactive_module, "datetime", FixedDatetime)
    service = InteractiveNotificationService()
    service.SLOTS = (
        InteractiveNotificationSlot("first", time(hour=10)),
        InteractiveNotificationSlot("second", time(hour=10)),
        InteractiveNotificationSlot("later", time(hour=11)),
    )

    slots, next_run = await service._calculate_next_run()

    assert [slot.key for slot in slots] == ["first", "second"]
    assert next_run == datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)


async def test_later_touch_requires_second_touch_for_same_campaign(monkeypatch) -> None:
    db = AsyncMock()
    candidate = HotInvoiceCandidate(
        payment=_payment(datetime(2026, 7, 10, 6, 0), payment_id=77),
        user=User(id=10, telegram_id=1000, status="active"),
    )
    service = InteractiveNotificationService()
    service.bot = AsyncMock()
    send_message = AsyncMock()
    record_log = AsyncMock()
    was_touch_sent = AsyncMock(side_effect=[False, False])

    monkeypatch.setattr(
        interactive_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(db),
    )
    monkeypatch.setattr(hot_invoice_offer_service, "is_debug_enabled", lambda: False)
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "get_active_campaign_payment_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "list_daily_touch_candidates",
        AsyncMock(side_effect=[[candidate], []]),
    )
    monkeypatch.setattr(hot_invoice_offer_service, "was_touch_sent", was_touch_sent)
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "is_campaign_eligible",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(service, "_send_hot_invoice_message", send_message)
    monkeypatch.setattr(service, "_record_log", record_log)

    result = await service._send_hot_invoice_touch(
        InteractiveNotificationSlot(hot_invoice_offer_service.THIRD_SLOT_KEY)
    )

    assert result.payload == {"sent": 0, "failed": 0, "skipped": 1}
    assert (
        was_touch_sent.await_args_list[0].kwargs["slot_key"]
        == hot_invoice_offer_service.THIRD_SLOT_KEY
    )
    assert (
        was_touch_sent.await_args_list[1].kwargs["slot_key"]
        == hot_invoice_offer_service.SECOND_SLOT_KEY
    )
    send_message.assert_not_awaited()
    record_log.assert_not_awaited()


async def test_later_touch_sends_when_second_touch_exists(monkeypatch) -> None:
    db = AsyncMock()
    candidate = HotInvoiceCandidate(
        payment=_payment(datetime(2026, 7, 10, 6, 0), payment_id=77),
        user=User(id=10, telegram_id=1000, status="active"),
    )
    service = InteractiveNotificationService()
    service.bot = AsyncMock()
    send_message = AsyncMock(return_value=42)
    record_log = AsyncMock()
    was_touch_sent = AsyncMock(side_effect=[False, True])

    monkeypatch.setattr(
        interactive_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(db),
    )
    monkeypatch.setattr(hot_invoice_offer_service, "is_debug_enabled", lambda: False)
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "get_active_campaign_payment_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "list_daily_touch_candidates",
        AsyncMock(side_effect=[[candidate], []]),
    )
    monkeypatch.setattr(hot_invoice_offer_service, "was_touch_sent", was_touch_sent)
    monkeypatch.setattr(
        hot_invoice_offer_service,
        "is_campaign_eligible",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(service, "_send_hot_invoice_message", send_message)
    monkeypatch.setattr(service, "_record_log", record_log)

    result = await service._send_hot_invoice_touch(
        InteractiveNotificationSlot(hot_invoice_offer_service.THIRD_SLOT_KEY)
    )

    assert result.payload == {"sent": 1, "failed": 0, "skipped": 0}
    send_message.assert_awaited_once()
    record_log.assert_awaited_once()


def test_campaign_payload_anchors_platega_invoice() -> None:
    user = User(id=10, telegram_id=1000, status="active")
    candidate = HotInvoiceCandidate(
        payment=_payment(datetime(2026, 7, 13, 6, 0), payment_id=77),
        user=user,
    )

    payload = hot_invoice_offer_service.build_campaign_payload(candidate)

    assert payload["provider"] == "platega"
    assert payload["payment_id"] == 77
    assert payload["campaign_key"] == "platega:77"
    assert payload["trigger_at"] == "2026-07-13T07:00:00"
