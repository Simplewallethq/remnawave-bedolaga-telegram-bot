"""Тесты Tribute-платежей PaymentService."""

from pathlib import Path
from types import SimpleNamespace
import sys
import hmac
import hashlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.payment_service import PaymentService  # noqa: E402
from app.config import settings  # noqa: E402
import app.services.tribute_service as tribute_module  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.yookassa_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.cryptobot_service = None
    service.stars_service = None
    service.heleket_service = None
    return service


@pytest.mark.anyio("asyncio")
async def test_create_tribute_payment_requires_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    monkeypatch.setattr(settings, "TRIBUTE_ENABLED", False, raising=False)

    with pytest.raises(ValueError):
        await service.create_tribute_payment(1000, 1, "Пополнение")


@pytest.mark.anyio("asyncio")
async def test_create_tribute_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    monkeypatch.setattr(settings, "TRIBUTE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "WEBHOOK_URL", "https://example.com", raising=False)

    result = await service.create_tribute_payment(
        amount_kopeks=15000,
        user_id=5,
        description="Оплата подписки",
    )

    assert "https://tribute.ru/pay" in result
    assert "amount=15000" in result
    assert "user=5" in result


def test_verify_tribute_webhook_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    monkeypatch.setattr(settings, "TRIBUTE_API_KEY", "secret", raising=False)

    payload = {"payment": "ok"}
    signature = hmac.new(
        b"secret",
        str(payload).encode(),
        hashlib.sha256,
    ).hexdigest()

    assert service.verify_tribute_webhook(payload, signature) is True
    assert service.verify_tribute_webhook(payload, "invalid") is False


def test_verify_tribute_webhook_returns_false_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    monkeypatch.setattr(settings, "TRIBUTE_API_KEY", "", raising=False)

    assert service.verify_tribute_webhook({}, "signature") is False


@pytest.mark.anyio("asyncio")
async def test_tribute_success_auto_purchase_does_not_send_generic_topup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyBot:
        def __init__(self) -> None:
            self.sent_messages: list[dict[str, Any]] = []

        async def send_message(self, *args: Any, **kwargs: Any) -> None:
            self.sent_messages.append({"args": args, "kwargs": kwargs})

    class DummySession:
        pass

    class DummyDbIterator:
        def __init__(self, value: DummySession) -> None:
            self.value = value
            self.used = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> DummySession:
            if self.used:
                raise StopAsyncIteration
            self.used = True
            return self.value

    bot = DummyBot()
    service = tribute_module.TributeService.__new__(tribute_module.TributeService)
    service.bot = bot
    user = SimpleNamespace(id=77, telegram_id=7700, language="ru")
    session = DummySession()

    monkeypatch.setattr(tribute_module, "get_db", lambda: DummyDbIterator(session))
    monkeypatch.setattr(
        tribute_module,
        "get_user_by_telegram_id",
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(
        "app.services.user_cart_service.user_cart_service.has_user_cart",
        AsyncMock(return_value=True),
    )
    auto_purchase_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tribute_module,
        "auto_purchase_saved_cart_after_topup",
        auto_purchase_mock,
    )

    await service._send_success_notification(user.telegram_id, 5000)

    auto_purchase_mock.assert_awaited_once_with(session, user, bot=bot)
    assert bot.sent_messages == []
