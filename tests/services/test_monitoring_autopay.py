"""Автоплатеж: тарифные подписки списываются по цене тарифа за свой период,
legacy à-la-carte — по старому пересчету на 30 дней."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as monitoring_module
from app.services.monitoring_service import MonitoringService


def _make_db(subscriptions):
    result = MagicMock()
    result.scalars.return_value.all.return_value = subscriptions
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _make_user(**overrides):
    user = SimpleNamespace(
        id=1,
        telegram_id=1001,
        balance_kopeks=500_000,
        language="ru",
        created_at=datetime(2024, 1, 1),
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        tariff_pricing_cohort_override=None,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _make_subscription(user, **overrides):
    subscription = SimpleNamespace(
        id=10,
        user=user,
        user_id=user.id,
        end_date=datetime.utcnow() + timedelta(days=1),
        autopay_days_before=3,
        plan_id=None,
        plan_period_days=None,
        plan=None,
    )
    for key, value in overrides.items():
        setattr(subscription, key, value)
    subscription.is_legacy = subscription.plan_id is None
    return subscription


def _make_service(monkeypatch):
    service = MonitoringService(bot=None)
    service.subscription_service = MagicMock()
    service.subscription_service.calculate_renewal_price = AsyncMock(return_value=100_000)
    service.subscription_service.update_remnawave_user = AsyncMock()

    monkeypatch.setattr(monitoring_module, "record_subscription_renewal_event", AsyncMock())
    monkeypatch.setattr(service, "_log_monitoring_event", AsyncMock())
    monkeypatch.setattr(service, "_notify_cabinet_autopay_failed", AsyncMock())
    return service


async def test_autopay_charges_tariff_price_for_plan_period(monkeypatch):
    import app.handlers.subscription.tariffs as tariffs_module
    import app.services.plan_pricing_service as pricing_module

    user = _make_user(balance_kopeks=500_000)
    plan = SimpleNamespace(id=7, code="plus", display_name="Plus")
    subscription = _make_subscription(user, plan_id=7, plan_period_days=90, plan=plan)

    service = _make_service(monkeypatch)
    db = _make_db([subscription])

    get_plan_price = AsyncMock(return_value=270_000)
    monkeypatch.setattr(pricing_module, "get_plan_price", get_plan_price)

    async def fake_finalize(db_, user_, sub_, plan_, period_days_, price_kopeks_):
        old_end_date = sub_.end_date
        sub_.end_date = old_end_date + timedelta(days=period_days_)
        user_.balance_kopeks -= price_kopeks_
        return sub_, SimpleNamespace(id=555, amount_kopeks=price_kopeks_), old_end_date

    finalize = AsyncMock(side_effect=fake_finalize)
    monkeypatch.setattr(tariffs_module, "finalize_tariff_renewal", finalize)

    await service._process_autopayments(db)

    # Цена берется из тарифа за период подписки, а не из legacy-расчета.
    get_plan_price.assert_awaited_once()
    assert get_plan_price.await_args.args[1:] == (7, 90)
    service.subscription_service.calculate_renewal_price.assert_not_awaited()

    finalize.assert_awaited_once()
    assert finalize.await_args.args[4] == 90
    assert finalize.await_args.args[5] == 270_000
    assert user.balance_kopeks == 230_000

    renewal_event = monitoring_module.record_subscription_renewal_event
    assert renewal_event.await_args.kwargs["period_days"] == 90
    assert renewal_event.await_args.kwargs["amount_kopeks"] == 270_000
    assert renewal_event.await_args.kwargs["transaction_id"] == 555

    # Remnawave синхронизирует сам finalize_tariff_renewal.
    service.subscription_service.update_remnawave_user.assert_not_awaited()


async def test_autopay_skips_tariff_without_configured_price(monkeypatch):
    import app.handlers.subscription.tariffs as tariffs_module
    import app.services.plan_pricing_service as pricing_module

    user = _make_user()
    plan = SimpleNamespace(id=7, code="plus", display_name="Plus")
    subscription = _make_subscription(user, plan_id=7, plan_period_days=180, plan=plan)

    service = _make_service(monkeypatch)
    db = _make_db([subscription])

    monkeypatch.setattr(pricing_module, "get_plan_price", AsyncMock(return_value=None))
    finalize = AsyncMock()
    monkeypatch.setattr(tariffs_module, "finalize_tariff_renewal", finalize)

    await service._process_autopayments(db)

    finalize.assert_not_awaited()
    service.subscription_service.calculate_renewal_price.assert_not_awaited()
    service._notify_cabinet_autopay_failed.assert_not_awaited()
    assert user.balance_kopeks == 500_000


async def test_autopay_keeps_legacy_path_for_alacarte_subscription(monkeypatch):
    user = _make_user(balance_kopeks=150_000)
    subscription = _make_subscription(user)

    service = _make_service(monkeypatch)
    db = _make_db([subscription])

    subtract = AsyncMock(return_value=True)
    extend = AsyncMock()
    monkeypatch.setattr(monitoring_module, "subtract_user_balance", subtract)
    monkeypatch.setattr(monitoring_module, "extend_subscription", extend)

    await service._process_autopayments(db)

    service.subscription_service.calculate_renewal_price.assert_awaited_once()
    assert service.subscription_service.calculate_renewal_price.await_args.args[1] == 30
    subtract.assert_awaited_once()
    assert subtract.await_args.args[2] == 100_000
    assert extend.await_args.args[2] == 30
    service.subscription_service.update_remnawave_user.assert_awaited_once()

    renewal_event = monitoring_module.record_subscription_renewal_event
    assert renewal_event.await_args.kwargs["period_days"] == 30


async def test_autopay_reports_insufficient_balance(monkeypatch):
    import app.handlers.subscription.tariffs as tariffs_module
    import app.services.plan_pricing_service as pricing_module

    user = _make_user(balance_kopeks=1_000)
    plan = SimpleNamespace(id=7, code="plus", display_name="Plus")
    subscription = _make_subscription(user, plan_id=7, plan_period_days=30, plan=plan)

    service = _make_service(monkeypatch)
    db = _make_db([subscription])

    monkeypatch.setattr(pricing_module, "get_plan_price", AsyncMock(return_value=100_000))
    finalize = AsyncMock()
    monkeypatch.setattr(tariffs_module, "finalize_tariff_renewal", finalize)

    await service._process_autopayments(db)

    finalize.assert_not_awaited()
    service._notify_cabinet_autopay_failed.assert_awaited_once()
    assert service._notify_cabinet_autopay_failed.await_args.args[3] == 100_000
