"""Тесты сервиса верификации Apple IAP: мок SignedDataVerifier, без реальной криптографии."""

from datetime import datetime
from types import SimpleNamespace

import jwt
import pytest
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus

import app.services.apple_iap_service as apple_iap_service_module
from app.services.apple_iap_service import (
    AppleIapConfigurationError,
    AppleIapVerificationFailedError,
    AppleIapVerificationService,
)


def _unsigned_jws(environment: str) -> str:
    """JWT с валидной структурой, но без реальной подписи Apple — SignedDataVerifier мокается,
    поэтому проверяется только чтение claim'а environment для выбора верифаера."""
    return jwt.encode({"environment": environment}, "test-secret", algorithm="HS256")


def _decoded_payload(environment: Environment) -> SimpleNamespace:
    return SimpleNamespace(
        transactionId="txn_1",
        originalTransactionId="orig_1",
        productId="com.letovpn.ios.subscription.monthly",
        expiresDate=1_735_689_600_000,
        purchaseDate=1_735_686_000_000,
        environment=environment,
    )


class _FakeVerifier:
    def __init__(self, decoded=None, error=None):
        self._decoded = decoded
        self._error = error
        self.calls = 0

    def verify_and_decode_signed_transaction(self, transaction_jws: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._decoded


def test_verify_transaction_valid_sandbox_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AppleIapVerificationService()
    fake = _FakeVerifier(decoded=_decoded_payload(Environment.SANDBOX))
    monkeypatch.setattr(service, "_get_sandbox_verifier", lambda: fake)

    result = service.verify_transaction(_unsigned_jws("Sandbox"))

    assert result.transaction_id == "txn_1"
    assert result.original_transaction_id == "orig_1"
    assert result.product_id == "com.letovpn.ios.subscription.monthly"
    assert result.environment == "Sandbox"
    assert result.expires_date == datetime.utcfromtimestamp(1_735_689_600_000 / 1000)
    assert result.purchase_date == datetime.utcfromtimestamp(1_735_686_000_000 / 1000)
    assert fake.calls == 1


def test_verify_transaction_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AppleIapVerificationService()
    fake = _FakeVerifier(error=VerificationException(VerificationStatus.VERIFICATION_FAILURE))
    monkeypatch.setattr(service, "_get_sandbox_verifier", lambda: fake)

    with pytest.raises(AppleIapVerificationFailedError):
        service.verify_transaction(_unsigned_jws("Sandbox"))


def test_verify_transaction_foreign_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AppleIapVerificationService()
    fake = _FakeVerifier(error=VerificationException(VerificationStatus.INVALID_APP_IDENTIFIER))
    monkeypatch.setattr(service, "_get_production_verifier", lambda: fake)

    with pytest.raises(AppleIapVerificationFailedError):
        service.verify_transaction(_unsigned_jws("Production"))


def test_verify_transaction_selects_verifier_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AppleIapVerificationService()
    sandbox_fake = _FakeVerifier(decoded=_decoded_payload(Environment.SANDBOX))
    production_fake = _FakeVerifier(decoded=_decoded_payload(Environment.PRODUCTION))
    monkeypatch.setattr(service, "_get_sandbox_verifier", lambda: sandbox_fake)
    monkeypatch.setattr(service, "_get_production_verifier", lambda: production_fake)

    service.verify_transaction(_unsigned_jws("Sandbox"))
    assert sandbox_fake.calls == 1
    assert production_fake.calls == 0

    service.verify_transaction(_unsigned_jws("Production"))
    assert sandbox_fake.calls == 1
    assert production_fake.calls == 1


def test_production_verifier_requires_app_apple_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apple_iap_service_module.settings, "APPLE_IAP_APP_APPLE_ID", None)
    service = AppleIapVerificationService()
    monkeypatch.setattr(service, "_get_root_certificates", lambda: [b"fake-cert"])

    with pytest.raises(AppleIapConfigurationError):
        service.verify_transaction(_unsigned_jws("Production"))
