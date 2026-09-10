"""Тесты миксина 1Payment: создание счёта, колбеки, привязка токена, рекуррент."""

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

import app.services.payment.onepayment as onepayment_mixin_module  # noqa: E402
import app.services.payment_service as payment_service_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.payment_service import PaymentService  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class DummySession:
    def __init__(self) -> None:
        self.commits = 0
        self.execute_results: List[Any] = []

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, *_: Any, **__: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, *_: Any, **__: Any) -> Any:
        if self.execute_results:
            return self.execute_results.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: None, rowcount=0)


class StubOnePaymentService:
    def __init__(self, response: Optional[Dict[str, Any]] = None, *, error: Optional[Exception] = None) -> None:
        self.response = response or {}
        self.error = error
        self.calls: List[Dict[str, Any]] = []
        self.charges: List[Dict[str, Any]] = []
        self.is_configured = True

    async def create_payment(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    async def charge_token(self, **kwargs: Any) -> Dict[str, Any]:
        self.charges.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class DummyPayment:
    def __init__(self, **overrides: Any) -> None:
        self.id = 1
        self.user_id = 42
        self.user_data = "1p_42_abc123"
        self.provider_order_id: Optional[str] = None
        self.amount_kopeks = 15_000
        self.currency = "RUB"
        self.description = "Тариф"
        self.status = "INIT"
        self.status_code: Optional[str] = None
        self.is_paid = False
        self.paid_at: Optional[datetime] = None
        self.url = "https://merchant.1payment.com/x"
        self.is_recurring = False
        self.binding_id: Optional[int] = None
        self.metadata_json: Dict[str, Any] = {}
        self.callback_payload: Optional[Dict[str, Any]] = None
        self.transaction_id: Optional[int] = None
        self.created_at = datetime.utcnow()
        for key, value in overrides.items():
            setattr(self, key, value)


class DummyUser:
    def __init__(self, balance: int = 0) -> None:
        self.id = 42
        self.telegram_id = None  # без Telegram — уведомления пропускаются
        self.language = "ru"
        self.balance_kopeks = balance
        self.has_made_first_topup = False
        self.updated_at = datetime.utcnow()
        self.subscription = None

    def get_primary_promo_group(self) -> None:
        return None


class DummyBinding:
    def __init__(self) -> None:
        self.id = 5
        self.user_id = 42
        self.active_user_id = 42
        self.token = "sbp_t_old"
        self.status = "ACTIVE"
        self.failed_attempts = 0
        self.last_charge_at: Optional[datetime] = None
        self.last_charged_period_end: Optional[datetime] = None


def _make_service(stub: Optional[StubOnePaymentService]) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.onepayment_service = stub
    return service


def _patch_finalize_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payment: DummyPayment,
    user: DummyUser,
    cas_result: bool = True,
) -> Dict[str, Any]:
    """Подменяет CRUD-обёртки и внешние сервисы, собирая вызовы в словарь."""
    calls: Dict[str, Any] = {"updates": [], "transactions": [], "bindings": [], "router": []}

    async def fake_update(db: Any, payment_arg: Any, **kwargs: Any) -> DummyPayment:
        calls["updates"].append(kwargs)
        for key, value in kwargs.items():
            if key == "metadata":
                payment_arg.metadata_json = value
            elif key == "callback_payload":
                payment_arg.callback_payload = value
            elif key == "provider_order_id" and value is not None:
                payment_arg.provider_order_id = value
            elif key in ("status", "status_code", "is_paid", "paid_at", "url", "expires_at"):
                if value is not None:
                    setattr(payment_arg, key, value)
        return payment_arg

    async def fake_cas(db: Any, payment_id: int, **_: Any) -> bool:
        calls["cas"] = payment_id
        if cas_result:
            payment.is_paid = True
            payment.status = "SUCCESS"
        return cas_result

    async def fake_get_by_id(db: Any, payment_id: int) -> DummyPayment:
        return payment

    async def fake_get_user(db: Any, user_id: int) -> DummyUser:
        return user

    async def fake_create_transaction(db: Any, **kwargs: Any) -> SimpleNamespace:
        calls["transactions"].append(kwargs)
        return SimpleNamespace(id=777, **kwargs)

    async def fake_link(db: Any, payment_arg: Any, transaction_id: int) -> DummyPayment:
        payment_arg.transaction_id = transaction_id
        return payment_arg

    async def fake_upsert(db: Any, **kwargs: Any) -> DummyBinding:
        calls["bindings"].append(kwargs)
        binding = DummyBinding()
        binding.token = kwargs["token"]
        return binding

    async def fake_router(db: Any, **kwargs: Any) -> None:
        calls["router"].append(kwargs)

    async def fake_referral(*_: Any, **__: Any) -> None:
        return None

    async def fake_has_cart(user_id: int) -> bool:
        return False

    async def fake_auto_purchase(*_: Any, **__: Any) -> bool:
        return False

    async def fake_get_binding(db: Any, binding_id: int) -> DummyBinding:
        return DummyBinding()

    async def fake_get_binding_for_update(db: Any, binding_id: int) -> DummyBinding:
        return DummyBinding()

    async def fake_update_binding(db: Any, binding: Any, **kwargs: Any) -> Any:
        calls.setdefault("binding_updates", []).append(kwargs)
        return binding

    monkeypatch.setattr(payment_service_module, "update_onepayment_payment", fake_update, raising=False)
    monkeypatch.setattr(payment_service_module, "mark_onepayment_payment_paid_cas", fake_cas, raising=False)
    monkeypatch.setattr(payment_service_module, "get_onepayment_payment_by_id", fake_get_by_id, raising=False)
    monkeypatch.setattr(payment_service_module, "get_user_by_id", fake_get_user, raising=False)
    monkeypatch.setattr(payment_service_module, "create_transaction", fake_create_transaction, raising=False)
    monkeypatch.setattr(payment_service_module, "link_onepayment_payment_to_transaction", fake_link, raising=False)
    monkeypatch.setattr(payment_service_module, "upsert_active_onepayment_binding", fake_upsert, raising=False)
    monkeypatch.setattr(payment_service_module, "get_onepayment_binding_by_id", fake_get_binding, raising=False)
    monkeypatch.setattr(
        payment_service_module, "get_onepayment_binding_by_id_for_update", fake_get_binding_for_update, raising=False
    )
    monkeypatch.setattr(payment_service_module, "update_onepayment_binding", fake_update_binding, raising=False)
    monkeypatch.setattr(onepayment_mixin_module, "_record_router_payment", fake_router)
    monkeypatch.setattr(onepayment_mixin_module, "auto_purchase_saved_cart_after_topup", fake_auto_purchase)
    monkeypatch.setattr("app.services.referral_service.process_referral_topup", fake_referral, raising=False)
    monkeypatch.setattr("app.services.user_cart_service.user_cart_service.has_user_cart", fake_has_cart, raising=False)
    return calls


# ------------------------------------------------------------------ создание


@pytest.mark.anyio
async def test_create_onepayment_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubOnePaymentService(
        {"url": "https://merchant.1payment.com/x", "order_id": None, "status": None, "method": "init_form", "raw": {}}
    )
    service = _make_service(stub)
    db = DummySession()
    captured: Dict[str, Any] = {}

    async def fake_create(db_arg: Any, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(id=555)

    async def fake_snapshot(user_id: int, amount: int) -> Dict[str, Any]:
        return {"kind": "tariff_purchase", "final_price_kopeks": 20_000}

    monkeypatch.setattr(payment_service_module, "create_onepayment_payment", fake_create, raising=False)
    monkeypatch.setattr(onepayment_mixin_module, "build_invoice_checkout_snapshot", fake_snapshot)
    monkeypatch.setattr(settings, "ONEPAYMENT_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_MAX_AMOUNT_KOPEKS", 1_000_000, raising=False)

    result = await service.create_onepayment_payment(
        db, user_id=42, amount_kopeks=15_000, description="Тариф", language="ru", metadata={"payment_router": {"v": 1}}
    )

    assert result is not None
    assert result["local_payment_id"] == 555
    assert result["payment_url"] == "https://merchant.1payment.com/x"
    assert result["order_id"].startswith("1p_42_")
    assert stub.calls[0]["subscribe"] is True
    assert stub.calls[0]["amount_kopeks"] == 15_000
    assert captured["user_id"] == 42
    assert captured["metadata"]["tariff_checkout"]["kind"] == "tariff_purchase"
    assert captured["metadata"]["payment_router"] == {"v": 1}


@pytest.mark.anyio
async def test_create_onepayment_payment_respects_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubOnePaymentService({"url": "x"})
    service = _make_service(stub)
    monkeypatch.setattr(settings, "ONEPAYMENT_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_MAX_AMOUNT_KOPEKS", 20_000, raising=False)

    assert await service.create_onepayment_payment(DummySession(), user_id=1, amount_kopeks=5_000, description="x") is None
    assert await service.create_onepayment_payment(DummySession(), user_id=1, amount_kopeks=25_000, description="x") is None
    assert not stub.calls


@pytest.mark.anyio
async def test_create_onepayment_payment_without_service() -> None:
    service = _make_service(None)
    assert await service.create_onepayment_payment(DummySession(), user_id=1, amount_kopeks=15_000, description="x") is None


# -------------------------------------------------------------------- колбеки


def _lookup(monkeypatch: pytest.MonkeyPatch, payment: Optional[DummyPayment]) -> None:
    async def by_user_data(db: Any, user_data: str) -> Optional[DummyPayment]:
        return payment if payment and payment.user_data == user_data else None

    async def by_order(db: Any, order_id: str) -> Optional[DummyPayment]:
        return None

    monkeypatch.setattr(payment_service_module, "get_onepayment_payment_by_user_data", by_user_data, raising=False)
    monkeypatch.setattr(payment_service_module, "get_onepayment_payment_by_provider_order_id", by_order, raising=False)


@pytest.mark.anyio
async def test_webhook_success_credits_balance_and_saves_token(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    db = DummySession()
    payment = DummyPayment()
    user = DummyUser(balance=1_000)
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)
    monkeypatch.setattr(settings, "ONEPAYMENT_ACCEPT_TEST_PAYMENTS", False, raising=False)

    payload = {
        "payment_type": "sbp",
        "order_id": "8p3b",
        "status": "3",
        "merchant_price": "150",
        "user_data": payment.user_data,
        "token": "sbp_t_new",
    }
    assert await service.process_onepayment_webhook(db, payload) is True

    assert calls["cas"] == payment.id
    assert user.balance_kopeks == 16_000
    assert len(calls["transactions"]) == 1
    assert calls["transactions"][0]["external_id"] == "8p3b"
    assert calls["bindings"][0]["token"] == "sbp_t_new"
    assert calls["bindings"][0]["user_id"] == 42
    assert calls["router"][0]["gateway"] == "onepayment"
    assert payment.transaction_id == 777


@pytest.mark.anyio
async def test_webhook_success_duplicate_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment(is_paid=True, status="SUCCESS", transaction_id=1)
    user = DummyUser()
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)

    payload = {"status": "3", "user_data": payment.user_data, "order_id": "8p3b"}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True
    assert "cas" not in calls
    assert not calls["transactions"]


@pytest.mark.anyio
async def test_webhook_success_cas_loser_does_not_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment()
    user = DummyUser()
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user, cas_result=False)

    payload = {"status": "3", "user_data": payment.user_data}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True
    assert calls["cas"] == payment.id
    assert not calls["transactions"]
    assert user.balance_kopeks == 0


@pytest.mark.anyio
async def test_webhook_unknown_payment_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    _lookup(monkeypatch, None)
    assert await service.process_onepayment_webhook(DummySession(), {"status": "3", "user_data": "nope"}) is True


@pytest.mark.anyio
async def test_webhook_test_flag_ignored_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment()
    user = DummyUser()
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)
    monkeypatch.setattr(settings, "ONEPAYMENT_ACCEPT_TEST_PAYMENTS", False, raising=False)

    payload = {"status": "3", "user_data": payment.user_data, "test": "1"}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True
    assert not calls["updates"]
    assert "cas" not in calls


@pytest.mark.anyio
async def test_webhook_failure_marks_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment()
    user = DummyUser()
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)

    payload = {"status": "4", "user_data": payment.user_data, "status_code": "05"}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True
    assert payment.status == "FAILURE"
    assert payment.status_code == "05"
    assert "cas" not in calls


# ------------------------------------------------------------------ рекуррент


def _renewal_payment(period_end: datetime) -> DummyPayment:
    return DummyPayment(
        user_data="1p_42_rabc",
        amount_kopeks=20_000,
        is_recurring=True,
        binding_id=5,
        metadata_json={
            "renewal": {
                "v": 1,
                "subscription_id": 9,
                "plan_id": None,
                "period_days": 30,
                "price_kopeks": 30_000,
                "balance_planned_kopeks": 10_000,
                "shortfall_kopeks": 20_000,
                "period_end": period_end.isoformat(),
                "is_legacy": True,
            }
        },
    )


def _patch_legacy_renewal(monkeypatch: pytest.MonkeyPatch, calls: Dict[str, Any]) -> None:
    async def fake_subtract(db: Any, user: Any, amount: int, *args: Any, **kwargs: Any) -> bool:
        calls["subtracted"] = amount
        user.balance_kopeks -= amount
        return True

    async def fake_extend(db: Any, subscription: Any, days: int) -> Any:
        calls["extended_days"] = days
        subscription.end_date = subscription.end_date + timedelta(days=days)
        return subscription

    async def fake_update_remnawave(self: Any, db: Any, subscription: Any, **kwargs: Any) -> None:
        calls["remnawave"] = True

    async def fake_event(db: Any, **kwargs: Any) -> None:
        calls["event"] = kwargs

    monkeypatch.setattr("app.database.crud.user.subtract_user_balance", fake_subtract)
    monkeypatch.setattr("app.database.crud.subscription.extend_subscription", fake_extend)
    monkeypatch.setattr(
        "app.services.subscription_service.SubscriptionService.update_remnawave_user", fake_update_remnawave
    )
    monkeypatch.setattr("app.database.crud.subscription_event.record_subscription_renewal_event", fake_event)


@pytest.mark.anyio
async def test_recurring_success_renews_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    period_end = datetime(2030, 1, 10, 12, 0, 0)
    payment = _renewal_payment(period_end)
    user = DummyUser(balance=10_000)
    subscription = SimpleNamespace(id=9, user_id=42, status="active", end_date=period_end, plan_id=None)
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)
    _patch_legacy_renewal(monkeypatch, calls)

    db = DummySession()
    db.execute_results.append(SimpleNamespace(scalar_one_or_none=lambda: subscription))

    payload = {"status": "3", "user_data": payment.user_data, "order_id": "r1", "token": "sbp_t_same"}
    assert await service.process_onepayment_webhook(db, payload) is True

    # Деньги зачислены (10 000 + 20 000), затем списана цена продления 30 000.
    assert calls["subtracted"] == 30_000
    assert user.balance_kopeks == 0
    assert calls["extended_days"] == 30
    assert calls["event"]["source"] == "onepayment_recurring"
    assert payment.metadata_json.get("renewal_applied") is True
    binding_updates = calls.get("binding_updates") or []
    assert any(u.get("set_last_charged_period_end") for u in binding_updates)
    assert subscription.end_date == period_end + timedelta(days=30)


@pytest.mark.anyio
async def test_recurring_success_skips_renewal_when_period_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пользователь продлил вручную раньше — деньги остаются на балансе."""
    service = _make_service(StubOnePaymentService())
    period_end = datetime(2030, 1, 10, 12, 0, 0)
    payment = _renewal_payment(period_end)
    user = DummyUser(balance=0)
    subscription = SimpleNamespace(
        id=9, user_id=42, status="active", end_date=period_end + timedelta(days=30), plan_id=None
    )
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)
    _patch_legacy_renewal(monkeypatch, calls)

    db = DummySession()
    db.execute_results.append(SimpleNamespace(scalar_one_or_none=lambda: subscription))

    assert await service.process_onepayment_webhook(db, {"status": "3", "user_data": payment.user_data}) is True
    assert user.balance_kopeks == 20_000
    assert "subtracted" not in calls
    assert payment.metadata_json.get("renewal_applied") is None
    # Само списание удалось, даже если продлевать было нечего.
    binding_updates = calls.get("binding_updates") or []
    assert any(
        u.get("last_charge_status") == "SUCCESS" and u.get("failed_attempts") == 0
        for u in binding_updates
    )
    assert not any(u.get("set_last_charged_period_end") for u in binding_updates)


@pytest.mark.anyio
async def test_recurring_success_renews_expired_subscription_with_same_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Колбек пришёл после перевода подписки в EXPIRED — продление всё равно проводится."""
    service = _make_service(StubOnePaymentService())
    period_end = datetime(2030, 1, 10, 12, 0, 0)
    payment = _renewal_payment(period_end)
    user = DummyUser(balance=10_000)
    subscription = SimpleNamespace(id=9, user_id=42, status="expired", end_date=period_end, plan_id=None)
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)
    _patch_legacy_renewal(monkeypatch, calls)

    db = DummySession()
    db.execute_results.append(SimpleNamespace(scalar_one_or_none=lambda: subscription))

    assert await service.process_onepayment_webhook(db, {"status": "3", "user_data": payment.user_data}) is True
    assert calls["extended_days"] == 30


@pytest.mark.anyio
async def test_recurring_failure_increments_attempts_and_deactivates(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    payment = _renewal_payment(datetime(2030, 1, 10, 12, 0, 0))
    user = DummyUser()
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)

    binding = DummyBinding()
    binding.failed_attempts = 2

    async def fake_get_binding_for_update(db: Any, binding_id: int) -> DummyBinding:
        return binding

    async def fake_update_binding(db: Any, binding_arg: Any, **kwargs: Any) -> Any:
        calls.setdefault("binding_updates", []).append(kwargs)
        for key in ("status", "failed_attempts", "last_charge_status"):
            if kwargs.get(key) is not None:
                setattr(binding_arg, key, kwargs[key])
        return binding_arg

    async def fake_deactivate(db: Any, binding_arg: Any, *, status: str) -> Any:
        calls["deactivated"] = status
        binding_arg.status = status
        binding_arg.active_user_id = None
        return binding_arg

    monkeypatch.setattr(
        payment_service_module, "get_onepayment_binding_by_id_for_update", fake_get_binding_for_update, raising=False
    )
    monkeypatch.setattr(payment_service_module, "update_onepayment_binding", fake_update_binding, raising=False)
    monkeypatch.setattr(payment_service_module, "deactivate_onepayment_binding", fake_deactivate, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_MAX_ATTEMPTS", 3, raising=False)

    assert await service.process_onepayment_webhook(
        DummySession(), {"status": "4", "user_data": payment.user_data, "status_code": "51"}
    ) is True
    assert binding.failed_attempts == 3
    assert calls["deactivated"] == "FAILED"


@pytest.mark.anyio
async def test_charge_binding_creates_recurring_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubOnePaymentService({"order_id": "r-order", "status": 2, "status_description": "PENDING", "raw": {}})
    service = _make_service(stub)
    binding = DummyBinding()
    created: Dict[str, Any] = {}
    updates: List[Dict[str, Any]] = []

    async def fake_create(db: Any, **kwargs: Any) -> DummyPayment:
        created.update(kwargs)
        return DummyPayment(user_data=kwargs["user_data"], is_recurring=True, binding_id=binding.id, metadata_json=kwargs["metadata"])

    async def fake_update(db: Any, payment: Any, **kwargs: Any) -> Any:
        updates.append(kwargs)
        for key in ("status", "provider_order_id"):
            if kwargs.get(key) is not None:
                setattr(payment, key, kwargs[key])
        return payment

    async def fake_update_binding(db: Any, binding_arg: Any, **kwargs: Any) -> Any:
        created["binding_update"] = kwargs
        return binding_arg

    monkeypatch.setattr(payment_service_module, "create_onepayment_payment", fake_create, raising=False)
    monkeypatch.setattr(payment_service_module, "update_onepayment_payment", fake_update, raising=False)
    monkeypatch.setattr(payment_service_module, "update_onepayment_binding", fake_update_binding, raising=False)

    snapshot = {"subscription_id": 9, "period_days": 30, "price_kopeks": 30_000, "period_end": "2030-01-10T12:00:00"}
    payment = await service.charge_onepayment_binding(
        DummySession(), binding, amount_kopeks=20_000, description="Продление", renewal_snapshot=snapshot, language="ru"
    )

    assert payment is not None
    assert created["is_recurring"] is True
    assert created["binding_id"] == binding.id
    assert created["metadata"]["renewal"] == snapshot
    assert created["user_data"].startswith("1p_42_r")
    assert stub.charges[0]["token"] == binding.token
    assert stub.charges[0]["amount_kopeks"] == 20_000
    assert payment.status == "PENDING"
    assert payment.provider_order_id == "r-order"
    assert created["binding_update"]["last_charge_status"] == "PENDING"


@pytest.mark.anyio
async def test_charge_binding_api_error_marks_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.onepayment_service import OnePaymentAPIError

    stub = StubOnePaymentService(error=OnePaymentAPIError("bad", error_code=6, http_status=400))
    service = _make_service(stub)
    binding = DummyBinding()
    updates: List[Dict[str, Any]] = []
    binding_updates: List[Dict[str, Any]] = []

    async def fake_create(db: Any, **kwargs: Any) -> DummyPayment:
        return DummyPayment(user_data=kwargs["user_data"], is_recurring=True, metadata_json=kwargs["metadata"])

    async def fake_update(db: Any, payment: Any, **kwargs: Any) -> Any:
        updates.append(kwargs)
        return payment

    async def fake_update_binding(db: Any, binding_arg: Any, **kwargs: Any) -> Any:
        binding_updates.append(kwargs)
        binding_arg.failed_attempts = kwargs.get("failed_attempts", binding_arg.failed_attempts)
        return binding_arg

    monkeypatch.setattr(payment_service_module, "create_onepayment_payment", fake_create, raising=False)
    monkeypatch.setattr(payment_service_module, "update_onepayment_payment", fake_update, raising=False)
    monkeypatch.setattr(payment_service_module, "update_onepayment_binding", fake_update_binding, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_RECURRING_MAX_ATTEMPTS", 3, raising=False)

    result = await service.charge_onepayment_binding(
        DummySession(), binding, amount_kopeks=20_000, description="x", renewal_snapshot={}
    )
    assert result is None
    assert updates[0]["status"] == "FAILURE"
    assert updates[0]["status_code"] == "api_error_6"
    assert binding.failed_attempts == 1


@pytest.mark.anyio
async def test_cancel_binding_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(StubOnePaymentService())
    binding = DummyBinding()
    calls: Dict[str, Any] = {}

    async def fake_get(db: Any, binding_id: int) -> DummyBinding:
        return binding

    async def fake_deactivate(db: Any, binding_arg: Any, *, status: str) -> Any:
        calls["status"] = status
        binding_arg.status = status
        return binding_arg

    monkeypatch.setattr(payment_service_module, "get_onepayment_binding_by_id_for_update", fake_get, raising=False)
    monkeypatch.setattr(payment_service_module, "deactivate_onepayment_binding", fake_deactivate, raising=False)

    result = await service.cancel_onepayment_binding(DummySession(), binding_id=5, user_id=42)
    assert result and result["already_cancelled"] is False
    assert calls["status"] == "CANCELLED"

    wrong_user = await service.cancel_onepayment_binding(DummySession(), binding_id=5, user_id=1)
    assert wrong_user is None


@pytest.mark.anyio
async def test_status_check_tolerates_transaction_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пока плательщик не открыл форму, транзакции у 1Payment нет —
    кнопка «Проверить оплату» не должна считать это сбоем."""
    from app.services.onepayment_service import (
        ERROR_TRANSACTION_NOT_FOUND,
        OnePaymentAPIError,
    )

    payment = DummyPayment()

    class RaisingService(StubOnePaymentService):
        async def get_payment_status(self, **_: Any) -> Dict[str, Any]:
            raise OnePaymentAPIError("not found", error_code=ERROR_TRANSACTION_NOT_FOUND)

    service = _make_service(RaisingService())

    async def fake_get_by_id(db: Any, payment_id: int) -> DummyPayment:
        return payment

    monkeypatch.setattr(
        payment_service_module, "get_onepayment_payment_by_id", fake_get_by_id, raising=False
    )

    result = await service.get_onepayment_payment_status(DummySession(), payment.id)

    assert result is not None
    assert result["is_paid"] is False
    assert result["status"] == "INIT"
    assert result["remote"] is None


# ---------------------------------------------- A/B «доступ за 1 ₽ вместо триала»


@pytest.mark.anyio
async def test_create_onepayment_payment_allows_below_minimum_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubOnePaymentService({"url": "https://merchant.1payment.com/x", "order_id": "1", "status": 1, "raw": {}})
    service = _make_service(stub)
    monkeypatch.setattr(settings, "ONEPAYMENT_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)

    async def fake_create_local(db: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id=11, **kwargs)

    async def fake_snapshot(user_id: int, amount: int) -> None:
        return None

    monkeypatch.setattr(payment_service_module, "create_onepayment_payment", fake_create_local, raising=False)
    monkeypatch.setattr(onepayment_mixin_module, "build_invoice_checkout_snapshot", fake_snapshot)

    assert await service.create_onepayment_payment(DummySession(), user_id=42, amount_kopeks=100, description="x") is None
    assert not stub.calls

    result = await service.create_onepayment_payment(
        DummySession(), user_id=42, amount_kopeks=100, description="x", allow_below_min=True
    )
    assert result is not None and result["local_payment_id"] == 11
    assert stub.calls[0]["amount_kopeks"] == 100
    assert stub.calls[0]["subscribe"] is True

    # Ноль и отрицательные суммы не проходят даже с allow_below_min.
    assert await service.create_onepayment_payment(
        DummySession(), user_id=42, amount_kopeks=0, description="x", allow_below_min=True
    ) is None
    # Максимум проверяется всегда.
    assert await service.create_onepayment_payment(
        DummySession(), user_id=42, amount_kopeks=6_000_000, description="x", allow_below_min=True
    ) is None


def _paid_trial_metadata() -> Dict[str, Any]:
    return {
        "paid_trial": {
            "v": 1,
            "kind": "paid_trial",
            "user_id": 42,
            "plan_id": 2,
            "access_days": 1,
            "renewal_period_days": 30,
            "price_kopeks": 100,
        }
    }


@pytest.mark.anyio
async def test_webhook_paid_trial_snapshot_activates_and_skips_topup_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment(amount_kopeks=100, metadata_json=_paid_trial_metadata())
    user = DummyUser(balance=0)
    _lookup(monkeypatch, payment)
    calls = _patch_finalize_deps(monkeypatch, payment=payment, user=user)

    activation_calls: List[Dict[str, Any]] = []

    async def fake_activate(db: Any, user_arg: Any, snapshot: Dict[str, Any], *, bot: Any = None) -> bool:
        activation_calls.append({"user": user_arg, "snapshot": snapshot, "balance": user_arg.balance_kopeks})
        return True

    async def fail_auto_purchase(*_: Any, **__: Any) -> bool:
        raise AssertionError("корзина не должна проверяться для оффера")

    notices: List[int] = []

    async def fake_notify(user_arg: Any, amount: int) -> None:
        notices.append(amount)

    monkeypatch.setattr(onepayment_mixin_module.trial_paid_offer_service, "activate_from_payment", fake_activate)
    monkeypatch.setattr(onepayment_mixin_module, "auto_purchase_saved_cart_after_topup", fail_auto_purchase)
    monkeypatch.setattr(service, "_notify_onepayment_topup", fake_notify)

    payload = {"status": "3", "user_data": payment.user_data, "order_id": "o1", "token": "sbp_t_new"}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True

    # Деньги зачислены и токен привязан ДО активации.
    assert activation_calls[0]["balance"] == 100
    assert activation_calls[0]["snapshot"]["kind"] == "paid_trial"
    assert calls["bindings"][0]["token"] == "sbp_t_new"
    assert notices == []


@pytest.mark.anyio
async def test_webhook_paid_trial_activation_failure_keeps_money_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment(amount_kopeks=100, metadata_json=_paid_trial_metadata())
    user = DummyUser(balance=0)
    _lookup(monkeypatch, payment)
    _patch_finalize_deps(monkeypatch, payment=payment, user=user)

    async def fake_activate(*_: Any, **__: Any) -> bool:
        return False

    notices: List[int] = []

    async def fake_notify(user_arg: Any, amount: int) -> None:
        notices.append(amount)

    monkeypatch.setattr(onepayment_mixin_module.trial_paid_offer_service, "activate_from_payment", fake_activate)
    monkeypatch.setattr(service, "_notify_onepayment_topup", fake_notify)

    payload = {"status": "3", "user_data": payment.user_data, "order_id": "o1"}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True
    assert user.balance_kopeks == 100
    assert notices == [100]


@pytest.mark.anyio
async def test_webhook_paid_trial_activation_exception_does_not_break_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(StubOnePaymentService())
    payment = DummyPayment(amount_kopeks=100, metadata_json=_paid_trial_metadata())
    user = DummyUser(balance=0)
    _lookup(monkeypatch, payment)
    _patch_finalize_deps(monkeypatch, payment=payment, user=user)

    async def boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("remnawave down")

    notices: List[int] = []

    async def fake_notify(user_arg: Any, amount: int) -> None:
        notices.append(amount)

    monkeypatch.setattr(onepayment_mixin_module.trial_paid_offer_service, "activate_from_payment", boom)
    monkeypatch.setattr(service, "_notify_onepayment_topup", fake_notify)

    payload = {"status": "3", "user_data": payment.user_data, "order_id": "o1"}
    assert await service.process_onepayment_webhook(DummySession(), payload) is True
    assert notices == [100]
