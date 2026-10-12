"""AuraPay: подпись вебхука, счёт, зачисление и роутинг только у своей витрины."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment.aurapay as aurapay_mixin_module  # noqa: E402
import app.services.payment_service as payment_service_module  # noqa: E402
from app.branding.context import brand_scope  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.models import PaymentMethod  # noqa: E402
from app.services.aurapay_service import AurapayService  # noqa: E402
from app.services.payment_gateway_router import PaymentGatewayRouter  # noqa: E402
from app.services.payment_service import PaymentService  # noqa: E402
from app.utils import bot_registry  # noqa: E402

SECRET = "test-secret-2"
SHOP_ID = "9bee9309-2585-4332-b63d-e1d897f7ce84"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_registry():
    bot_registry.clear()
    yield
    bot_registry.clear()


def _sign(concatenated: str) -> str:
    return hmac.new(SECRET.encode(), concatenated.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- подпись

DOC_BODY = {
    "id": "9beea835-0937-4b5c-8f5a-c3a0d0e60346",
    "amount": "1250.00",
    "status": "PAID",
    "comment": "test",
    "created_at": "2024-01-30 19:07:45",
    "expires_at": "2024-01-30 20:07:45",
    "service": "sbp",
    "payer_details": "794*****254",
    "payer_ip": "37.251.8.13",
    "shop_id": SHOP_ID,
    "order_id": "11113",
    "custom_fields": None,
}


def _doc_concatenation(body: Dict[str, Any]) -> str:
    return "".join("" if body[k] is None else str(body[k]) for k in sorted(body))


def test_signature_matches_documented_algorithm():
    service = AurapayService(secret_key=SECRET)
    raw = json.dumps(DOC_BODY).encode()

    assert service.verify_signature(raw, _sign(_doc_concatenation(DOC_BODY)))
    assert AurapayService.build_signature(DOC_BODY, SECRET) == _sign(_doc_concatenation(DOC_BODY))


def test_signature_rejects_tampered_body_and_missing_header():
    service = AurapayService(secret_key=SECRET)
    signature = _sign(_doc_concatenation(DOC_BODY))
    tampered = dict(DOC_BODY, amount="99999.00")

    assert not service.verify_signature(json.dumps(tampered).encode(), signature)
    assert not service.verify_signature(json.dumps(DOC_BODY).encode(), None)
    assert not service.verify_signature(b"not json", signature)


def test_signature_keeps_numbers_as_written_in_json():
    service = AurapayService(secret_key=SECRET)
    raw = b'{"order_id": "1", "amount": 1250.00, "status": "PAID"}'

    # PHP-сторона могла склеить «1250.00» как в теле или 1250 как float
    assert service.verify_signature(raw, _sign("1250.001PAID"))
    assert service.verify_signature(raw, _sign("12501PAID"))


# -------------------------------------------------------------------- счёт


class StubAurapayService:
    def __init__(self, response: Optional[Dict[str, Any]] = None) -> None:
        self.response = response or {}
        self.calls: List[Dict[str, Any]] = []
        self.status_response: Dict[str, Any] = {}

    async def create_invoice(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self.response

    async def get_invoice_status(self, **kwargs: Any) -> Dict[str, Any]:
        return self.status_response


def _make_service(stub: Optional[StubAurapayService], bot: Any = None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = bot
    service.aurapay_service = stub
    return service


@pytest.mark.anyio
async def test_create_invoice_stores_router_metadata(monkeypatch):
    stub = StubAurapayService(
        {
            "id": "inv-1",
            "status": "PENDING",
            "payment_data": {"url": "https://payform.aurapay.tech/inv-1"},
        }
    )
    service = _make_service(stub)
    captured: Dict[str, Any] = {}

    async def fake_create(db: Any, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(id=555, status=kwargs["status"])

    async def no_snapshot(*_: Any) -> None:
        return None

    monkeypatch.setattr(payment_service_module, "create_aurapay_payment", fake_create, raising=False)
    monkeypatch.setattr(aurapay_mixin_module, "build_invoice_checkout_snapshot", no_snapshot)
    monkeypatch.setattr(settings, "AURAPAY_MIN_AMOUNT_KOPEKS", 1000, raising=False)
    monkeypatch.setattr(settings, "AURAPAY_MAX_AMOUNT_KOPEKS", 1_000_000, raising=False)
    monkeypatch.setattr(settings, "WEBHOOK_URL", "https://hooks.example", raising=False)

    result = await service.create_aurapay_payment(
        None, user_id=42, amount_kopeks=29_000, description="Пополнение",
        metadata={"payment_router": {"bot_id": 777}},
    )

    assert result["local_payment_id"] == 555
    assert result["payment_url"] == "https://payform.aurapay.tech/inv-1"
    assert result["invoice_id"] == "inv-1"
    assert stub.calls[0]["amount_kopeks"] == 29_000
    assert stub.calls[0]["callback_url"] == "https://hooks.example/aurapay-webhook"
    assert captured["order_id"].startswith("aurapay_42_")
    assert captured["metadata"]["payment_router"] == {"bot_id": 777}


@pytest.mark.anyio
async def test_create_invoice_respects_minimum(monkeypatch):
    stub = StubAurapayService()
    service = _make_service(stub)
    monkeypatch.setattr(settings, "AURAPAY_MIN_AMOUNT_KOPEKS", 1000, raising=False)

    assert await service.create_aurapay_payment(None, user_id=1, amount_kopeks=500, description="x") is None
    assert stub.calls == []


# ------------------------------------------------------------- зачисление


class DummyPayment:
    def __init__(self, **overrides: Any) -> None:
        self.id = 1
        self.user_id = 42
        self.order_id = "aurapay_42_abc"
        self.invoice_id = "inv-1"
        self.amount_kopeks = 29_000
        self.status = "PENDING"
        self.is_paid = False
        self.paid_at: Optional[datetime] = None
        self.metadata_json: Dict[str, Any] = {"invoice_message": {"chat_id": 100, "message_id": 5}}
        self.callback_payload = None
        self.transaction_id: Optional[int] = None
        self.created_at = datetime.utcnow()
        for key, value in overrides.items():
            setattr(self, key, value)


class DummyUser:
    def __init__(self) -> None:
        self.id = 42
        self.telegram_id = 100
        self.bot_id = 777
        self.language = "ru"
        self.balance_kopeks = 0
        self.has_made_first_topup = False
        self.updated_at = datetime.utcnow()
        self.subscription = None

    def get_primary_promo_group(self) -> None:
        return None


class DummySession:
    async def commit(self) -> None:
        return None

    async def refresh(self, *_: Any, **__: Any) -> None:
        return None


class RecordingBot:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: List[Any] = []
        self.deleted: List[Any] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


def _patch_finalize(monkeypatch, payment: DummyPayment, user: DummyUser) -> Dict[str, Any]:
    calls: Dict[str, Any] = {"transactions": [], "router": [], "cas": 0}

    async def fake_cas(db: Any, payment_id: int, **_: Any) -> bool:
        calls["cas"] += 1
        if payment.is_paid:
            return False
        payment.is_paid = True
        payment.status = "PAID"
        return True

    async def fake_get(db: Any, *_: Any) -> DummyPayment:
        return payment

    async def fake_update(db: Any, payment_arg: Any, **kwargs: Any) -> DummyPayment:
        if kwargs.get("metadata") is not None:
            payment_arg.metadata_json = kwargs["metadata"]
        if kwargs.get("status") is not None:
            payment_arg.status = kwargs["status"]
        return payment_arg

    async def fake_get_user(db: Any, user_id: int) -> DummyUser:
        return user

    async def fake_create_transaction(db: Any, **kwargs: Any) -> SimpleNamespace:
        calls["transactions"].append(kwargs)
        return SimpleNamespace(id=900, **kwargs)

    async def fake_link(db: Any, payment_arg: Any, transaction_id: int) -> DummyPayment:
        payment_arg.transaction_id = transaction_id
        return payment_arg

    async def fake_router(db: Any, **kwargs: Any) -> None:
        calls["router"].append(kwargs)

    async def noop(*_: Any, **__: Any) -> Any:
        return False

    async def fake_keyboard(self: Any, user: Any) -> None:
        return None

    for name, fn in {
        "mark_aurapay_payment_paid_cas": fake_cas,
        "get_aurapay_payment_by_id": fake_get,
        "get_aurapay_payment_by_order_id": fake_get,
        "get_aurapay_payment_by_invoice_id": fake_get,
        "update_aurapay_payment": fake_update,
        "get_user_by_id": fake_get_user,
        "create_transaction": fake_create_transaction,
        "link_aurapay_payment_to_transaction": fake_link,
    }.items():
        monkeypatch.setattr(payment_service_module, name, fn, raising=False)
    monkeypatch.setattr(aurapay_mixin_module, "_record_router_payment", fake_router)
    monkeypatch.setattr(aurapay_mixin_module, "auto_purchase_saved_cart_after_topup", noop)
    monkeypatch.setattr("app.services.referral_service.process_referral_topup", noop, raising=False)
    monkeypatch.setattr("app.services.user_cart_service.user_cart_service.has_user_cart", noop, raising=False)
    monkeypatch.setattr(
        "app.services.admin_notification_service.AdminNotificationService.send_balance_topup_notification",
        noop,
        raising=False,
    )
    monkeypatch.setattr(PaymentService, "build_topup_success_keyboard", fake_keyboard, raising=False)
    monkeypatch.setattr(settings, "AURAPAY_SHOP_ID", SHOP_ID, raising=False)
    return calls


@pytest.mark.anyio
async def test_paid_webhook_credits_once_and_talks_through_users_bot(monkeypatch):
    payment = DummyPayment()
    user = DummyUser()
    calls = _patch_finalize(monkeypatch, payment, user)
    primary, storefront = RecordingBot("primary"), RecordingBot("storefront")
    bot_registry.register_bot(777, Path("logo.png"), storefront)
    service = _make_service(StubAurapayService(), bot=primary)
    body = dict(DOC_BODY, order_id=payment.order_id, amount="290.00")

    db = DummySession()
    assert await service.process_aurapay_webhook(db, body) is True
    assert await service.process_aurapay_webhook(db, body) is True  # повтор вебхука

    assert user.balance_kopeks == 29_000
    assert len(calls["transactions"]) == 1
    assert calls["transactions"][0]["payment_method"] == PaymentMethod.AURAPAY
    assert calls["router"][0]["gateway"] == "aurapay"
    assert storefront.deleted == [(100, 5)]
    assert len(storefront.sent) == 1
    assert primary.sent == []


@pytest.mark.anyio
async def test_webhook_from_other_shop_or_underpaid_is_not_credited(monkeypatch):
    payment = DummyPayment()
    user = DummyUser()
    calls = _patch_finalize(monkeypatch, payment, user)
    service = _make_service(StubAurapayService())

    foreign = dict(DOC_BODY, order_id=payment.order_id, amount="290.00", shop_id="other")
    assert await service.process_aurapay_webhook(DummySession(), foreign) is False

    underpaid = dict(DOC_BODY, order_id=payment.order_id, amount="10.00")
    assert await service.process_aurapay_webhook(DummySession(), underpaid) is True

    assert user.balance_kopeks == 0
    assert calls["transactions"] == []


@pytest.mark.anyio
async def test_status_check_finalizes_paid_invoice(monkeypatch):
    payment = DummyPayment()
    user = DummyUser()
    _patch_finalize(monkeypatch, payment, user)
    stub = StubAurapayService()
    stub.status_response = {"id": "inv-1", "status": "PAID", "amount": 290}
    service = _make_service(stub)

    result = await service.get_aurapay_payment_status(DummySession(), payment.id)

    assert result["is_paid"] is True
    assert user.balance_kopeks == 29_000


# ------------------------------------------------------------------ роутер


@pytest.fixture
def aurapay_only_for_storefront(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_ONEPAYMENT", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_AURAPAY", 0, raising=False)
    monkeypatch.setattr(type(settings), "is_platega_universal_enabled", lambda self: True)
    monkeypatch.setattr(type(settings), "is_aurapay_enabled", lambda self: True)
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 10_000_000, raising=False)
    monkeypatch.setattr(settings, "AURAPAY_MIN_AMOUNT_KOPEKS", 1000, raising=False)
    monkeypatch.setattr(settings, "AURAPAY_MAX_AMOUNT_KOPEKS", 10_000_000, raising=False)
    bot_registry.set_payment_weights(777, {"aurapay": 1}, "bot")


def test_aurapay_is_routed_only_in_its_storefront(aurapay_only_for_storefront):
    router = PaymentGatewayRouter()

    assert router.enabled_gateways() == ["platega"]
    assert router.enabled_gateways(bot_id=555) == ["platega"]
    with brand_scope(777):
        assert router.enabled_gateways() == ["aurapay"]
        assert router.pick_order(29_000, rng=random.Random(1)) == ["aurapay"]
        assert router.combined_min_kopeks() == 1000


@pytest.mark.anyio
async def test_storefront_invoice_goes_through_aurapay(aurapay_only_for_storefront, monkeypatch):
    router = PaymentGatewayRouter()
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_LOG_ENABLED", False, raising=False)

    async def fake_aurapay(payment_service, db, user, amount, description, language, metadata):
        return {
            "payment_url": "https://payform.aurapay.tech/x",
            "local_payment_id": 7,
            "invoice_id": "x",
        }

    monkeypatch.setattr(router, "_create_aurapay", fake_aurapay)
    user = SimpleNamespace(id=1, bot_id=777, language="ru")

    routed = await router.create_invoice(
        None, payment_service=None, user=user, amount_kopeks=29_000,
        source="balance_topup", description="d",
    )

    assert routed.gateway == "aurapay"
    assert routed.check_callback == "check_aurapay_7"
    assert routed.external_id == "x"
