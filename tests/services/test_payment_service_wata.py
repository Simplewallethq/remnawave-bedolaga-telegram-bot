"""Tests for WATA payment mixin."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Dict, Optional

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment_service as payment_service_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.payment_service import PaymentService  # noqa: E402
from app.services.wata_service import WataService  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class DummySession:
    async def commit(self) -> None:  # pragma: no cover - no logic required
        return None

    async def refresh(self, *_: Any) -> None:  # pragma: no cover - no logic required
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 42) -> None:
        self.id = payment_id
        self.created_at = datetime.utcnow()


class StubWataService:
    def __init__(self, response: Optional[Dict[str, Any]]) -> None:
        self.response = response
        self.calls: list[Dict[str, Any]] = []

    async def create_payment_link(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.calls.append(kwargs)
        return self.response


class DummyWataPayment:
    def __init__(self) -> None:
        self.id = 1
        self.user_id = 42
        self.payment_link_id = "link-123"
        self.order_id = "order-123"
        self.amount_kopeks = 15_000
        self.currency = "RUB"
        self.description = "Пополнение"
        self.status = "Opened"
        self.is_paid = False
        self.metadata_json: Dict[str, Any] = {}
        self.transaction_id: Optional[int] = None
        self.callback_payload: Optional[Dict[str, Any]] = None
        self.terminal_public_id: Optional[str] = None


def _make_service(stub: Optional[StubWataService]) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.wata_service = stub
    service.mulenpay_service = None
    service.pal24_service = None
    service.yookassa_service = None
    service.stars_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    return service


def test_wata_service_format_datetime_accepts_naive_utc() -> None:
    value = datetime(2024, 5, 20, 12, 30, 0)
    formatted = WataService._format_datetime(value)
    assert formatted == "2024-05-20T12:30:00Z"


def test_wata_service_parse_datetime_returns_naive_utc() -> None:
    parsed = WataService._parse_datetime("2024-05-20T12:30:00Z")
    assert parsed == datetime(2024, 5, 20, 12, 30, 0)
    assert parsed.tzinfo is None

    parsed_with_offset = WataService._parse_datetime("2024-05-20T15:30:00+03:00")
    assert parsed_with_offset == datetime(2024, 5, 20, 12, 30, 0)
    assert parsed_with_offset.tzinfo is None


@pytest.mark.anyio("asyncio")
async def test_create_wata_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "url": "https://wata.example/link",
        "status": "Opened",
        "type": "OneTime",
        "terminalPublicId": "terminal-id",
        "successRedirectUrl": "https://example.com/success",
        "failRedirectUrl": "https://example.com/fail",
        "expirationDateTime": "2030-01-01T00:00:00Z",
    }
    stub = StubWataService(response)
    service = _make_service(stub)
    db = DummySession()

    captured_args: Dict[str, Any] = {}

    async def fake_create_wata_payment(**kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=777)

    monkeypatch.setattr(payment_service_module, "create_wata_payment", fake_create_wata_payment, raising=False)
    monkeypatch.setattr(settings, "WATA_MIN_AMOUNT_KOPEKS", 5000, raising=False)
    monkeypatch.setattr(settings, "WATA_MAX_AMOUNT_KOPEKS", 500_000, raising=False)

    result = await service.create_wata_payment(
        db=db,
        user_id=101,
        amount_kopeks=15000,
        description="Пополнение",
        language="ru",
    )

    assert result is not None
    assert result["local_payment_id"] == 777
    assert result["payment_link_id"] == response["id"]
    assert result["payment_url"] == response["url"]
    assert captured_args["user_id"] == 101
    assert captured_args["amount_kopeks"] == 15000
    assert captured_args["payment_link_id"] == response["id"]
    assert stub.calls and stub.calls[0]["amount_kopeks"] == 15000


@pytest.mark.anyio("asyncio")
async def test_create_wata_payment_respects_amount_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubWataService({"id": "link"})
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, "WATA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "WATA_MAX_AMOUNT_KOPEKS", 20_000, raising=False)

    too_low = await service.create_wata_payment(
        db=db,
        user_id=1,
        amount_kopeks=5_000,
        description="Пополнение",
    )
    assert too_low is None

    too_high = await service.create_wata_payment(
        db=db,
        user_id=1,
        amount_kopeks=25_000,
        description="Пополнение",
    )
    assert too_high is None
    assert not stub.calls


@pytest.mark.anyio("asyncio")
async def test_create_wata_payment_returns_none_without_service() -> None:
    service = _make_service(None)
    db = DummySession()

    result = await service.create_wata_payment(
        db=db,
        user_id=5,
        amount_kopeks=10_000,
        description="Пополнение",
    )
    assert result is None


@pytest.mark.anyio("asyncio")
async def test_process_wata_webhook_updates_status(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(None)
    db = DummySession()
    payment = DummyWataPayment()
    update_kwargs: Dict[str, Any] = {}
    link_lookup_called = False

    async def fake_get_by_order_id(db_arg: Any, order_id: str) -> DummyWataPayment:
        assert db_arg is db
        assert order_id == payment.order_id
        return payment

    async def fake_get_by_link_id(*_: Any, **__: Any) -> Optional[DummyWataPayment]:
        nonlocal link_lookup_called
        link_lookup_called = True
        return None

    async def fake_update_status(
        db_arg: Any,
        *,
        payment: DummyWataPayment,
        **kwargs: Any,
    ) -> DummyWataPayment:
        assert db_arg is db
        update_kwargs.update(kwargs)
        if "status" in kwargs:
            payment.status = kwargs["status"]
        if "is_paid" in kwargs:
            payment.is_paid = kwargs["is_paid"]
        if "metadata" in kwargs:
            payment.metadata_json = kwargs["metadata"]
        if "callback_payload" in kwargs:
            payment.callback_payload = kwargs["callback_payload"]
        if "terminal_public_id" in kwargs:
            payment.terminal_public_id = kwargs["terminal_public_id"]
        return payment

    monkeypatch.setattr(
        payment_service_module,
        "get_wata_payment_by_order_id",
        fake_get_by_order_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "get_wata_payment_by_link_id",
        fake_get_by_link_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_wata_payment_status",
        fake_update_status,
        raising=False,
    )

    payload = {
        "orderId": payment.order_id,
        "transactionStatus": "Declined",
        "terminalPublicId": "terminal-001",
    }

    processed = await service.process_wata_webhook(db, payload)

    assert processed is True
    assert link_lookup_called is False
    assert payment.status == "Declined"
    assert payment.is_paid is False
    assert payment.metadata_json.get("last_webhook") == payload
    assert payment.callback_payload == payload
    assert payment.terminal_public_id == "terminal-001"
    assert update_kwargs["status"] == "Declined"
    assert update_kwargs["is_paid"] is False


@pytest.mark.anyio("asyncio")
async def test_process_wata_webhook_finalizes_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(None)
    db = DummySession()
    payment = DummyWataPayment()
    finalize_called = False

    async def fake_get_by_order_id(*_: Any, **__: Any) -> DummyWataPayment:
        return payment

    async def fake_update_status(
        db_arg: Any,
        *,
        payment: DummyWataPayment,
        **kwargs: Any,
    ) -> DummyWataPayment:
        if "metadata" in kwargs:
            payment.metadata_json = kwargs["metadata"]
        if "callback_payload" in kwargs:
            payment.callback_payload = kwargs["callback_payload"]
        if "status" in kwargs:
            payment.status = kwargs["status"]
        return payment

    async def fake_finalize(
        db_arg: Any,
        payment_arg: DummyWataPayment,
        payload_arg: Dict[str, Any],
    ) -> DummyWataPayment:
        nonlocal finalize_called
        finalize_called = True
        payment_arg.is_paid = True
        return payment_arg

    monkeypatch.setattr(
        payment_service_module,
        "get_wata_payment_by_order_id",
        fake_get_by_order_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "get_wata_payment_by_link_id",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_wata_payment_status",
        fake_update_status,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_finalize_wata_payment",
        fake_finalize,
        raising=False,
    )

    payload = {
        "orderId": payment.order_id,
        "transactionStatus": "Paid",
        "transactionId": "tx-001",
    }

    processed = await service.process_wata_webhook(db, payload)

    assert processed is True
    assert finalize_called is True
    assert payment.is_paid is True
    assert payment.metadata_json.get("last_webhook") == payload


@pytest.mark.anyio("asyncio")
async def test_process_wata_webhook_returns_false_when_payment_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    db = DummySession()

    async def fake_get_by_order_id(*_: Any, **__: Any) -> None:
        return None

    async def fake_get_by_link_id(*_: Any, **__: Any) -> None:
        return None

    async def fail_update(*_: Any, **__: Any) -> None:
        pytest.fail("update_wata_payment_status should not be called")

    monkeypatch.setattr(
        payment_service_module,
        "get_wata_payment_by_order_id",
        fake_get_by_order_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "get_wata_payment_by_link_id",
        fake_get_by_link_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        "update_wata_payment_status",
        fail_update,
        raising=False,
    )

    payload = {
        "orderId": "missing-order",
        "transactionStatus": "Paid",
    }

    processed = await service.process_wata_webhook(db, payload)

    assert processed is False


# ------------------------------------------------------ терминалы витрин

import json  # noqa: E402

import app.services.payment.wata as wata_mixin_module  # noqa: E402
from app.utils import bot_registry  # noqa: E402

QUATTRA_TOKEN = "quattra-token"
QUATTRA_TERMINAL_ID = "3a000000-0000-0000-0000-00000000q001"


@pytest.fixture
def quattra_terminal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "WATA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "WATA_ACCESS_TOKEN", "main-token", raising=False)
    monkeypatch.setattr(settings, "WATA_TERMINAL_PUBLIC_ID", "main-terminal", raising=False)
    monkeypatch.setattr(settings, "WATA_SUCCESS_REDIRECT_URL", "https://t.me/main", raising=False)
    monkeypatch.setattr(
        settings,
        "WATA_EXTRA_TERMINALS",
        json.dumps(
            {
                "Quattra": {
                    "access_token": QUATTRA_TOKEN,
                    "terminal_public_id": QUATTRA_TERMINAL_ID,
                    "success_url": "https://t.me/quattra_vpn_bot",
                },
                "broken": {"access_token": "x"},
            }
        ),
        raising=False,
    )
    yield
    bot_registry.set_wata_terminal(777, None)


def test_extra_terminals_parsed_and_resolved(quattra_terminal) -> None:
    from app.services.wata_service import wata_service_for_terminal

    assert set(settings.get_wata_extra_terminals()) == {"quattra"}
    quattra = wata_service_for_terminal("quattra")
    assert quattra.access_token == QUATTRA_TOKEN
    assert quattra.success_url == "https://t.me/quattra_vpn_bot"
    assert quattra.fail_url == settings.WATA_FAIL_REDIRECT_URL
    assert wata_service_for_terminal(None).access_token == "main-token"
    assert wata_service_for_terminal("default").success_url == "https://t.me/main"
    assert wata_service_for_terminal("broken") is None


def _capture_local_payment(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment(payment_id=5)

    async def no_snapshot(*_: Any) -> None:
        return None

    monkeypatch.setattr(payment_service_module, "create_wata_payment", fake_create, raising=False)
    monkeypatch.setattr(wata_mixin_module, "build_invoice_checkout_snapshot", no_snapshot)
    monkeypatch.setattr(settings, "WATA_MIN_AMOUNT_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "WATA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)
    return captured


@pytest.mark.anyio("asyncio")
async def test_storefront_with_own_terminal_invoices_into_it(
    quattra_terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_stub = StubWataService({"id": "main-link", "url": "https://wata/main"})
    quattra_stub = StubWataService({"id": "q-link", "url": "https://wata/q"})
    monkeypatch.setattr(
        wata_mixin_module,
        "wata_service_for_terminal",
        lambda terminal: quattra_stub if terminal == "quattra" else None,
    )
    captured = _capture_local_payment(monkeypatch)
    service = _make_service(main_stub)
    bot_registry.set_wata_terminal(777, "quattra")

    result = await service.create_wata_payment(
        db=DummySession(), user_id=42, amount_kopeks=29_000, description="x",
        metadata={"payment_router": {"bot_id": 777}},
    )

    assert result["payment_link_id"] == "q-link"
    assert main_stub.calls == []
    assert len(quattra_stub.calls) == 1
    assert captured["metadata"]["wata_terminal"] == "quattra"


@pytest.mark.anyio("asyncio")
async def test_bot_without_own_terminal_uses_main(
    quattra_terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_stub = StubWataService({"id": "main-link", "url": "https://wata/main"})
    captured = _capture_local_payment(monkeypatch)
    service = _make_service(main_stub)

    result = await service.create_wata_payment(
        db=DummySession(), user_id=42, amount_kopeks=29_000, description="x",
        metadata={"payment_router": {"bot_id": 555}},
    )

    assert result["payment_link_id"] == "main-link"
    assert "wata_terminal" not in captured["metadata"]


@pytest.mark.anyio("asyncio")
async def test_storefront_terminal_from_brand_context_without_router(
    quattra_terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.branding.context import brand_scope

    quattra_stub = StubWataService({"id": "q-link", "url": "https://wata/q"})
    monkeypatch.setattr(
        wata_mixin_module, "wata_service_for_terminal", lambda terminal: quattra_stub
    )
    _capture_local_payment(monkeypatch)
    service = _make_service(StubWataService({"id": "main-link"}))
    bot_registry.set_wata_terminal(777, "quattra")

    with brand_scope(777):
        result = await service.create_wata_payment(
            db=DummySession(), user_id=42, amount_kopeks=29_000, description="x"
        )

    assert result["payment_link_id"] == "q-link"


@pytest.mark.anyio("asyncio")
async def test_storefront_terminal_without_keys_does_not_fall_back_to_main(
    quattra_terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_stub = StubWataService({"id": "main-link"})
    service = _make_service(main_stub)
    bot_registry.set_wata_terminal(777, "unknown")

    result = await service.create_wata_payment(
        db=DummySession(), user_id=42, amount_kopeks=29_000, description="x",
        metadata={"payment_router": {"bot_id": 777}},
    )

    assert result is None
    assert main_stub.calls == []


@pytest.mark.anyio("asyncio")
async def test_status_check_asks_terminal_of_the_invoice(
    quattra_terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = DummyWataPayment()
    payment.metadata_json = {"wata_terminal": "quattra"}
    asked: list[str] = []

    class LinkStub:
        def __init__(self, name: str) -> None:
            self.name = name

        async def get_payment_link(self, link_id: str) -> Dict[str, Any]:
            asked.append(self.name)
            return {"id": link_id, "status": "Opened"}

    monkeypatch.setattr(
        wata_mixin_module, "wata_service_for_terminal", lambda terminal: LinkStub(terminal)
    )

    async def fake_get_by_id(*_: Any) -> DummyWataPayment:
        return payment

    monkeypatch.setattr(payment_service_module, "get_wata_payment_by_id", fake_get_by_id, raising=False)
    service = _make_service(LinkStub("main"))  # type: ignore[arg-type]

    result = await service.get_wata_payment_status(DummySession(), 1)

    assert result["is_paid"] is False
    assert asked == ["quattra"]
