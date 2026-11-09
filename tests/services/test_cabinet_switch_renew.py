"""Автопокупка корзины `switch_renew`: другой тариф на срок из кабинета."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _patch(monkeypatch, user, active_sub, *, price=348_000):
    plan = SimpleNamespace(id=4, code="pro", display_name="Pro", is_active=True)
    subscription = MagicMock(id=1, end_date=datetime.utcnow() + timedelta(days=415))
    transaction = MagicMock(id=9, amount_kopeks=price, payment_method="manual")
    renewal = AsyncMock(return_value=(subscription, transaction, active_sub.end_date))

    monkeypatch.setattr("app.database.crud.user.get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(
        "app.services.plan_pricing_service.get_plan_by_id", AsyncMock(return_value=plan)
    )
    monkeypatch.setattr(
        "app.services.plan_pricing_service.get_plan_price", AsyncMock(return_value=price)
    )
    monkeypatch.setattr(
        "app.handlers.subscription.tariffs._resolve_active_subscription",
        AsyncMock(return_value=active_sub),
    )
    monkeypatch.setattr("app.handlers.subscription.tariffs.finalize_tariff_renewal", renewal)
    monkeypatch.setattr(
        "app.handlers.subscription.tariffs.finalize_tier_switch",
        AsyncMock(side_effect=AssertionError("prorated switch must not run")),
    )
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.record_subscription_renewal_event",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.user_cart_service.delete_user_cart",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.clear_subscription_checkout_draft",
        AsyncMock(),
    )
    return renewal


def _user(balance):
    return SimpleNamespace(
        id=92631, telegram_id=1, language="ru", balance_kopeks=balance, subscription=None
    )


async def test_switch_renew_extends_on_new_plan(monkeypatch):
    from app.services.subscription_auto_purchase_service import _auto_tariff_purchase

    user = _user(348_000)
    active_sub = SimpleNamespace(plan_id=3, end_date=datetime.utcnow() + timedelta(days=55))
    renewal = _patch(monkeypatch, user, active_sub)

    cart = {
        "cart_mode": "tariff",
        "tariff_op": "switch_renew",
        "plan_id": 4,
        "period_days": 360,
        "total_price": 348_000,
        "source": "cabinet",
    }
    assert await _auto_tariff_purchase(MagicMock(), user, cart) is True

    renewal.assert_awaited_once()
    args, kwargs = renewal.await_args
    assert args[2] is active_sub
    assert args[4:] == (360, 348_000)
    assert kwargs == {"switch_plan": True}


async def test_switch_renew_waits_for_full_price(monkeypatch):
    from app.services.subscription_auto_purchase_service import _auto_tariff_purchase

    user = _user(18_333)
    active_sub = SimpleNamespace(plan_id=3, end_date=datetime.utcnow() + timedelta(days=55))
    renewal = _patch(monkeypatch, user, active_sub)

    cart = {"cart_mode": "tariff", "tariff_op": "switch_renew", "plan_id": 4, "period_days": 360}
    assert await _auto_tariff_purchase(MagicMock(), user, cart) is False
    renewal.assert_not_awaited()
