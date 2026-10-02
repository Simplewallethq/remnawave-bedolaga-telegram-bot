import logging
from types import SimpleNamespace
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


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (SimpleNamespace(email="ivan@example.com", email_verified=True, username="ivan", telegram_id=1), "ivan@example.com"),
        (SimpleNamespace(email="ivan@example.com", email_verified=False, username="@ivan", telegram_id=1), "ivan@t.me"),
        (SimpleNamespace(email=None, email_verified=False, username=None, telegram_id=463239844), "463239844@t.me"),
        (SimpleNamespace(email="web@example.com", email_verified=False, username=None, telegram_id=None), "web@example.com"),
        (SimpleNamespace(email=None, email_verified=False, username=None, telegram_id=None), None),
        (None, None),
    ],
)
def test_build_user_email_follows_provider_fallbacks(user, expected) -> None:
    assert PlategaService.build_user_email(user) == expected


@pytest.mark.anyio("asyncio")
async def test_create_payment_sends_user_email_in_metadata() -> None:
    service = PlategaService()
    request_mock = AsyncMock(return_value={"transactionId": "trx-1"})
    service._request = request_mock  # type: ignore[method-assign]

    await service.create_payment_universal(
        amount=150, currency="RUB", user_email="ivan@t.me"
    )

    body = request_mock.await_args.kwargs["json_data"]
    assert body["metadata"] == {"user_email": "ivan@t.me"}
