"""Тесты POST /api/iap/apple/verify: роут вызывается напрямую, зависимости мокаются.

Реальной БД в тестах нет (см. tests/conftest.py), поэтому по конвенции репозитория
(см. tests/services/test_payment_service_yookassa.py) все CRUD/сервисные вызовы
подменяются через monkeypatch, а не поднимается настоящий ASGI-клиент.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.webapi.routes.iap as iap_module
from app.services.apple_iap_service import (
    AppleIapConfigurationError,
    AppleIapDuplicateTransactionError,
    AppleIapExpiredTransactionError,
    AppleIapUnknownProductError,
    AppleIapVerificationFailedError,
    AppleIapVerifiedTransaction,
)
from app.webapi.schemas.iap import AppleIapVerifyRequest


class DummySession:
    """Заглушка AsyncSession — сама логика БД не тестируется на этом уровне."""


def _verified(**overrides) -> AppleIapVerifiedTransaction:
    fields = dict(
        transaction_id="txn_1",
        original_transaction_id="orig_1",
        product_id="com.letovpn.ios.subscription.monthly",
        expires_date=datetime(2026, 9, 1, 12, 0, 0),
        purchase_date=datetime(2026, 8, 1, 12, 0, 0),
        environment="Sandbox",
    )
    fields.update(overrides)
    return AppleIapVerifiedTransaction(**fields)


class _FakeSubscriptionService:
    is_configured = True
    sync_calls = 0
    should_raise = False

    async def update_remnawave_user(self, db, subscription):
        type(self).sync_calls += 1
        if type(self).should_raise:
            raise RuntimeError("remnawave sync failed")


def _reset_fake_subscription_service(monkeypatch: pytest.MonkeyPatch, *, should_raise=False) -> None:
    _FakeSubscriptionService.sync_calls = 0
    _FakeSubscriptionService.should_raise = should_raise
    monkeypatch.setattr(iap_module, "SubscriptionService", _FakeSubscriptionService)


def _payload(**overrides) -> AppleIapVerifyRequest:
    fields = dict(user_id=1, transaction_jws="fake.jws.token", product_id=None)
    fields.update(overrides)
    return AppleIapVerifyRequest(**fields)


async def test_unknown_user_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=None))
    verify_transaction = AsyncMock()
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", verify_transaction)

    with pytest.raises(HTTPException) as exc_info:
        await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert exc_info.value.status_code == 404
    verify_transaction.assert_not_called()


async def test_invalid_signature_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(
        iap_module.apple_iap_verification_service,
        "verify_transaction",
        lambda jws: (_ for _ in ()).throw(AppleIapVerificationFailedError("bad signature")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert exc_info.value.status_code == 422


async def test_configuration_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(
        iap_module.apple_iap_verification_service,
        "verify_transaction",
        lambda jws: (_ for _ in ()).throw(AppleIapConfigurationError("certs missing")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert exc_info.value.status_code == 500


async def test_idempotent_replay_returns_existing_end_date(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()
    existing_row = SimpleNamespace(
        user_id=1, expires_date=datetime(2026, 9, 1, 12, 0, 0), original_transaction_id="orig_1"
    )

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=existing_row)
    )
    activate = AsyncMock()
    monkeypatch.setattr(iap_module, "activate_apple_iap_transaction", activate)

    response = await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert response.status == "ok"
    assert response.subscription_end_date == existing_row.expires_date
    activate.assert_not_called()


async def test_foreign_account_binding_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()
    binding_row = SimpleNamespace(user_id=999, original_transaction_id="orig_1")

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=binding_row)
    )
    activate = AsyncMock()
    monkeypatch.setattr(iap_module, "activate_apple_iap_transaction", activate)

    with pytest.raises(HTTPException) as exc_info:
        await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert exc_info.value.status_code == 409
    activate.assert_not_called()


async def test_renewal_for_same_account_activates(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified(transaction_id="txn_2")
    binding_row = SimpleNamespace(user_id=1, original_transaction_id="orig_1")
    subscription = SimpleNamespace(id=5, end_date=verified.expires_date)

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=binding_row)
    )
    monkeypatch.setattr(iap_module, "activate_apple_iap_transaction", AsyncMock(return_value=subscription))
    _reset_fake_subscription_service(monkeypatch)

    response = await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert response.status == "ok"
    assert response.subscription_end_date == verified.expires_date


async def test_unknown_product_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        iap_module,
        "activate_apple_iap_transaction",
        AsyncMock(side_effect=AppleIapUnknownProductError("unknown product")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert exc_info.value.status_code == 422


async def test_expired_transaction_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        iap_module,
        "activate_apple_iap_transaction",
        AsyncMock(side_effect=AppleIapExpiredTransactionError("expired")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert exc_info.value.status_code == 422


async def test_concurrent_duplicate_returns_idempotent_200(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()
    winner_row = SimpleNamespace(user_id=1, expires_date=verified.expires_date, original_transaction_id="orig_1")

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    get_by_transaction_id = AsyncMock(side_effect=[None, winner_row])
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", get_by_transaction_id)
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        iap_module,
        "activate_apple_iap_transaction",
        AsyncMock(side_effect=AppleIapDuplicateTransactionError("raced")),
    )

    response = await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert response.status == "ok"
    assert response.subscription_end_date == verified.expires_date
    assert get_by_transaction_id.await_count == 2


async def test_success_end_date_matches_expires_date_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()
    subscription = SimpleNamespace(id=5, end_date=verified.expires_date)

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=None)
    )
    activate = AsyncMock(return_value=subscription)
    monkeypatch.setattr(iap_module, "activate_apple_iap_transaction", activate)
    _reset_fake_subscription_service(monkeypatch)

    response = await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert response.status == "ok"
    assert response.subscription_end_date == verified.expires_date
    activate.assert_awaited_once()
    assert _FakeSubscriptionService.sync_calls == 1


async def test_remnawave_sync_failure_still_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1, subscription=None)
    verified = _verified()
    subscription = SimpleNamespace(id=5, end_date=verified.expires_date)

    monkeypatch.setattr(iap_module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(iap_module.apple_iap_verification_service, "verify_transaction", lambda jws: verified)
    monkeypatch.setattr(iap_module.apple_iap_crud, "get_by_transaction_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        iap_module.apple_iap_crud, "get_binding_by_original_transaction_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(iap_module, "activate_apple_iap_transaction", AsyncMock(return_value=subscription))
    _reset_fake_subscription_service(monkeypatch, should_raise=True)

    response = await iap_module.verify_apple_iap(_payload(), db=DummySession())

    assert response.status == "ok"
    assert response.subscription_end_date == verified.expires_date
    assert _FakeSubscriptionService.sync_calls == 1
