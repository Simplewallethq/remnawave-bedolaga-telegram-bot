"""Режим чека 54-ФЗ: receipt обязателен только при фискализации через ЮKassa."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_receipt_satisfiable_when_sending_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", None, raising=False)
    assert settings.is_yookassa_receipt_required() is False
    assert settings.is_yookassa_receipt_satisfiable() is True


def test_receipt_not_satisfiable_without_default_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", "  ", raising=False)
    assert settings.is_yookassa_receipt_required() is True
    assert settings.is_yookassa_receipt_satisfiable() is False


def test_receipt_satisfiable_with_default_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", True, raising=False)
    monkeypatch.setattr(
        settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", "r@example.com", raising=False
    )
    assert settings.is_yookassa_receipt_satisfiable() is True


@pytest.mark.anyio
async def test_create_payment_without_receipt_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Раньше без email платёж падал с {'error': True} для каждого юзера."""
    from app.services import yookassa_service as module

    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", None, raising=False)

    captured = {"receipt_set": False}
    real_builder = module.PaymentRequestBuilder

    class SpyBuilder(real_builder):  # type: ignore[misc, valid-type]
        def set_receipt(self, value):  # noqa: ANN001
            captured["receipt_set"] = True
            return super().set_receipt(value)

    monkeypatch.setattr(module, "PaymentRequestBuilder", SpyBuilder)

    service = module.YooKassaService.__new__(module.YooKassaService)
    service.configured = True
    service.return_url = "https://example.com/return"

    result = await module.YooKassaService.create_payment(
        service,
        amount=100.0,
        currency="RUB",
        description="test",
        metadata={"user_id": "1"},
    )

    assert result is not None
    assert not result.get("error")
    assert captured["receipt_set"] is False


@pytest.mark.anyio
async def test_create_payment_sends_receipt_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import yookassa_service as module

    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", True, raising=False)
    monkeypatch.setattr(
        settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", "r@example.com", raising=False
    )

    captured = {"receipt_set": False}
    real_builder = module.PaymentRequestBuilder

    class SpyBuilder(real_builder):  # type: ignore[misc, valid-type]
        def set_receipt(self, value):  # noqa: ANN001
            captured["receipt_set"] = True
            return super().set_receipt(value)

    monkeypatch.setattr(module, "PaymentRequestBuilder", SpyBuilder)

    service = module.YooKassaService.__new__(module.YooKassaService)
    service.configured = True
    service.return_url = "https://example.com/return"

    result = await module.YooKassaService.create_payment(
        service,
        amount=100.0,
        currency="RUB",
        description="test",
        metadata={"user_id": "1"},
    )

    assert result is not None
    assert not result.get("error")
    assert captured["receipt_set"] is True


@pytest.mark.anyio
async def test_create_payment_aborts_when_receipt_required_without_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import yookassa_service as module

    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", None, raising=False)

    service = module.YooKassaService.__new__(module.YooKassaService)
    service.configured = True
    service.return_url = "https://example.com/return"

    result = await module.YooKassaService.create_payment(
        service,
        amount=100.0,
        currency="RUB",
        description="test",
        metadata={"user_id": "1"},
    )

    assert result is not None
    assert result.get("error") is True
