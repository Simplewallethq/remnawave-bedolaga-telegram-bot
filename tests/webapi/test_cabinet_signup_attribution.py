from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils.signup_attribution import (
    acquisition_source_from,
    sanitize_signup_attribution,
)
from app.webapi.routes import cabinet as cabinet_module
from app.webapi.schemas.cabinet import OtpVerifyRequest

DIRECT = {
    "from": "letovps",
    "utm_source": "yandex-roman",
    "utm_medium": "cpc",
    "utm_campaign": "715898846",
    "utm_content": "1926254666105587320",
    "utm_term": "---autotargeting",
    "yclid": "10072207069929275391",
}


def test_sanitize_keeps_known_keys_and_drops_junk() -> None:
    raw = {**DIRECT, "evil": "x", "utm_term": "  ", "yclid": 10072207069929275391, "referrer": None}

    cleaned = sanitize_signup_attribution(raw)

    assert cleaned == {**{k: v for k, v in DIRECT.items() if k != "utm_term"}}
    assert "evil" not in cleaned


def test_sanitize_truncates_and_rejects_non_dict() -> None:
    assert len(sanitize_signup_attribution({"utm_campaign": "x" * 1000})["utm_campaign"]) == 255
    assert sanitize_signup_attribution("utm_source=yandex") is None
    assert sanitize_signup_attribution({"utm_source": ""}) is None
    assert sanitize_signup_attribution(None) is None


def test_acquisition_source_prefers_utm_source() -> None:
    assert acquisition_source_from(DIRECT) == "yandex-roman"
    assert acquisition_source_from({"from": "letovps"}) == "letovps"
    assert acquisition_source_from(None) is None


@pytest.mark.anyio("asyncio")
@pytest.mark.parametrize(
    ("attribution", "expected"),
    [
        (DIRECT, DIRECT),
        ({"utm_source": ["not", "a", "string"]}, None),
        (None, None),
    ],
)
async def test_otp_verify_saves_attribution_of_new_user(
    monkeypatch: pytest.MonkeyPatch, attribution, expected
) -> None:
    user = SimpleNamespace(id=7, status="active")
    create_web_user = AsyncMock(return_value=user)
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(cabinet_module, "_ensure_enabled", lambda: None)
    monkeypatch.setattr(cabinet_module, "_verify_registration_code", AsyncMock())
    monkeypatch.setattr(cabinet_module, "get_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(cabinet_module, "create_web_user", create_web_user)
    monkeypatch.setattr(cabinet_module, "create_trial_subscription", AsyncMock(return_value=None))
    monkeypatch.setattr(cabinet_module, "_auth_response", AsyncMock(return_value={"token": "t"}))

    payload = OtpVerifyRequest(email="new@example.com", code="123456", attribution=attribution)
    result = await cabinet_module.auth_otp_verify(payload, db=db)

    assert result == {"token": "t"}
    assert create_web_user.await_args.kwargs["signup_attribution"] == expected


@pytest.mark.anyio("asyncio")
async def test_otp_verify_does_not_touch_existing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = SimpleNamespace(id=8, status="active")
    create_web_user = AsyncMock()

    monkeypatch.setattr(cabinet_module, "_ensure_enabled", lambda: None)
    monkeypatch.setattr(cabinet_module, "_verify_registration_code", AsyncMock())
    monkeypatch.setattr(cabinet_module, "get_user_by_email", AsyncMock(return_value=existing))
    monkeypatch.setattr(cabinet_module, "ensure_remnawave_account", AsyncMock())
    monkeypatch.setattr(cabinet_module, "create_web_user", create_web_user)
    monkeypatch.setattr(cabinet_module, "_auth_response", AsyncMock(return_value={"token": "t"}))

    payload = OtpVerifyRequest(email="old@example.com", code="123456", attribution=DIRECT)
    await cabinet_module.auth_otp_verify(payload, db=SimpleNamespace())

    create_web_user.assert_not_awaited()
    assert not hasattr(existing, "signup_attribution")
