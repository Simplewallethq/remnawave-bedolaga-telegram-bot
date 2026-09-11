"""Карта вместо СБП на счёте 1Payment: ленивый Platega-счёт, пометки для аналитики."""

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

from app.config import settings  # noqa: E402
import app.handlers.balance.auto as auto_handlers  # noqa: E402
import app.handlers.balance.onepayment as onepayment_handlers  # noqa: E402
import app.services.payment_service as payment_module  # noqa: E402
from app.services.payment.onepayment import OnePaymentPaymentMixin  # noqa: E402
from app.services.payment.onepayment_card_alt import (  # noqa: E402
    CARD_ALT_CALLBACK_PREFIX,
    CARD_ALT_METADATA_KEY,
    CARD_ALT_PAID_AT_KEY,
    CARD_ALT_PURPOSE,
    CARD_ALT_SOURCE_KEY,
    parse_card_alt_callback,
)
from app.services.payment.platega import PlategaPaymentMixin  # noqa: E402
from app.services.tariff_partial_payment_service import SNAPSHOT_METADATA_KEY  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def card_alt_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PLATEGA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MERCHANT_ID", "m", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_SECRET", "s", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_CARD_BUTTON_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_CARD_BUTTON_PLATEGA_METHOD", 11, raising=False)


def _onepayment_payment(**overrides: Any) -> SimpleNamespace:
    payment = SimpleNamespace(
        id=501,
        user_id=42,
        amount_kopeks=29_000,
        description="Оплата тарифа Solo",
        status="INIT",
        is_paid=False,
        metadata_json={
            "language": "ru",
            "invoice_message": {"chat_id": 42, "message_id": 777},
            SNAPSHOT_METADATA_KEY: {"kind": "tariff_checkout", "final_price_kopeks": 29_000},
            "payment_router": {"v": 1, "gateway": "onepayment"},
        },
    )
    for key, value in overrides.items():
        setattr(payment, key, value)
    return payment


class FakeService(OnePaymentPaymentMixin):
    """PaymentService без сети: create_platega_payment записывает вызов."""

    def __init__(self, platega_result: Optional[Dict[str, Any]]) -> None:
        self.platega_result = platega_result
        self.platega_calls: List[Dict[str, Any]] = []

    async def create_platega_payment(self, db: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.platega_calls.append(kwargs)
        return self.platega_result


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Подменяет CRUD платёжного модуля: что сохранили и что «лежит в БД»."""
    state: Dict[str, Any] = {"onepayment_updates": [], "platega_payments": {}}

    async def fake_update_onepayment(db: Any, payment: Any, **kwargs: Any) -> Any:
        state["onepayment_updates"].append(kwargs)
        if kwargs.get("metadata") is not None:
            payment.metadata_json = kwargs["metadata"]
        return payment

    async def fake_get_platega(db: Any, payment_id: int) -> Any:
        return state["platega_payments"].get(payment_id)

    monkeypatch.setattr(payment_module, "update_onepayment_payment", fake_update_onepayment)
    monkeypatch.setattr(payment_module, "get_platega_payment_by_id", fake_get_platega)
    return state


@pytest.mark.anyio
async def test_card_alternative_creates_platega_card_payment(stored: Dict[str, Any]) -> None:
    payment = _onepayment_payment()
    service = FakeService(
        {"local_payment_id": 9001, "transaction_id": "tx-1", "redirect_url": "https://pay/1"}
    )

    result = await service.create_onepayment_card_alternative(None, payment)

    assert result == {"platega_payment_id": 9001, "redirect_url": "https://pay/1", "reused": False}

    call = service.platega_calls[0]
    assert call["user_id"] == 42
    assert call["amount_kopeks"] == 29_000
    assert call["payment_method_code"] == 11
    carried = call["metadata"]
    assert carried["purpose"] == CARD_ALT_PURPOSE
    assert carried[CARD_ALT_SOURCE_KEY] == 501
    # Финализация Platega должна удалить исходное сообщение и провести автопокупку.
    assert carried["invoice_message"] == {"chat_id": 42, "message_id": 777}
    assert carried[SNAPSHOT_METADATA_KEY]["final_price_kopeks"] == 29_000

    block = payment.metadata_json[CARD_ALT_METADATA_KEY]
    assert block["platega_payment_id"] == 9001
    assert block["platega_transaction_id"] == "tx-1"
    assert len(stored["onepayment_updates"]) == 1


@pytest.mark.anyio
async def test_card_alternative_reuses_pending_platega_payment(stored: Dict[str, Any]) -> None:
    payment = _onepayment_payment()
    payment.metadata_json[CARD_ALT_METADATA_KEY] = {"platega_payment_id": 9001}
    stored["platega_payments"][9001] = SimpleNamespace(
        id=9001,
        status="PENDING",
        is_paid=False,
        redirect_url="https://pay/old",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    service = FakeService({"local_payment_id": 9002, "redirect_url": "https://pay/new"})

    result = await service.create_onepayment_card_alternative(None, payment)

    assert result == {"platega_payment_id": 9001, "redirect_url": "https://pay/old", "reused": True}
    assert service.platega_calls == []


@pytest.mark.anyio
async def test_card_alternative_replaces_expired_platega_payment(stored: Dict[str, Any]) -> None:
    payment = _onepayment_payment()
    payment.metadata_json[CARD_ALT_METADATA_KEY] = {"platega_payment_id": 9001}
    stored["platega_payments"][9001] = SimpleNamespace(
        id=9001,
        status="PENDING",
        is_paid=False,
        redirect_url="https://pay/old",
        expires_at=datetime.utcnow() - timedelta(seconds=5),
    )
    service = FakeService({"local_payment_id": 9002, "redirect_url": "https://pay/new"})

    result = await service.create_onepayment_card_alternative(None, payment)

    assert result["platega_payment_id"] == 9002
    assert result["reused"] is False
    assert payment.metadata_json[CARD_ALT_METADATA_KEY]["platega_payment_id"] == 9002


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [{"is_paid": True}, {"status": "SUCCESS"}, {"status": "FAILURE"}],
)
async def test_card_alternative_refuses_settled_invoice(
    stored: Dict[str, Any], overrides: Dict[str, Any]
) -> None:
    service = FakeService({"local_payment_id": 9002, "redirect_url": "https://pay/new"})

    assert await service.create_onepayment_card_alternative(None, _onepayment_payment(**overrides)) is None
    assert service.platega_calls == []


@pytest.mark.anyio
async def test_card_alternative_disabled_by_setting(
    stored: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ONEPAYMENT_CARD_BUTTON_ENABLED", False, raising=False)
    service = FakeService({"local_payment_id": 9002, "redirect_url": "https://pay/new"})

    assert await service.create_onepayment_card_alternative(None, _onepayment_payment()) is None


@pytest.mark.anyio
async def test_card_alternative_failed_platega_leaves_invoice_untouched(
    stored: Dict[str, Any],
) -> None:
    payment = _onepayment_payment()
    service = FakeService(None)

    assert await service.create_onepayment_card_alternative(None, payment) is None
    assert CARD_ALT_METADATA_KEY not in payment.metadata_json
    assert stored["onepayment_updates"] == []


# ------------------------------------------------------------ финализация Platega


@pytest.mark.anyio
async def test_settle_marks_source_invoice_and_router(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _onepayment_payment()
    source.metadata_json[CARD_ALT_METADATA_KEY] = {"platega_payment_id": 9001}
    updates: List[Dict[str, Any]] = []
    router_calls: List[Dict[str, Any]] = []

    async def fake_get_onepayment(db: Any, payment_id: int) -> Any:
        return source if payment_id == 501 else None

    async def fake_update_onepayment(db: Any, payment: Any, **kwargs: Any) -> Any:
        updates.append(kwargs)
        payment.metadata_json = kwargs["metadata"]
        return payment

    async def fake_record_payment(db: Any, **kwargs: Any) -> None:
        router_calls.append(kwargs)

    monkeypatch.setattr(payment_module, "get_onepayment_payment_by_id", fake_get_onepayment)
    monkeypatch.setattr(payment_module, "update_onepayment_payment", fake_update_onepayment)

    from app.services.payment_gateway_router import payment_gateway_router

    monkeypatch.setattr(payment_gateway_router, "record_payment", fake_record_payment)

    platega_payment = SimpleNamespace(
        id=9001,
        correlation_id="corr",
        amount_kopeks=29_000,
        metadata_json={"purpose": CARD_ALT_PURPOSE, CARD_ALT_SOURCE_KEY: 501},
    )
    paid_at = datetime(2026, 9, 11, 12, 0, 0)

    await PlategaPaymentMixin()._settle_onepayment_card_alt(
        None, platega_payment, transaction=SimpleNamespace(id=77), paid_at=paid_at
    )

    assert source.metadata_json[CARD_ALT_PAID_AT_KEY] == paid_at.isoformat()
    assert source.metadata_json[CARD_ALT_METADATA_KEY]["paid_at"] == paid_at.isoformat()
    assert "invoice_message" not in source.metadata_json
    assert router_calls == [
        {
            "gateway": "onepayment",
            "local_payment_id": 501,
            "transaction_id": 77,
            "amount_kopeks": 29_000,
            "paid_at": paid_at,
        }
    ]

    # Повторная финализация (ретрай вебхука) ничего не переписывает.
    await PlategaPaymentMixin()._settle_onepayment_card_alt(
        None, platega_payment, transaction=SimpleNamespace(id=77), paid_at=paid_at
    )
    assert len(updates) == 1
    assert len(router_calls) == 1


@pytest.mark.anyio
async def test_settle_ignores_regular_platega_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_get(db: Any, payment_id: int) -> Any:  # pragma: no cover - не должен вызываться
        raise AssertionError("обычный платёж Platega не должен трогать 1Payment")

    monkeypatch.setattr(payment_module, "get_onepayment_payment_by_id", fail_get)

    await PlategaPaymentMixin()._settle_onepayment_card_alt(
        None,
        SimpleNamespace(id=1, correlation_id="c", amount_kopeks=100, metadata_json={"selected_method": "universal"}),
        transaction=None,
        paid_at=None,
    )


# ------------------------------------------------------------------ хендлеры


def _routed(gateway: str = "onepayment", local_payment_id: Optional[int] = 501) -> SimpleNamespace:
    return SimpleNamespace(gateway=gateway, local_payment_id=local_payment_id)


def test_card_button_only_for_onepayment_within_platega_limits() -> None:
    assert auto_handlers._card_alternative_available(_routed(), 29_000) is True
    assert auto_handlers._card_alternative_available(_routed("platega"), 29_000) is False
    assert auto_handlers._card_alternative_available(_routed(local_payment_id=None), 29_000) is False
    assert auto_handlers._card_alternative_available(_routed(), 9_999) is False
    assert auto_handlers._card_alternative_available(_routed(), 5_000_001) is False


def test_card_button_hidden_when_platega_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PLATEGA_SECRET", None, raising=False)
    assert auto_handlers._card_alternative_available(_routed(), 29_000) is False


def test_card_alt_callback_roundtrip() -> None:
    assert parse_card_alt_callback(f"{CARD_ALT_CALLBACK_PREFIX}501") == 501
    assert parse_card_alt_callback("onepay_card_abc") is None
    assert parse_card_alt_callback("check_onepayment_501") is None
    assert parse_card_alt_callback(None) is None


def test_replace_card_alt_button_swaps_callback_for_url() -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="СБП", url="https://sbp")],
            [InlineKeyboardButton(text="Картой", callback_data="onepay_card_501")],
            [InlineKeyboardButton(text="Назад", callback_data="back")],
        ]
    )

    replaced = onepayment_handlers._replace_card_alt_button(
        markup, callback_data="onepay_card_501", text="💳 Оплатить картой", url="https://card"
    )

    assert replaced is not None
    rows = replaced.inline_keyboard
    assert rows[0][0].url == "https://sbp"
    assert rows[1][0].url == "https://card" and rows[1][0].callback_data is None
    assert rows[2][0].callback_data == "back"

    assert (
        onepayment_handlers._replace_card_alt_button(
            markup, callback_data="onepay_card_999", text="x", url="https://card"
        )
        is None
    )
