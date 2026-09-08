"""Подпись, форматирование сумм и сборка запросов клиента 1Payment."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from typing import Any, Dict

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.services.onepayment_service import (  # noqa: E402
    OnePaymentAPIError,
    OnePaymentService,
    build_callback_sign,
    build_sign,
    canonical_query,
    format_amount,
    parse_amount_to_kopeks,
    parse_status,
    stringify_value,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_sign_matches_documentation_example() -> None:
    """Пример из /pages/sbp/init-form: init_form + параметры A→Z + ключ."""
    params = {
        "partner_id": 123,
        "project_id": 456,
        "amount": "100.00",
        "payment_type": "sbp",
        "user_data": "order_777",
        "shop_url": "myshop.ru",
    }
    expected_base = (
        "init_formamount=100.00&partner_id=123&payment_type=sbp&project_id=456"
        "&shop_url=myshop.ru&user_data=order_777secret_key"
    )
    assert build_sign("init_form", params, "secret_key") == hashlib.md5(
        expected_base.encode("utf-8")
    ).hexdigest()


def test_sign_ignores_sign_and_none_values() -> None:
    params = {"b": "2", "a": "1", "sign": "junk", "c": None}
    assert canonical_query(params) == "a=1&b=2"


def test_callback_sign_has_no_method_prefix() -> None:
    payload = {"status": "3", "user_data": "x", "sign": "ignored"}
    expected = hashlib.md5(b"status=3&user_data=xkey").hexdigest()
    assert build_callback_sign(payload, "key") == expected


def test_stringify_value_bool_and_none() -> None:
    assert stringify_value(True) == "true"
    assert stringify_value(False) == "false"
    assert stringify_value(None) is None
    assert stringify_value(50) == "50"


def test_format_amount_two_decimals() -> None:
    assert format_amount(10000) == "100.00"
    assert format_amount(12345) == "123.45"
    assert format_amount(5) == "0.05"


def test_parse_helpers() -> None:
    assert parse_status("3") == 3
    assert parse_status(None) is None
    assert parse_amount_to_kopeks("50") == 5000
    assert parse_amount_to_kopeks("48.5") == 4850
    assert parse_amount_to_kopeks("") is None


def _service(monkeypatch: pytest.MonkeyPatch) -> OnePaymentService:
    monkeypatch.setattr(settings, "WEBHOOK_URL", "https://bot.example", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_SUCCESS_URL", None, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_FAILURE_URL", None, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_INIT_METHOD", "form", raising=False)
    return OnePaymentService(
        partner_id="1234",
        project_id="5678",
        api_key="secret",
        shop_url="https://shop.example",
    )


def test_verify_callback_sign_accepts_valid_and_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch)
    payload: Dict[str, Any] = {
        "payment_type": "sbp",
        "order_id": "o1",
        "status": "3",
        "merchant_price": "100.00",
        "user_data": "1p_1_abc",
    }
    payload["sign"] = build_callback_sign(payload, "secret")
    assert service.verify_callback_sign(payload) is True

    payload["sign"] = "0" * 32
    assert service.verify_callback_sign(payload) is False

    payload.pop("sign")
    assert service.verify_callback_sign(payload) is False


@pytest.mark.anyio
async def test_create_payment_form_sends_subscribe_and_signed_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch)
    captured: Dict[str, Any] = {}

    async def fake_request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        captured["method"] = method
        captured["params"] = service._prepare(method, params)
        return {"url": "https://merchant.1payment.com/xZ5g7F"}

    monkeypatch.setattr(service, "_request", fake_request)

    result = await service.create_payment(
        amount_kopeks=15_000,
        user_data="1p_7_abc",
        description="Тариф",
        language="ru",
        user_id=7,
    )

    assert result["url"] == "https://merchant.1payment.com/xZ5g7F"
    assert result["method"] == "init_form"
    params = captured["params"]
    assert captured["method"] == "init_form"
    assert params["amount"] == "150.00"
    assert params["subscribe"] == "1"
    assert params["payment_type"] == "sbp"
    assert params["success_url"] == "https://bot.example/payment-success"
    assert params["failure_url"] == "https://bot.example/payment-failed"
    assert params["lang"] == "ru"
    # Подпись считается по тем же строкам, что отправлены.
    unsigned = {k: v for k, v in params.items() if k != "sign"}
    assert params["sign"] == build_sign("init_form", unsigned, "secret")


@pytest.mark.anyio
async def test_charge_token_uses_init_payment_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch)
    captured: Dict[str, Any] = {}

    async def fake_request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        captured["method"] = method
        captured["params"] = dict(params)
        return {"order_id": "8p3b", "status": 2, "status_description": "PENDING"}

    monkeypatch.setattr(service, "_request", fake_request)

    result = await service.charge_token(
        token="sbp_t_1a2b3c", amount_kopeks=50_000, user_data="1p_7_r1", description="Продление"
    )

    assert captured["method"] == "init_payment"
    assert captured["params"]["token"] == "sbp_t_1a2b3c"
    assert captured["params"]["amount"] == "500.00"
    assert "subscribe" not in captured["params"]
    assert result["order_id"] == "8p3b"
    assert result["status"] == 2


@pytest.mark.anyio
async def test_get_payment_status_requires_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch)
    with pytest.raises(OnePaymentAPIError):
        await service.get_payment_status()
