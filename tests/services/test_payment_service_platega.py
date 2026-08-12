"""Тесты для сценариев Platega в PaymentService."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment_service as payment_service_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.payment_service import PaymentService  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class DummySession:
    async def commit(self) -> None:  # pragma: no cover - no custom logic required
        return None

    async def refresh(self, *_: Any) -> None:  # pragma: no cover - no custom logic required
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 101) -> None:
        self.id = payment_id
        self.created_at = datetime.utcnow()


class StubPlategaService:
    def __init__(
        self,
        *,
        configured: bool = True,
        response: Optional[Dict[str, Any]] = None,
        transaction_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.is_configured = configured
        self.response = response or {
            "transactionId": "trx-001",
            "redirect": "https://platega.example/pay",
            "status": "PENDING",
            "expiresIn": 900,
        }
        self.transaction_payload = transaction_payload
        self.calls: list[Dict[str, Any]] = []
        self.raise_error: Optional[Exception] = None

    async def create_payment(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.calls.append(kwargs)
        if self.raise_error:
            raise self.raise_error
        return self.response

    async def get_transaction(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        self.calls.append({"transaction_lookup": transaction_id})
        return self.transaction_payload

    async def create_subscription(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.calls.append({"subscription": kwargs})
        if self.raise_error:
            raise self.raise_error
        return self.response

    async def cancel_subscription(self, subscription_id: str) -> Optional[Dict[str, Any]]:
        self.calls.append({"cancel_subscription": subscription_id})
        if self.raise_error:
            raise self.raise_error
        return {"subscriptionId": subscription_id, "status": "cancelled"}


def _make_service(stub: Optional[StubPlategaService]) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.platega_service = stub
    service.yookassa_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.stars_service = None
    service.wata_service = None
    return service


@pytest.mark.anyio("asyncio")
async def test_create_platega_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPlategaService()
    service = _make_service(stub)
    db = DummySession()

    captured_args: Dict[str, Any] = {}

    async def fake_create_platega_payment(*args: Any, **kwargs: Any) -> DummyLocalPayment:
        if args:
            captured_args["db_arg"] = args[0]
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=777)

    monkeypatch.setattr(
        payment_service_module,
        "create_platega_payment",
        fake_create_platega_payment,
        raising=False,
    )
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 500_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_CURRENCY", "RUB", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_RETURN_URL", "https://return", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_FAILED_URL", "https://failed", raising=False)

    result = await service.create_platega_payment(
        db=db,
        user_id=42,
        amount_kopeks=50_000,
        description="Пополнение счёта",
        language="ru",
        payment_method_code=10,
    )

    assert result is not None
    assert result["local_payment_id"] == 777
    assert result["transaction_id"] == "trx-001"
    assert result["redirect_url"] == "https://platega.example/pay"
    assert result["status"] == "PENDING"
    assert "correlation_id" in result and len(result["correlation_id"]) == 32
    assert captured_args["user_id"] == 42
    assert captured_args["amount_kopeks"] == 50_000
    assert captured_args["payment_method_code"] == 10
    assert captured_args["metadata"]["selected_method"] == 10
    assert stub.calls and stub.calls[0]["payment_method"] == 10
    assert stub.calls[0]["amount"] == pytest.approx(500.0)
    assert stub.calls[0]["currency"] == "RUB"
    assert captured_args["metadata"]["language"] == "ru"


@pytest.mark.anyio("asyncio")
async def test_create_platega_payment_respects_limits_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPlategaService()
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 20_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 40_000, raising=False)

    too_low = await service.create_platega_payment(
        db=db,
        user_id=1,
        amount_kopeks=10_000,
        description="Пополнение",
        language="ru",
        payment_method_code=2,
    )
    assert too_low is None

    too_high = await service.create_platega_payment(
        db=db,
        user_id=1,
        amount_kopeks=100_000,
        description="Пополнение",
        language="ru",
        payment_method_code=2,
    )
    assert too_high is None

    not_configured_service = _make_service(StubPlategaService(configured=False))
    result = await not_configured_service.create_platega_payment(
        db=db,
        user_id=1,
        amount_kopeks=30_000,
        description="Пополнение",
        language="ru",
        payment_method_code=2,
    )
    assert result is None


@pytest.mark.anyio("asyncio")
async def test_create_platega_payment_handles_service_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPlategaService()
    stub.raise_error = RuntimeError("network down")
    service = _make_service(stub)
    db = DummySession()

    async def fake_create_platega_payment(*_: Any, **__: Any) -> DummyLocalPayment:
        pytest.fail("local payment must not be created when Platega call fails")

    monkeypatch.setattr(
        payment_service_module,
        "create_platega_payment",
        fake_create_platega_payment,
        raising=False,
    )
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 1_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 1_000_000, raising=False)

    result = await service.create_platega_payment(
        db=db,
        user_id=5,
        amount_kopeks=25_000,
        description="Пополнение",
        language="ru",
        payment_method_code=13,
    )
    assert result is None
    assert stub.calls and "payment_method" in stub.calls[0]


def test_get_platega_active_methods_parses_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "PLATEGA_ACTIVE_METHODS",
        " 2,10, 11 ;12,13,13,invalid ",
        raising=False,
    )

    methods = settings.get_platega_active_methods()

    assert methods == [2, 10, 11, 12, 13]


def test_get_platega_active_methods_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PLATEGA_ACTIVE_METHODS", "", raising=False)

    methods = settings.get_platega_active_methods()

    assert methods == [2]


def test_platega_method_display_helpers() -> None:
    assert settings.get_platega_method_display_name(6) == "Регулярный платёж по СБП"
    assert settings.get_platega_method_display_title(6) == "🕑 СБП — регулярно"
    assert settings.get_platega_method_display_name(10) == "Банковские карты (RUB)"
    assert settings.get_platega_method_display_title(10) == "💳 Карты (RUB)"
    assert settings.get_platega_method_display_name(999) == "Метод 999"
    assert settings.get_platega_method_display_title(999) == "Platega 999"


@pytest.mark.anyio("asyncio")
async def test_create_platega_subscription_reserves_active_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubPlategaService(
        response={
            "transactionId": "subscription-001",
            "redirect": "https://platega.example/subscription-001",
            "status": "PENDING",
        }
    )
    service = _make_service(stub)
    db = DummySession()
    subscription = SimpleNamespace(id=71, user_id=42, status="PENDING")
    captured_create: Dict[str, Any] = {}

    async def fake_get_active(*_: Any, **__: Any) -> None:
        return None

    async def fake_create(*_: Any, **kwargs: Any) -> Any:
        captured_create.update(kwargs)
        return subscription

    async def fake_update(*_: Any, **kwargs: Any) -> Any:
        for key in ("status", "platega_subscription_id", "redirect_url"):
            if key in kwargs and kwargs[key] is not None:
                setattr(subscription, key, kwargs[key])
        return subscription

    monkeypatch.setattr(
        payment_service_module,
        "get_active_platega_subscription_for_user",
        fake_get_active,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "create_platega_subscription",
        fake_create,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        fake_update,
        raising=False,
    )
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 500_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_CURRENCY", "RUB", raising=False)

    result = await service.create_platega_subscription(
        db,
        user_id=42,
        amount_kopeks=10_000,
        description="Регулярное пополнение",
    )

    assert result is not None
    assert result["subscription_id"] == "subscription-001"
    assert captured_create["active_user_id"] == 42
    assert captured_create["platega_subscription_id"].startswith("pending:")
    assert stub.calls[0]["subscription"] == {
        "amount": 100,
        "currency": "RUB",
        "description": "Регулярное пополнение",
    }


@pytest.mark.anyio("asyncio")
async def test_create_platega_subscription_returns_existing_active_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubPlategaService()
    service = _make_service(stub)
    existing = SimpleNamespace(id=12, user_id=42, status="SUBSCRIPTION_ACTIVATED")

    async def fake_get_active(*_: Any, **__: Any) -> Any:
        return existing

    monkeypatch.setattr(
        payment_service_module,
        "get_active_platega_subscription_for_user",
        fake_get_active,
        raising=False,
    )

    result = await service.create_platega_subscription(
        DummySession(),
        user_id=42,
        amount_kopeks=10_000,
        description="Регулярное пополнение",
    )

    assert result == {"subscription": existing, "already_exists": True}
    assert not stub.calls


@pytest.mark.anyio("asyncio")
async def test_subscription_status_callback_updates_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    subscription = SimpleNamespace(id=1, user_id=42, status="PENDING")
    update_mock = AsyncMock(return_value=subscription)

    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_provider_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_mock,
        raising=False,
    )

    result = await service.process_platega_webhook(
        DummySession(),
        {
            "Id": "subscription-001",
            "SubscriptionId": "subscription-001",
            "Status": "SUBSCRIPTION_ACTIVATED",
            "NextChargeAt": "2026-08-09T09:10:00Z",
        },
    )

    assert result is True
    assert update_mock.await_args.kwargs["status"] == "SUBSCRIPTION_ACTIVATED"
    assert update_mock.await_args.kwargs["active_user_id"] == 42
    assert update_mock.await_args.kwargs["next_charge_at"] == datetime(2026, 8, 9, 9, 10)


@pytest.mark.anyio("asyncio")
async def test_failed_subscription_status_callback_releases_active_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    subscription = SimpleNamespace(id=1, user_id=42, status="PENDING")
    update_mock = AsyncMock(return_value=subscription)

    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_provider_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_mock,
        raising=False,
    )

    result = await service.process_platega_webhook(
        DummySession(),
        {
            "Id": "subscription-001",
            "SubscriptionId": "subscription-001",
            "Status": "SUBSCRIPTION_FAILED",
            "NextChargeAt": None,
        },
    )

    assert result is True
    assert update_mock.await_args.kwargs["status"] == "SUBSCRIPTION_FAILED"
    assert update_mock.await_args.kwargs["active_user_id"] is None


@pytest.mark.anyio("asyncio")
async def test_cancelled_subscription_ignores_late_activation_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    subscription = SimpleNamespace(
        id=1, user_id=42, status="SUBSCRIPTION_CANCELLED"
    )
    update_mock = AsyncMock()

    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_provider_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_mock,
        raising=False,
    )

    result = await service.process_platega_webhook(
        DummySession(),
        {
            "Id": "subscription-001",
            "SubscriptionId": "subscription-001",
            "Status": "SUBSCRIPTION_ACTIVATED",
            "NextChargeAt": "2026-08-09T09:10:00Z",
        },
    )

    assert result is True
    update_mock.assert_not_awaited()


@pytest.mark.anyio("asyncio")
async def test_cancelled_subscription_charge_does_not_finalize_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    subscription = SimpleNamespace(id=1, user_id=42, description="Регулярное пополнение")
    payment = SimpleNamespace(id=2, is_paid=False)
    update_subscription_mock = AsyncMock(return_value=subscription)
    update_payment_mock = AsyncMock(return_value=payment)
    finalize_mock = AsyncMock()

    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_provider_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "get_or_create_platega_subscription_charge",
        AsyncMock(return_value=payment),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "get_platega_payment_by_id_for_update",
        AsyncMock(return_value=payment),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_payment",
        update_payment_mock,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_subscription_mock,
        raising=False,
    )
    monkeypatch.setattr(service, "_finalize_platega_payment", finalize_mock)

    result = await service.process_platega_webhook(
        DummySession(),
        {
            "Id": "charge-001",
            "SubscriptionId": "subscription-001",
            "Amount": 100,
            "Currency": "RUB",
            "Status": "CANCELED",
            "NextChargeAt": None,
        },
    )

    assert result is True
    update_payment_mock.assert_awaited_once()
    assert update_subscription_mock.await_args.kwargs["status"] == "SUBSCRIPTION_PAST_DUE"
    assert update_subscription_mock.await_args.kwargs["active_user_id"] is None
    finalize_mock.assert_not_awaited()


@pytest.mark.anyio("asyncio")
async def test_confirmed_subscription_charge_finalizes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    subscription = SimpleNamespace(id=1, user_id=42, description="Регулярное пополнение")
    payment = SimpleNamespace(id=2, is_paid=False)
    finalize_mock = AsyncMock()

    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_provider_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    get_or_create_mock = AsyncMock(return_value=payment)
    monkeypatch.setattr(
        payment_service_module,
        "get_or_create_platega_subscription_charge",
        get_or_create_mock,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "get_platega_payment_by_id_for_update",
        AsyncMock(return_value=payment),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_payment",
        AsyncMock(return_value=payment),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(service, "_finalize_platega_payment", finalize_mock)

    payload = {
        "Id": "charge-001",
        "SubscriptionId": "subscription-001",
        "Amount": 100,
        "Currency": "RUB",
        "Status": "CONFIRMED",
        "NextChargeAt": "2026-09-09T09:10:00Z",
    }
    db = DummySession()
    assert await service.process_platega_webhook(db, payload) is True
    assert get_or_create_mock.await_args.kwargs["amount_kopeks"] == 10_000
    finalize_mock.assert_awaited_once_with(db, payment, payload)

    payment.is_paid = True
    assert await service.process_platega_webhook(db, payload) is True
    assert finalize_mock.await_count == 1


@pytest.mark.anyio("asyncio")
async def test_cancel_platega_subscription_releases_active_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubPlategaService()
    service = _make_service(stub)
    subscription = SimpleNamespace(
        id=1,
        user_id=42,
        status="SUBSCRIPTION_ACTIVATED",
        platega_subscription_id="subscription-001",
    )
    update_mock = AsyncMock(return_value=subscription)

    monkeypatch.setattr(
        payment_service_module,
        "get_platega_subscription_by_id_for_update",
        AsyncMock(return_value=subscription),
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_platega_subscription",
        update_mock,
        raising=False,
    )

    result = await service.cancel_platega_subscription(
        DummySession(), subscription_id=1, user_id=42
    )

    assert result == {"subscription": subscription, "already_cancelled": False}
    assert stub.calls == [{"cancel_subscription": "subscription-001"}]
    assert update_mock.await_args.kwargs["status"] == "SUBSCRIPTION_CANCELLED"
    assert update_mock.await_args.kwargs["active_user_id"] is None
