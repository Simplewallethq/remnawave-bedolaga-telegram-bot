import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver.payments import create_payment_router


class DummyBot:
    pass
@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRIBUTE_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "TRIBUTE_API_KEY", None, raising=False)
    monkeypatch.setattr(settings, "TRIBUTE_WEBHOOK_PATH", "/tribute", raising=False)
    monkeypatch.setattr(settings, "MULENPAY_WEBHOOK_PATH", "/mulen", raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_API_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_WEBHOOK_PATH", "/cryptobot", raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_WEBHOOK_SECRET", None, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_WEBHOOK_PATH", "/yookassa", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "shop", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "key", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_TRUSTED_PROXY_NETWORKS", "", raising=False)
    monkeypatch.setattr(settings, "WEBHOOK_URL", "http://test", raising=False)


def _get_route(router, path: str, method: str = "POST"):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route
    raise AssertionError(f"Route {path} with method {method} not found")


def _build_request(
    path: str,
    body: bytes,
    headers: dict[str, str],
    client_ip: str | None = "185.71.76.1",
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": path,
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }

    if client_ip is not None:
        scope["client"] = (client_ip, 12345)

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.anyio
async def test_tribute_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRIBUTE_ENABLED", True, raising=False)

    process_mock = AsyncMock(return_value={"status": "ok"})

    class StubTributeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def process_webhook(self, payload: str):  # type: ignore[override]
            return await process_mock(payload)

    class StubTributeAPI:
        @staticmethod
        def verify_webhook_signature(payload: str, signature: str) -> bool:  # noqa: D401 - test stub
            return True

    monkeypatch.setattr("app.webserver.payments.TributeService", StubTributeService)
    monkeypatch.setattr("app.webserver.payments.TributeAPI", StubTributeAPI)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    route = _get_route(router, settings.TRIBUTE_WEBHOOK_PATH)
    request = _build_request(
        settings.TRIBUTE_WEBHOOK_PATH,
        body=json.dumps({"event": "payment"}).encode("utf-8"),
        headers={"trbt-signature": "sig"},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body.decode("utf-8"))["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_unknown_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={},
        client_ip=None,
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reason"] == "unknown_ip"
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_forbidden_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={},
        client_ip="8.8.8.8",
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reason"] == "forbidden_ip"
    assert payload["ip"] == "8.8.8.8"
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_forbidden_ip_ignores_spoofed_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={"X-Forwarded-For": "185.71.76.10"},
        client_ip="8.8.8.8",
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reason"] == "forbidden_ip"
    assert payload["ip"] == "8.8.8.8"
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_forbidden_ip_ignores_spoofed_forwarded_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={"X-Forwarded-For": "185.71.76.10, 8.8.8.8"},
        client_ip="10.0.0.5",
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reason"] == "forbidden_ip"
    assert payload["ip"] == "8.8.8.8"
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_allowed_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={},
        client_ip="185.71.76.10",
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_forwarded_header_when_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={"X-Forwarded-For": "185.71.76.10"},
        client_ip="10.0.0.5",
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_cf_connecting_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={"Cf-Connecting-Ip": "185.71.76.10"},
        client_ip="172.64.223.133",
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_trusted_forwarded_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_TRUSTED_PROXY_NETWORKS", "203.0.113.0/24", raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={"X-Forwarded-For": "185.71.76.10, 203.0.113.10"},
        client_ip="10.0.0.5",
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_trusted_public_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_TRUSTED_PROXY_NETWORKS", "198.51.100.0/24", raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({"event": "payment.succeeded"}).encode("utf-8"),
        headers={"X-Forwarded-For": "185.71.76.10, 198.51.100.10"},
        client_ip="198.51.100.20",
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    payload = {"event": "payment.succeeded"}
    body = json.dumps(payload).encode("utf-8")
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=body,
        headers={},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_webhook_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    payload = {"event": "payment.canceled"}
    body = json.dumps(payload).encode("utf-8")
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=body,
        headers={},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_webhook_with_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr("app.webserver.payments.get_db", fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    payload = {"event": "payment.succeeded"}
    body = json.dumps(payload).encode("utf-8")
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=body,
        headers={"Signature": "dummy"},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "ok"
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_cryptobot_missing_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CRYPTOBOT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_API_TOKEN", "token", raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_WEBHOOK_SECRET", "secret", raising=False)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    route = _get_route(router, settings.CRYPTOBOT_WEBHOOK_PATH)
    request = _build_request(
        settings.CRYPTOBOT_WEBHOOK_PATH,
        body=json.dumps({"test": "value"}).encode("utf-8"),
        headers={},
    )

    response = await route.endpoint(request)

    assert response.status_code == 401
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reason"] == "missing_signature"


@pytest.mark.anyio
async def test_cryptobot_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CRYPTOBOT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_API_TOKEN", "token", raising=False)
    monkeypatch.setattr(settings, "CRYPTOBOT_WEBHOOK_SECRET", "secret", raising=False)

    class StubCryptoBotService:
        @staticmethod
        def verify_webhook_signature(body: str, signature: str) -> bool:  # noqa: D401 - test stub
            return False

    monkeypatch.setattr("app.external.cryptobot.CryptoBotService", StubCryptoBotService)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    route = _get_route(router, settings.CRYPTOBOT_WEBHOOK_PATH)
    request = _build_request(
        settings.CRYPTOBOT_WEBHOOK_PATH,
        body=json.dumps({"test": "value"}).encode("utf-8"),
        headers={"Crypto-Pay-API-Signature": "sig"},
    )

    response = await route.endpoint(request)

    assert response.status_code == 401


def _has_route(router, path: str, method: str = "POST") -> bool:
    if router is None:
        return False
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return True
    return False


def test_routes_registered_when_configured_but_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Роуты гейтятся по «настроен», а не «включён».

    Иначе выключение шлюза в рантайме осиротит уже выставленные счета:
    вебхук придёт в 404, деньги спишутся, баланс не пополнится.
    """
    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "shop", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "key", raising=False)

    monkeypatch.setattr(settings, "WATA_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "WATA_ACCESS_TOKEN", "token", raising=False)
    monkeypatch.setattr(settings, "WATA_TERMINAL_PUBLIC_ID", "terminal", raising=False)
    monkeypatch.setattr(settings, "WATA_WEBHOOK_PATH", "/wata", raising=False)

    monkeypatch.setattr(settings, "PLATEGA_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MERCHANT_ID", "merchant", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_SECRET", "secret", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_WEBHOOK_PATH", "/platega", raising=False)

    router = create_payment_router(DummyBot(), SimpleNamespace())

    assert _has_route(router, "/yookassa")
    assert _has_route(router, "/wata")
    assert _has_route(router, "/platega")


def test_routes_absent_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", None, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", None, raising=False)
    monkeypatch.setattr(settings, "WATA_ACCESS_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "WATA_TERMINAL_PUBLIC_ID", None, raising=False)
    monkeypatch.setattr(settings, "WATA_WEBHOOK_PATH", "/wata", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MERCHANT_ID", None, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_SECRET", None, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_WEBHOOK_PATH", "/platega", raising=False)

    router = create_payment_router(DummyBot(), SimpleNamespace())

    assert not _has_route(router, "/yookassa")
    assert not _has_route(router, "/wata")
    assert not _has_route(router, "/platega")


# --------------------------------------------------------------------- 1Payment


def _onepayment_settings(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(settings, "ONEPAYMENT_ENABLED", enabled, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PARTNER_ID", "1234", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PROJECT_ID", "5678", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_API_KEY", "secret", raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_WEBHOOK_PATH", "/onepayment", raising=False)


def test_onepayment_route_registered_when_configured_but_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _onepayment_settings(monkeypatch, enabled=False)
    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert _has_route(router, "/onepayment")


def test_onepayment_route_absent_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ONEPAYMENT_PARTNER_ID", None, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_PROJECT_ID", None, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_API_KEY", None, raising=False)
    monkeypatch.setattr(settings, "ONEPAYMENT_WEBHOOK_PATH", "/onepayment", raising=False)
    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert not _has_route(router, "/onepayment")


@pytest.mark.anyio
async def test_onepayment_webhook_rejects_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _onepayment_settings(monkeypatch)
    process_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.webserver.payments._process_payment_service_callback", process_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, "/onepayment")
    body = json.dumps({"status": 3, "user_data": "1p_1_a", "sign": "0" * 32}).encode("utf-8")
    response = await route.endpoint(
        _build_request("/onepayment", body=body, headers={"content-type": "application/json"})
    )

    assert response.status_code == 401
    process_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_onepayment_webhook_accepts_signed_json_with_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Числа из JSON участвуют в подписи в исходном текстовом виде."""
    from app.services.onepayment_service import build_callback_sign

    _onepayment_settings(monkeypatch)
    process_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.webserver.payments._process_payment_service_callback", process_mock)

    fields = {
        "payment_type": "sbp",
        "order_id": "8p3b",
        "project_id": "5678",
        "status": "3",
        "status_description": "SUCCESS",
        "merchant_price": "100.50",
        "user_price": "97.5",
        "currency": "RUB",
        "account": "sbp",
        "user_data": "1p_1_abc",
        "token": "sbp_t_1",
        "test": "false",
    }
    sign = build_callback_sign(fields, "secret")
    raw_json = (
        '{"payment_type":"sbp","order_id":"8p3b","project_id":5678,"status":3,'
        '"status_description":"SUCCESS","merchant_price":100.50,"user_price":97.5,'
        '"currency":"RUB","account":"sbp","user_data":"1p_1_abc","token":"sbp_t_1",'
        f'"test":false,"redirect_url":null,"sign":"{sign}"}}'
    )

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, "/onepayment")
    response = await route.endpoint(
        _build_request(
            "/onepayment", body=raw_json.encode("utf-8"), headers={"content-type": "application/json"}
        )
    )

    assert response.status_code == 200
    process_mock.assert_awaited_once()
    _, payload, method_name = process_mock.await_args.args
    assert method_name == "process_onepayment_webhook"
    assert payload["merchant_price"] == "100.50"
    assert payload["status"] == "3"
    assert payload["test"] == "false"
    assert "redirect_url" not in payload


@pytest.mark.anyio
async def test_onepayment_webhook_accepts_form_urlencoded(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.onepayment_service import build_callback_sign

    _onepayment_settings(monkeypatch)
    process_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.webserver.payments._process_payment_service_callback", process_mock)

    fields = {"status": "4", "user_data": "1p_1_abc", "status_code": "05"}
    fields["sign"] = build_callback_sign(fields, "secret")
    body = "&".join(f"{k}={v}" for k, v in fields.items()).encode("utf-8")

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, "/onepayment")
    response = await route.endpoint(
        _build_request(
            "/onepayment", body=body, headers={"content-type": "application/x-www-form-urlencoded"}
        )
    )

    assert response.status_code == 200
    process_mock.assert_awaited_once()
