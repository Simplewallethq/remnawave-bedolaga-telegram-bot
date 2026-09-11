"""A/B «доступ за 1 ₽ вместо триала»: назначение варианта, оффер, активация, фолбэк."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.trial_paid_offer_service as service_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.trial_paid_offer_service import (  # noqa: E402
    PURCHASE_SOURCE,
    SNAPSHOT_KEY,
    VARIANT_CONTROL,
    VARIANT_PAID,
    TrialPaidOfferService,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _enable(monkeypatch: pytest.MonkeyPatch, *, percent: int = 100, enabled: bool = True) -> None:
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ENABLED", enabled, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PERCENT", percent, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PARTNER_ID", "p", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PROJECT_ID", "j", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_API_KEY", "k", raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PRICE_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ACCESS_DAYS", 1, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PLAN_CODE", "solo", raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_RENEWAL_PERIOD_DAYS", 30, raising=False)


def _user(**overrides: Any) -> SimpleNamespace:
    user = SimpleNamespace(
        id=42,
        telegram_id=None,
        language="ru",
        balance_kopeks=0,
        subscription=None,
        has_had_paid_subscription=False,
        trial_offer_variant=None,
        created_at=datetime.utcnow(),
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _plan(**overrides: Any) -> SimpleNamespace:
    plan = SimpleNamespace(id=2, code="solo", display_name="Solo", is_active=True)
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


# ------------------------------------------------------------- варианты


def test_assign_variant_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, enabled=False)
    user = _user()
    assert TrialPaidOfferService().assign_variant(user) is None
    assert user.trial_offer_variant is None


@pytest.mark.parametrize(
    ("percent", "roll", "expected"),
    [
        (0, 0.0, VARIANT_CONTROL),
        (100, 0.999, VARIANT_PAID),
        (50, 0.49, VARIANT_PAID),
        (50, 0.5, VARIANT_CONTROL),
    ],
)
def test_assign_variant_by_percent(
    monkeypatch: pytest.MonkeyPatch, percent: int, roll: float, expected: str
) -> None:
    _enable(monkeypatch, percent=percent)
    monkeypatch.setattr(service_module.random, "random", lambda: roll)
    user = _user()
    assert TrialPaidOfferService().assign_variant(user) == expected
    assert user.trial_offer_variant == expected


def test_offer_available_only_for_paid_variant_without_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    service = TrialPaidOfferService()

    assert service.is_offer_available(_user(trial_offer_variant=VARIANT_PAID)) is True
    assert service.is_offer_available(_user(trial_offer_variant=VARIANT_CONTROL)) is False
    assert service.is_offer_available(_user(trial_offer_variant=None)) is False
    assert (
        service.is_offer_available(
            _user(trial_offer_variant=VARIANT_PAID, subscription=SimpleNamespace(id=1))
        )
        is False
    )
    assert (
        service.is_offer_available(
            _user(trial_offer_variant=VARIANT_PAID, has_had_paid_subscription=True)
        )
        is False
    )


def test_offer_unavailable_when_test_or_onepayment_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    user = _user(trial_offer_variant=VARIANT_PAID)
    service = TrialPaidOfferService()

    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ENABLED", False, raising=False)
    assert service.is_offer_available(user) is False

    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_ENABLED", False, raising=False)
    assert service.is_offer_available(user) is False


# ----------------------------------------------------------------- тариф


@pytest.mark.anyio
async def test_resolve_plan_requires_renewal_price(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    plan = _plan()
    price_calls: List[Dict[str, Any]] = []

    async def fake_get_plan_by_code(db: Any, code: str) -> Any:
        return plan if code == "solo" else None

    async def fake_get_plan_price(db: Any, plan_id: int, period_days: int, cohort: str = "new") -> Optional[int]:
        price_calls.append({"plan_id": plan_id, "period_days": period_days, "cohort": cohort})
        return 32_000

    monkeypatch.setattr(service_module, "get_plan_by_code", fake_get_plan_by_code)
    monkeypatch.setattr(service_module, "get_plan_price", fake_get_plan_price)
    monkeypatch.setattr(service_module, "resolve_pricing_cohort", lambda user: "new")

    resolved = await TrialPaidOfferService().resolve_plan(object(), _user())
    assert resolved == (plan, 32_000)
    assert price_calls == [{"plan_id": 2, "period_days": 30, "cohort": "new"}]

    async def no_price(*_: Any, **__: Any) -> Optional[int]:
        return None

    monkeypatch.setattr(service_module, "get_plan_price", no_price)
    assert await TrialPaidOfferService().resolve_plan(object(), _user()) is None

    plan.is_active = False
    monkeypatch.setattr(service_module, "get_plan_price", fake_get_plan_price)
    assert await TrialPaidOfferService().resolve_plan(object(), _user()) is None


@pytest.mark.anyio
async def test_create_offer_invoice_bypasses_minimum_and_carries_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    plan = _plan()
    service = TrialPaidOfferService()

    async def fake_resolve(db: Any, user: Any) -> Any:
        return plan, 32_000

    monkeypatch.setattr(service, "resolve_plan", fake_resolve)

    captured: Dict[str, Any] = {}

    class FakePaymentService:
        def __init__(self, bot: Any) -> None:
            captured["bot"] = bot

        async def create_onepayment_payment(self, db: Any, **kwargs: Any) -> Dict[str, Any]:
            captured.update(kwargs)
            return {"local_payment_id": 7, "payment_url": "https://pay", "order_id": "1p_42_x"}

    monkeypatch.setattr("app.services.payment_service.PaymentService", FakePaymentService)

    result = await service.create_offer_invoice(object(), "bot", _user(trial_offer_variant=VARIANT_PAID))

    assert result["payment_url"] == "https://pay"
    assert result["plan"] is plan
    assert captured["amount_kopeks"] == 100
    assert captured["allow_below_min"] is True
    snapshot = captured["metadata"][SNAPSHOT_KEY]
    assert snapshot["kind"] == "paid_trial"
    assert snapshot["plan_id"] == 2
    assert snapshot["access_days"] == 1
    assert snapshot["renewal_period_days"] == 30
    assert snapshot["renewal_price_kopeks"] == 32_000
    assert snapshot["price_kopeks"] == 100
    assert result["snapshot"] == snapshot


def test_extract_snapshot_ignores_foreign_metadata() -> None:
    assert TrialPaidOfferService.extract_snapshot(None) is None
    assert TrialPaidOfferService.extract_snapshot({"tariff_checkout": {"kind": "tariff_purchase"}}) is None
    assert TrialPaidOfferService.extract_snapshot({SNAPSHOT_KEY: {"kind": "other"}}) is None
    snapshot = {"kind": "paid_trial", "plan_id": 2}
    assert TrialPaidOfferService.extract_snapshot({SNAPSHOT_KEY: snapshot}) == snapshot


# ------------------------------------------------------------- активация


def _snapshot(**overrides: Any) -> Dict[str, Any]:
    snapshot = {
        "v": 1,
        "kind": "paid_trial",
        "user_id": 42,
        "plan_id": 2,
        "plan_code": "solo",
        "access_days": 1,
        "renewal_period_days": 30,
        "renewal_price_kopeks": 32_000,
        "price_kopeks": 100,
    }
    snapshot.update(overrides)
    return snapshot


def _patch_activation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: Any,
    plan: Any,
    active_subscription: Any = None,
) -> Dict[str, Any]:
    calls: Dict[str, Any] = {}
    subscription = SimpleNamespace(
        id=9,
        start_date=datetime(2030, 1, 1),
        end_date=datetime(2030, 1, 2),
        subscription_url="https://sub/42",
        is_paid_trial=True,
    )
    transaction = SimpleNamespace(
        id=777,
        amount_kopeks=100,
        completed_at=datetime(2030, 1, 1),
        created_at=datetime(2030, 1, 1),
        payment_method="onepayment",
    )

    async def fake_finalize(db: Any, db_user: Any, plan_arg: Any, period_days: int, price: int, **kwargs: Any) -> Any:
        calls["finalize"] = {
            "plan": plan_arg,
            "period_days": period_days,
            "price": price,
            **kwargs,
        }
        return subscription, transaction, False

    async def fake_event(db: Any, **kwargs: Any) -> None:
        calls["event"] = kwargs

    async def fake_get_user(db: Any, user_id: int) -> Any:
        return user

    async def fake_get_plan(db: Any, plan_id: int) -> Any:
        return plan if plan_id == plan.id else None

    async def fake_active(db_user: Any) -> Any:
        return active_subscription

    monkeypatch.setattr("app.handlers.subscription.tariffs.finalize_tariff_purchase", fake_finalize)
    monkeypatch.setattr("app.handlers.subscription.tariffs._resolve_active_subscription", fake_active)
    monkeypatch.setattr("app.database.crud.subscription_event.record_subscription_purchase_event", fake_event)
    monkeypatch.setattr("app.database.crud.user.get_user_by_id", fake_get_user)
    monkeypatch.setattr(service_module, "get_plan_by_id", fake_get_plan)
    calls["subscription"] = subscription
    return calls


@pytest.mark.anyio
async def test_activate_from_payment_creates_daily_access_with_monthly_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    user = _user(balance_kopeks=100, trial_offer_variant=VARIANT_PAID)
    plan = _plan()
    calls = _patch_activation(monkeypatch, user=user, plan=plan)
    service = TrialPaidOfferService()
    notify = AsyncMock()
    monkeypatch.setattr(service, "notify_activated", notify)

    db = SimpleNamespace(refresh=AsyncMock())
    assert await service.activate_from_payment(db, user, _snapshot(), bot=None) is True

    finalize = calls["finalize"]
    assert finalize["plan"] is plan
    assert finalize["period_days"] == 30
    assert finalize["access_days"] == 1
    assert finalize["price"] == 100

    event = calls["event"]
    assert event["source"] == PURCHASE_SOURCE
    assert event["period_days"] == 1
    assert event["subscription_id"] == 9
    assert event["extra"]["renewal_period_days"] == 30
    assert event["extra"]["renewal_price_kopeks"] == 32_000
    # Без бота уведомления не шлём.
    notify.assert_not_awaited()


@pytest.mark.anyio
async def test_activate_from_payment_notifies_user_when_bot_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    user = _user(balance_kopeks=100, trial_offer_variant=VARIANT_PAID, telegram_id=1)
    plan = _plan()
    calls = _patch_activation(monkeypatch, user=user, plan=plan)
    service = TrialPaidOfferService()
    notify = AsyncMock()
    monkeypatch.setattr(service, "notify_activated", notify)

    admin_calls: List[Dict[str, Any]] = []

    class FakeAdminNotifications:
        def __init__(self, bot: Any) -> None:
            pass

        async def send_subscription_purchase_notification(self, db: Any, *args: Any, **kwargs: Any) -> bool:
            admin_calls.append({"args": args, **kwargs})
            return True

    monkeypatch.setattr(
        "app.services.admin_notification_service.AdminNotificationService", FakeAdminNotifications
    )

    assert await service.activate_from_payment(SimpleNamespace(refresh=AsyncMock()), user, _snapshot(), bot="bot") is True
    notify.assert_awaited_once()
    assert notify.await_args.args[3] is calls["subscription"]
    assert admin_calls and admin_calls[0]["record_event"] is False


@pytest.mark.anyio
async def test_activate_from_payment_skips_with_active_paid_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    user = _user(balance_kopeks=100)
    plan = _plan()
    active = SimpleNamespace(is_trial=False, plan_id=3)
    calls = _patch_activation(monkeypatch, user=user, plan=plan, active_subscription=active)

    assert await TrialPaidOfferService().activate_from_payment(SimpleNamespace(), user, _snapshot(), bot=None) is False
    assert "finalize" not in calls


@pytest.mark.anyio
async def test_activate_from_payment_replaces_trial_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    user = _user(balance_kopeks=100)
    plan = _plan()
    trial = SimpleNamespace(is_trial=True, plan_id=None)
    calls = _patch_activation(monkeypatch, user=user, plan=plan, active_subscription=trial)

    assert await TrialPaidOfferService().activate_from_payment(SimpleNamespace(refresh=AsyncMock()), user, _snapshot(), bot=None) is True
    assert calls["finalize"]["access_days"] == 1


@pytest.mark.anyio
async def test_activate_from_payment_requires_balance_and_valid_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    plan = _plan()
    poor = _user(balance_kopeks=50)
    calls = _patch_activation(monkeypatch, user=poor, plan=plan)
    service = TrialPaidOfferService()

    assert await service.activate_from_payment(SimpleNamespace(), poor, _snapshot(), bot=None) is False
    assert "finalize" not in calls

    rich = _user(balance_kopeks=100)
    calls = _patch_activation(monkeypatch, user=rich, plan=plan)
    assert await service.activate_from_payment(SimpleNamespace(), rich, _snapshot(plan_id=None), bot=None) is False
    assert await service.activate_from_payment(SimpleNamespace(), rich, _snapshot(access_days=0), bot=None) is False
    assert "finalize" not in calls


@pytest.mark.anyio
async def test_activate_from_payment_skips_unknown_or_inactive_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    user = _user(balance_kopeks=100)
    calls = _patch_activation(monkeypatch, user=user, plan=_plan(is_active=False))

    assert await TrialPaidOfferService().activate_from_payment(SimpleNamespace(), user, _snapshot(), bot=None) is False
    assert "finalize" not in calls


# ----------------------------------------------------------------- фолбэк


@pytest.mark.anyio
async def test_fallback_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_FALLBACK_TRIAL_MINUTES", 0, raising=False)
    service = TrialPaidOfferService()
    listed = AsyncMock(return_value=[_user()])
    monkeypatch.setattr(service, "list_fallback_candidates", listed)

    assert await service.grant_fallback_trials(SimpleNamespace(), None) == 0
    listed.assert_not_awaited()


@pytest.mark.anyio
async def test_fallback_grants_trial_to_unpaid_users_after_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_FALLBACK_TRIAL_MINUTES", 30, raising=False)
    service = TrialPaidOfferService()

    candidates = [_user(id=1, telegram_id=None), _user(id=2, telegram_id=None)]
    captured: Dict[str, Any] = {}

    async def fake_list(db: Any, *, before: datetime, not_before: Any = None, limit: Any = None) -> List[Any]:
        captured["before"] = before
        captured["not_before"] = not_before
        captured["limit"] = limit
        return candidates

    activated: List[int] = []

    async def fake_activate(bot: Any, db: Any, user: Any, **kwargs: Any) -> bool:
        activated.append(user.id)
        return user.id != 2  # второму триал не выдался

    monkeypatch.setattr(service, "list_fallback_candidates", fake_list)
    monkeypatch.setattr("app.handlers.start.activate_trial_for_user", fake_activate)
    notify = AsyncMock()
    monkeypatch.setattr(service, "_notify_fallback_trial", notify)

    before_call = datetime.utcnow()
    db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())
    granted = await service.grant_fallback_trials(db, None)

    assert granted == 1
    assert activated == [1, 2]
    assert notify.await_count == 1
    # Момент выдачи фиксируется только у того, кому триал реально выдан.
    assert candidates[0].paid_trial_fallback_at is not None
    assert getattr(candidates[1], "paid_trial_fallback_at", None) is None
    assert db.commit.await_count == 1
    # Порог — N минут назад от «сейчас».
    assert timedelta(minutes=29, seconds=59) < before_call - captured["before"] <= timedelta(minutes=30, seconds=5)
    # Бэклог ограничен: не старше MAX_AGE_HOURS и пачкой.
    assert timedelta(hours=23, minutes=59) < before_call - captured["not_before"] <= timedelta(hours=24, seconds=5)
    assert captured["limit"] == service.FALLBACK_BATCH_LIMIT


@pytest.mark.anyio
async def test_fallback_candidates_query_filters_variant_and_missing_subscription() -> None:
    captured: Dict[str, Any] = {}

    class FakeResult:
        def scalars(self) -> "FakeResult":
            return self

        def unique(self) -> "FakeResult":
            return self

        def all(self) -> List[Any]:
            return []

    class FakeDb:
        async def execute(self, statement: Any) -> FakeResult:
            captured["sql"] = str(statement)
            return FakeResult()

    await TrialPaidOfferService().list_fallback_candidates(FakeDb(), before=datetime(2030, 1, 1))
    sql = captured["sql"]
    assert "users.trial_offer_variant" in sql
    assert "subscriptions.id IS NULL" in sql
    assert "users.has_had_paid_subscription" in sql
    assert "users.created_at <" in sql
    assert "users.paid_trial_fallback_at IS NULL" in sql
    assert "users.created_at >=" not in sql
    assert "LIMIT" not in sql

    await TrialPaidOfferService().list_fallback_candidates(
        FakeDb(), before=datetime(2030, 1, 1), not_before=datetime(2029, 12, 31), limit=50
    )
    sql = captured["sql"]
    assert "users.created_at >=" in sql
    assert "LIMIT" in sql
    assert "ORDER BY users.created_at DESC" in sql


@pytest.mark.anyio
async def test_fallback_notification_goes_through_the_users_own_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пользователь зеркала получает сообщение из зеркала, а не из основного бота."""
    from app.utils import bot_registry

    bot_registry.clear()
    primary = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    mirror = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    bot_registry.register_bot(111, Path("images/logo.png"), primary)
    bot_registry.register_bot(222, Path("images/logo.png"), mirror)
    monkeypatch.setattr("os.path.exists", lambda _p: False)

    async def no_refresh(*_a: Any, **_k: Any) -> None:
        return None

    db = SimpleNamespace(refresh=no_refresh)
    service = TrialPaidOfferService()
    await service._notify_fallback_trial(db, primary, _user(telegram_id=5, bot_id=222, subscription=None))
    assert mirror.send_message.await_count == 1
    assert primary.send_message.await_count == 0

    # Незарегистрированный бот — шлём тем, что дали.
    await service._notify_fallback_trial(db, primary, _user(telegram_id=6, bot_id=999, subscription=None))
    assert primary.send_message.await_count == 1
    bot_registry.clear()
