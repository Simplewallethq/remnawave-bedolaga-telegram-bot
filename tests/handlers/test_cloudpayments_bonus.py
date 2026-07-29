from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.handlers.balance import cloudpayments as cloudpayments_handler
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _DummyMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append({"text": text, **kwargs})


class _DummyCallback:
    def __init__(self) -> None:
        self.data = "topup_amount|cloudpayments|1000"
        self.message = _DummyMessage()
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append({"text": text, **kwargs})


class _DummyState:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def get_data(self) -> dict[str, Any]:
        return self._data


@pytest.mark.anyio("asyncio")
async def test_cloudpayments_bonus_quick_amount_bypasses_minimum_and_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    db_user = SimpleNamespace(
        id=18835,
        telegram_id=4200,
        language="ru",
    )
    campaign_metadata = vpn_deposit_bonus_service.build_payment_metadata(db_user.id)
    state = _DummyState(
        {
            "topup_purpose": vpn_deposit_bonus_service.PURPOSE,
            "vpn_deposit_bonus_metadata": campaign_metadata,
        }
    )

    class _PaymentServiceStub:
        async def create_cloudpayments_payment(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"payment_url": "https://pay.example/invoice"}

    monkeypatch.setattr(
        type(settings),
        "is_cloudpayments_enabled",
        lambda self: True,
        raising=False,
    )
    monkeypatch.setattr(settings, "CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS", 5000, raising=False)
    monkeypatch.setattr(settings, "CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS", 100000, raising=False)
    monkeypatch.setattr(
        cloudpayments_handler,
        "PaymentService",
        lambda: _PaymentServiceStub(),
    )

    callback = _DummyCallback()

    await cloudpayments_handler.handle_cloudpayments_quick_amount(
        callback,
        db_user,
        db=object(),
        state=state,
    )

    assert calls
    assert calls[0]["amount_kopeks"] == vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS
    assert calls[0]["metadata"]["purpose"] == vpn_deposit_bonus_service.PURPOSE
    assert calls[0]["metadata"]["campaign_key"] == campaign_metadata["campaign_key"]
    assert callback.message.edits
    assert not any(answer.get("show_alert") for answer in callback.answers)
