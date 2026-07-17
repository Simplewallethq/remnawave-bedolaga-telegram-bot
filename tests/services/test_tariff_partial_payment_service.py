"""Тесты частичной оплаты тарифа: разбивка, клампинг минимумов, snapshot."""

import json
from types import SimpleNamespace

import pytest

from app.services.tariff_partial_payment_service import (
    SNAPSHOT_METADATA_KEY,
    build_invoice_checkout_snapshot,
    build_partial_breakdown,
    clamp_invoice_amount,
    extract_checkout_snapshot,
    get_provider_min_kopeks,
)


# --- build_partial_breakdown -------------------------------------------------

def test_breakdown_none_when_balance_zero():
    assert build_partial_breakdown(0, 32000) is None


def test_breakdown_none_when_balance_covers_price():
    assert build_partial_breakdown(32000, 32000) is None
    assert build_partial_breakdown(50000, 32000) is None


def test_breakdown_partial_without_discount():
    b = build_partial_breakdown(10000, 32000)
    assert b == {
        "base_price_kopeks": 32000,
        "discount_kopeks": 0,
        "final_price_kopeks": 32000,
        "balance_planned_kopeks": 10000,
        "shortfall_kopeks": 22000,
    }


def test_breakdown_discount_applies_before_balance():
    # Solo 320₽ → скидка 20% → 256₽ → баланс 100₽ → к доплате 156₽ (из СТЗ)
    b = build_partial_breakdown(10000, 25600, base_price_kopeks=32000)
    assert b["discount_kopeks"] == 6400
    assert b["final_price_kopeks"] == 25600
    assert b["shortfall_kopeks"] == 15600


# --- clamp_invoice_amount ----------------------------------------------------

def test_clamp_raises_to_provider_minimum():
    platega_min = get_provider_min_kopeks("platega")
    assert platega_min > 0
    assert clamp_invoice_amount("platega", platega_min - 4000) == platega_min


def test_clamp_keeps_amount_above_minimum():
    assert clamp_invoice_amount("platega", 15600) == 15600


def test_clamp_unknown_method_passthrough():
    assert clamp_invoice_amount("stars", 500) == 500
    assert clamp_invoice_amount("nonexistent", 700) == 700


# --- extract_checkout_snapshot ----------------------------------------------

def _payment(metadata):
    return SimpleNamespace(metadata_json=metadata)


def _valid_snapshot(**overrides):
    snapshot = {
        "v": 1,
        "kind": "tariff_purchase",
        "user_id": 1,
        "plan_id": 4,
        "period_days": 30,
        "final_price_kopeks": 25600,
        "invoice_amount_kopeks": 15600,
    }
    snapshot.update(overrides)
    return snapshot


def test_extract_from_dict_metadata():
    payment = _payment({SNAPSHOT_METADATA_KEY: _valid_snapshot()})
    extracted = extract_checkout_snapshot(payment)
    assert extracted["final_price_kopeks"] == 25600


def test_extract_from_str_encoded_metadata():
    payment = _payment(json.dumps({SNAPSHOT_METADATA_KEY: _valid_snapshot()}))
    extracted = extract_checkout_snapshot(payment)
    assert extracted["plan_id"] == 4


def test_extract_none_cases():
    assert extract_checkout_snapshot(_payment(None)) is None
    assert extract_checkout_snapshot(_payment({})) is None
    assert extract_checkout_snapshot(_payment("not json")) is None
    assert extract_checkout_snapshot(_payment({SNAPSHOT_METADATA_KEY: "garbage"})) is None
    assert extract_checkout_snapshot(SimpleNamespace()) is None


def test_extract_rejects_wrong_kind():
    payment = _payment({SNAPSHOT_METADATA_KEY: _valid_snapshot(kind="other")})
    assert extract_checkout_snapshot(payment) is None


def test_extract_rejects_zero_price():
    payment = _payment({SNAPSHOT_METADATA_KEY: _valid_snapshot(final_price_kopeks=0)})
    assert extract_checkout_snapshot(payment) is None


# --- build_invoice_checkout_snapshot -----------------------------------------

async def test_snapshot_built_from_partial_tariff_cart(monkeypatch):
    cart = {
        "cart_mode": "tariff",
        "tariff_op": "purchase",
        "intent": True,
        "plan_id": 4,
        "plan_code": "solo",
        "period_days": 30,
        "total_price": 25600,
        "offer_type": "hot_invoice_20",
        "offer_id": 55,
        "partial_payment": {
            "base_price_kopeks": 32000,
            "final_price_kopeks": 25600,
            "balance_planned_kopeks": 10000,
            "shortfall_kopeks": 15600,
        },
    }

    async def fake_get_user_cart(user_id):
        return cart

    from app.services import user_cart_service as cart_module

    monkeypatch.setattr(
        cart_module.user_cart_service, "get_user_cart", fake_get_user_cart
    )

    snapshot = await build_invoice_checkout_snapshot(1, 15600)
    assert snapshot["kind"] == "tariff_purchase"
    assert snapshot["final_price_kopeks"] == 25600
    assert snapshot["invoice_amount_kopeks"] == 15600
    assert snapshot["offer_id"] == 55
    assert snapshot["plan_id"] == 4


async def test_snapshot_not_built_without_partial_block(monkeypatch):
    cart = {
        "cart_mode": "tariff",
        "tariff_op": "purchase",
        "intent": True,
        "plan_id": 4,
        "total_price": 25600,
    }

    async def fake_get_user_cart(user_id):
        return cart

    from app.services import user_cart_service as cart_module

    monkeypatch.setattr(
        cart_module.user_cart_service, "get_user_cart", fake_get_user_cart
    )

    assert await build_invoice_checkout_snapshot(1, 25600) is None


async def test_snapshot_not_built_for_legacy_cart(monkeypatch):
    async def fake_get_user_cart(user_id):
        return {"period_days": 30, "total_price": 25600, "saved_cart": True}

    from app.services import user_cart_service as cart_module

    monkeypatch.setattr(
        cart_module.user_cart_service, "get_user_cart", fake_get_user_cart
    )

    assert await build_invoice_checkout_snapshot(1, 25600) is None


async def test_snapshot_not_built_without_cart(monkeypatch):
    async def fake_get_user_cart(user_id):
        return None

    from app.services import user_cart_service as cart_module

    monkeypatch.setattr(
        cart_module.user_cart_service, "get_user_cart", fake_get_user_cart
    )

    assert await build_invoice_checkout_snapshot(1, 25600) is None


# --- активация по snapshot (вебхук: числа из счёта, без пересчёта) -----------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402


def _make_user(balance_kopeks):
    from app.database.models import User

    user = MagicMock(spec=User)
    user.id = 42
    user.telegram_id = 4242
    user.balance_kopeks = balance_kopeks
    user.language = "ru"
    user.subscription = None
    return user


def _patch_snapshot_activation(monkeypatch, user, *, active_sub=None):
    """Заглушки для ленивых импортов _auto_tariff_purchase_from_snapshot."""
    plan = MagicMock()
    plan.id = 4
    plan.code = "solo"
    plan.is_active = True

    subscription = MagicMock()
    transaction = MagicMock()
    finalize_mock = AsyncMock(return_value=(subscription, transaction, False))
    mark_claimed_mock = AsyncMock()
    offer = MagicMock()

    monkeypatch.setattr(
        "app.database.crud.user.get_user_by_id", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        "app.services.plan_pricing_service.get_plan_by_id",
        AsyncMock(return_value=plan),
    )
    # Пересчёта цены быть не должно — упадём, если вызовут.
    monkeypatch.setattr(
        "app.services.plan_pricing_service.get_plan_price",
        AsyncMock(side_effect=AssertionError("get_plan_price must not be called")),
    )
    monkeypatch.setattr(
        "app.handlers.subscription.tariffs._resolve_active_subscription",
        AsyncMock(return_value=active_sub),
    )
    monkeypatch.setattr(
        "app.handlers.subscription.tariffs.finalize_tariff_purchase", finalize_mock
    )
    monkeypatch.setattr(
        "app.handlers.subscription.tariffs._mark_tariff_offer_claimed",
        mark_claimed_mock,
    )
    monkeypatch.setattr(
        "app.database.crud.discount_offer.get_offer_by_id",
        AsyncMock(return_value=offer),
    )
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.record_subscription_purchase_event",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.user_cart_service.get_user_cart",
        AsyncMock(return_value={"cart_mode": "tariff", "plan_id": 4, "period_days": 30}),
    )
    delete_cart_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.user_cart_service.delete_user_cart",
        delete_cart_mock,
    )
    monkeypatch.setattr(
        "app.services.subscription_auto_purchase_service.clear_subscription_checkout_draft",
        AsyncMock(),
    )
    return finalize_mock, mark_claimed_mock, delete_cart_mock


async def test_webhook_activates_by_snapshot_fixed_price(monkeypatch):
    from app.services.subscription_auto_purchase_service import (
        auto_purchase_saved_cart_after_topup,
    )

    # Баланс 100₽ + зачисленный счёт 156₽ = 256₽ — ровно цена из snapshot.
    user = _make_user(25600)
    finalize_mock, mark_claimed_mock, delete_cart_mock = _patch_snapshot_activation(
        monkeypatch, user
    )

    snapshot = _valid_snapshot(offer_id=55, base_price_kopeks=32000)
    db = AsyncMock()

    result = await auto_purchase_saved_cart_after_topup(
        db, user, bot=None, checkout_snapshot=snapshot
    )

    assert result is True
    # Списывается зафиксированная цена, без пересчёта и ревалидации оффера.
    args = finalize_mock.await_args.args
    assert args[4] == 25600
    mark_claimed_mock.assert_awaited_once()
    delete_cart_mock.assert_awaited_once_with(user.id)


async def test_webhook_snapshot_stale_balance_falls_back(monkeypatch):
    from app.services.subscription_auto_purchase_service import (
        auto_purchase_saved_cart_after_topup,
    )

    # Пользователь потратил баланс, пока счёт висел: 256₽ не набирается.
    user = _make_user(20600)
    finalize_mock, _, _ = _patch_snapshot_activation(monkeypatch, user)

    result = await auto_purchase_saved_cart_after_topup(
        AsyncMock(), user, bot=None, checkout_snapshot=_valid_snapshot()
    )

    assert result is False
    finalize_mock.assert_not_awaited()  # баланс не тронут, деньги остались на балансе


async def test_webhook_snapshot_skips_when_active_subscription(monkeypatch):
    from app.services.subscription_auto_purchase_service import (
        auto_purchase_saved_cart_after_topup,
    )

    user = _make_user(25600)
    active_sub = MagicMock()
    active_sub.is_trial = False
    finalize_mock, _, _ = _patch_snapshot_activation(
        monkeypatch, user, active_sub=active_sub
    )

    result = await auto_purchase_saved_cart_after_topup(
        AsyncMock(), user, bot=None, checkout_snapshot=_valid_snapshot()
    )

    assert result is False
    finalize_mock.assert_not_awaited()
