"""Планировщик автосписаний 1Payment в MonitoringService."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.monitoring_service as monitoring_module  # noqa: E402
import app.services.payment_service as payment_service_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.monitoring_service import MonitoringService  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self, user: Any) -> None:
        self.user = user

    async def execute(self, *_: Any, **__: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.user)

    async def commit(self) -> None:
        return None

    async def refresh(self, *_: Any, **__: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _user(balance: int, end_date: datetime, *, legacy: bool = True) -> SimpleNamespace:
    subscription = SimpleNamespace(
        id=9,
        user_id=42,
        status="active",
        is_trial=False,
        end_date=end_date,
        plan_id=None if legacy else 3,
        plan_period_days=None if legacy else 30,
        plan=None,
        is_legacy=legacy,
    )
    return SimpleNamespace(
        id=42,
        telegram_id=None,
        language="ru",
        balance_kopeks=balance,
        subscription=subscription,
    )


def _binding(**overrides: Any) -> SimpleNamespace:
    binding = SimpleNamespace(
        id=5,
        user_id=42,
        active_user_id=42,
        token="sbp_t",
        status="ACTIVE",
        failed_attempts=0,
        last_charge_at=None,
        last_charged_period_end=None,
    )
    for key, value in overrides.items():
        setattr(binding, key, value)
    return binding


class FakePaymentService:
    def __init__(self, result: Any = "payment") -> None:
        self.result = result
        self.charges: List[Dict[str, Any]] = []
        self.failed_notifications: List[Dict[str, Any]] = []

    async def charge_onepayment_binding(self, db: Any, binding: Any, **kwargs: Any) -> Any:
        self.charges.append(kwargs)
        if self.result == "payment":
            return SimpleNamespace(user_data="1p_42_r1")
        return self.result

    async def _notify_onepayment_recurring_failed(self, user: Any, amount: int, **kwargs: Any) -> None:
        self.failed_notifications.append({"amount": amount, **kwargs})


def _service(monkeypatch: pytest.MonkeyPatch, *, renewal_price: int = 30_000) -> MonitoringService:
    service = MonitoringService.__new__(MonitoringService)
    service.bot = None
    service._onepayment_failed_notified = {}
    service._notified_users = set()

    async def calculate_renewal_price(subscription: Any, days: int, db: Any, *, user: Any) -> int:
        return renewal_price

    async def update_remnawave_user(db: Any, subscription: Any, **kwargs: Any) -> None:
        return None

    service.subscription_service = SimpleNamespace(
        calculate_renewal_price=calculate_renewal_price,
        update_remnawave_user=update_remnawave_user,
    )

    async def log_event(*_: Any, **__: Any) -> None:
        return None

    service._log_monitoring_event = log_event  # type: ignore[method-assign]

    monkeypatch.setattr(settings, "ONEPAYMENT_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_DAYS_BEFORE", 1, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_MAX_ATTEMPTS", 3, raising=False)
    return service


def _patch_crud(monkeypatch: pytest.MonkeyPatch, *, pending: Any = None) -> Dict[str, Any]:
    calls: Dict[str, Any] = {}

    async def fake_pending(db: Any, binding_id: int, **kwargs: Any) -> Any:
        return pending

    async def fake_update_binding(db: Any, binding: Any, **kwargs: Any) -> Any:
        calls.setdefault("binding_updates", []).append(kwargs)
        return binding

    async def fake_get_binding(db: Any, binding_id: int) -> Any:
        return _binding(status=calls.get("binding_status", "ACTIVE"))

    async def fake_subtract(db: Any, user: Any, amount: int, *args: Any, **kwargs: Any) -> bool:
        calls["subtracted"] = amount
        user.balance_kopeks -= amount
        return True

    async def fake_extend(db: Any, subscription: Any, days: int) -> Any:
        calls["extended_days"] = days
        subscription.end_date = subscription.end_date + timedelta(days=days)
        return subscription

    async def fake_event(db: Any, **kwargs: Any) -> None:
        calls["event"] = kwargs

    monkeypatch.setattr(payment_service_module, "get_pending_recurring_payment_for_binding", fake_pending, raising=False)
    monkeypatch.setattr(payment_service_module, "update_onepayment_binding", fake_update_binding, raising=False)
    monkeypatch.setattr(payment_service_module, "get_onepayment_binding_by_id", fake_get_binding, raising=False)
    monkeypatch.setattr(monitoring_module, "subtract_user_balance", fake_subtract)
    monkeypatch.setattr(monitoring_module, "extend_subscription", fake_extend)
    monkeypatch.setattr(monitoring_module, "record_subscription_renewal_event", fake_event)
    return calls


NOW = datetime(2030, 1, 9, 12, 0, 0)
END = datetime(2030, 1, 10, 6, 0, 0)


@pytest.mark.anyio
async def test_shortfall_triggers_token_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    calls = _patch_crud(monkeypatch)
    user = _user(balance=12_000, end_date=END)
    payments = FakePaymentService()

    outcome = await service._process_onepayment_binding(FakeSession(user), payments, _binding(), NOW)

    assert outcome == "charged"
    charge = payments.charges[0]
    assert charge["amount_kopeks"] == 18_000  # 30 000 − 12 000
    snapshot = charge["renewal_snapshot"]
    assert snapshot["subscription_id"] == 9
    assert snapshot["price_kopeks"] == 30_000
    assert snapshot["shortfall_kopeks"] == 18_000
    assert snapshot["period_end"] == END.isoformat()
    assert snapshot["is_legacy"] is True
    assert "subtracted" not in calls


@pytest.mark.anyio
async def test_small_shortfall_is_clamped_to_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    user = _user(balance=27_000, end_date=END)
    payments = FakePaymentService()

    await service._process_onepayment_binding(FakeSession(user), payments, _binding(), NOW)

    assert payments.charges[0]["amount_kopeks"] == 10_000


@pytest.mark.anyio
async def test_balance_covers_price_renews_without_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    calls = _patch_crud(monkeypatch)
    user = _user(balance=35_000, end_date=END)
    payments = FakePaymentService()

    outcome = await service._process_onepayment_binding(FakeSession(user), payments, _binding(), NOW)

    assert outcome == "renewed_from_balance"
    assert not payments.charges
    assert calls["subtracted"] == 30_000
    assert calls["extended_days"] == 30
    assert calls["event"]["source"] == "onepayment_recurring"
    update = calls["binding_updates"][-1]
    assert update["set_last_charged_period_end"] is True
    assert update["last_charged_period_end"] == END


@pytest.mark.anyio
async def test_balance_branch_ignores_daily_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Страховочная ветка после потерянного продления не должна ждать сутки."""
    service = _service(monkeypatch)
    calls = _patch_crud(monkeypatch)
    user = _user(balance=35_000, end_date=END)

    outcome = await service._process_onepayment_binding(
        FakeSession(user), FakePaymentService(), _binding(last_charge_at=NOW - timedelta(hours=1)), NOW
    )
    assert outcome == "renewed_from_balance"
    assert calls["subtracted"] == 30_000


@pytest.mark.anyio
async def test_skips_when_period_already_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    user = _user(balance=0, end_date=END)
    payments = FakePaymentService()

    outcome = await service._process_onepayment_binding(
        FakeSession(user), payments, _binding(last_charged_period_end=END), NOW
    )
    assert outcome == "skipped"
    assert not payments.charges


@pytest.mark.anyio
async def test_skips_when_pending_charge_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch, pending=SimpleNamespace(user_data="1p_42_rpending"))
    user = _user(balance=0, end_date=END)
    payments = FakePaymentService()

    outcome = await service._process_onepayment_binding(FakeSession(user), payments, _binding(), NOW)
    assert outcome == "skipped"
    assert not payments.charges


@pytest.mark.anyio
async def test_skips_second_charge_within_24h(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    user = _user(balance=0, end_date=END)
    payments = FakePaymentService()

    outcome = await service._process_onepayment_binding(
        FakeSession(user), payments, _binding(last_charge_at=NOW - timedelta(hours=5)), NOW
    )
    assert outcome == "skipped"
    assert not payments.charges


@pytest.mark.anyio
async def test_skips_inactive_or_trial_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    payments = FakePaymentService()

    expired = _user(balance=0, end_date=END)
    expired.subscription.status = "expired"
    assert await service._process_onepayment_binding(FakeSession(expired), payments, _binding(), NOW) == "skipped"

    trial = _user(balance=0, end_date=END)
    trial.subscription.is_trial = True
    assert await service._process_onepayment_binding(FakeSession(trial), payments, _binding(), NOW) == "skipped"
    assert not payments.charges


@pytest.mark.anyio
async def test_charge_failure_notifies_once_per_period(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    user = _user(balance=0, end_date=END)
    payments = FakePaymentService(result=None)

    first = await service._process_onepayment_binding(FakeSession(user), payments, _binding(), NOW)
    second = await service._process_onepayment_binding(FakeSession(user), payments, _binding(), NOW)

    assert first == "failed" and second == "failed"
    assert len(payments.failed_notifications) == 1
    assert payments.failed_notifications[0]["amount"] == 30_000


@pytest.mark.anyio
async def test_recurring_disabled_globally_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    monkeypatch.setattr(settings, "ONEPAYMENT_ENABLED", False, raising=False)
    called = False

    async def fake_list(*_: Any, **__: Any) -> List[Any]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(payment_service_module, "list_onepayment_bindings_due", fake_list, raising=False)

    assert settings.is_onepayment_recurring_enabled() is False
    await service._process_onepayment_recurring(FakeSession(None))
    # Метод сам по себе не гейтится (гейт — в цикле), но без привязок ничего не делает.
    assert called is True


@pytest.mark.anyio
async def test_autopayments_skip_insufficient_notice_with_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """При активной привязке баланс-автоплатёж не шлёт «недостаточно средств»."""
    service = _service(monkeypatch)
    monkeypatch.setattr(settings, "ONEPAYMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PARTNER_ID", "1", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PROJECT_ID", "2", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_API_KEY", "k", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_ENABLED", True, raising=False)

    async def fake_active(db: Any, user_id: int) -> Any:
        return _binding()

    monkeypatch.setattr(payment_service_module, "get_active_onepayment_binding_for_user", fake_active, raising=False)
    assert await service._has_active_onepayment_binding(FakeSession(None), 42) is True

    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_ENABLED", False, raising=False)
    assert await service._has_active_onepayment_binding(FakeSession(None), 42) is False


# ---------------------------------------------- суточная подписка «за 1 ₽»


def _paid_trial_user(balance: int, end_date: datetime) -> SimpleNamespace:
    user = _user(balance=balance, end_date=end_date, legacy=False)
    user.subscription.is_paid_trial = True
    return user


@pytest.mark.anyio
async def test_paid_trial_retries_after_one_hour_not_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    user = _paid_trial_user(balance=0, end_date=NOW + timedelta(hours=2))
    plan = SimpleNamespace(id=3, code="solo")

    async def fake_resolve(db: Any, subscription: Any, user_arg: Any) -> Any:
        return plan, 30, 32_000

    monkeypatch.setattr(service, "_resolve_tariff_autopay_charge", fake_resolve)

    payments = FakePaymentService()
    outcome = await service._process_onepayment_binding(
        FakeSession(user), payments, _binding(last_charge_at=NOW - timedelta(minutes=30)), NOW
    )
    assert outcome == "skipped"
    assert not payments.charges

    outcome = await service._process_onepayment_binding(
        FakeSession(user), payments, _binding(last_charge_at=NOW - timedelta(hours=1, minutes=1)), NOW
    )
    assert outcome == "charged"
    charge = payments.charges[0]
    assert charge["amount_kopeks"] == 32_000
    assert charge["renewal_snapshot"]["period_days"] == 30
    assert charge["renewal_snapshot"]["plan_id"] == 3


@pytest.mark.anyio
async def test_regular_subscription_keeps_daily_retry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    _patch_crud(monkeypatch)
    user = _user(balance=0, end_date=END)
    payments = FakePaymentService()

    outcome = await service._process_onepayment_binding(
        FakeSession(user), payments, _binding(last_charge_at=NOW - timedelta(hours=2)), NOW
    )
    assert outcome == "skipped"


@pytest.mark.anyio
async def test_recurring_passes_hour_window_for_paid_trials(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_DAYS_BEFORE", 1, raising=False)
    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_RECURRING_HOURS_BEFORE", 3, raising=False)
    captured: Dict[str, Any] = {}

    async def fake_list(db: Any, *, before: datetime, paid_trial_before: Optional[datetime] = None) -> List[Any]:
        captured["before"] = before
        captured["paid_trial_before"] = paid_trial_before
        return []

    monkeypatch.setattr(payment_service_module, "list_onepayment_bindings_due", fake_list, raising=False)

    started = datetime.utcnow()
    await service._process_onepayment_recurring(FakeSession(None))

    assert captured["before"] - started >= timedelta(days=1) - timedelta(seconds=5)
    assert timedelta(hours=3) - timedelta(seconds=5) <= captured["paid_trial_before"] - started <= timedelta(hours=3, seconds=5)
