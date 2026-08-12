import logging
from unittest.mock import AsyncMock

import pytest

from app.services.platega_service import PlategaService


def test_sanitize_description_limits_utf8_bytes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    original = "Интернет-сервис - Пополнение баланса на 50 ₽ и ещё чуть-чуть"

    trimmed = PlategaService._sanitize_description(original, 64)

    assert len(trimmed.encode("utf-8")) <= 64
    assert trimmed != original
    assert any("trimmed" in record.message for record in caplog.records)


def test_sanitize_description_returns_clean_value() -> None:
    original = "  Обычное описание  "

    trimmed = PlategaService._sanitize_description(original, 64)

    assert trimmed == "Обычное описание"
    assert len(trimmed.encode("utf-8")) <= 64


@pytest.mark.anyio("asyncio")
async def test_create_subscription_uses_documented_monthly_sbp_payload() -> None:
    service = PlategaService()
    request_mock = AsyncMock(return_value={"transactionId": "sub-1"})
    service._request = request_mock  # type: ignore[method-assign]

    result = await service.create_subscription(
        amount=100,
        currency="RUB",
        description="  Регулярное пополнение  ",
    )

    assert result == {"transactionId": "sub-1"}
    request_mock.assert_awaited_once_with(
        "POST",
        "/transaction/process",
        json_data={
            "paymentMethod": 6,
            "paymentDetails": {"amount": 100, "currency": "RUB", "interval": 3},
            "description": "Регулярное пополнение",
        },
    )


@pytest.mark.anyio("asyncio")
async def test_cancel_subscription_uses_provider_endpoint() -> None:
    service = PlategaService()
    request_mock = AsyncMock(return_value={"status": "cancelled"})
    service._request = request_mock  # type: ignore[method-assign]

    await service.cancel_subscription("sub/1")

    request_mock.assert_awaited_once_with("POST", "/subscription/sub%2F1/cancel")
