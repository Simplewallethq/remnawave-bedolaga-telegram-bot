"""Signature and request validation of the ChatSupport support actions."""

import hashlib
import hmac
import json

import pytest
from pydantic import ValidationError

from app.webapi.routes.support_actions import (
    BalanceAction,
    ExtendAction,
    TariffAction,
    verify_signature,
)

SECRET = "s3cret"
NOW = 1_800_000_000


def _sign(body: bytes, ts: int = NOW, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), f"{ts}\n".encode() + body, hashlib.sha256).hexdigest()


BODY = json.dumps({"amountKopeks": 10000, "reason": "компенсация", "operator": "Андрей", "key": "abcdef1234"}).encode()


def test_valid_signature_accepted():
    assert verify_signature(SECRET, str(NOW), _sign(BODY), BODY, NOW)


@pytest.mark.parametrize(
    "secret,ts_header,body,now",
    [
        ("other", str(NOW), BODY, NOW),  # wrong secret
        (SECRET, str(NOW), BODY.replace(b"10000", b"99999"), NOW),  # altered body
        (SECRET, str(NOW + 1), BODY, NOW),  # altered timestamp
        (SECRET, str(NOW), BODY, NOW + 301),  # stale
        (SECRET, str(NOW), BODY, NOW - 301),  # from the future
        (SECRET, "", BODY, NOW),  # no timestamp
    ],
)
def test_bad_signature_refused(secret, ts_header, body, now):
    assert not verify_signature(secret, ts_header, _sign(BODY), body, now)


def test_garbage_signature_refused():
    assert not verify_signature(SECRET, str(NOW), "zz", BODY, NOW)


def test_balance_validation():
    ok = BalanceAction.model_validate(json.loads(BODY))
    assert ok.amount_kopeks == 10000
    assert BalanceAction.model_validate({**json.loads(BODY), "amountKopeks": -500}).amount_kopeks == -500
    for bad in (
        {"amountKopeks": 0},
        {"amountKopeks": 10_000_001},
        {"reason": "  "},
        {"operator": ""},
        {"key": "short"},
        {"key": "bad key here"},
    ):
        with pytest.raises(ValidationError):
            BalanceAction.model_validate({**json.loads(BODY), **bad})


def test_tariff_and_extend_validation():
    base = {"reason": "x", "operator": "y", "key": "0123456789"}
    assert TariffAction.model_validate({**base, "planId": 2, "periodDays": 30}).period_days == 30
    with pytest.raises(ValidationError):
        TariffAction.model_validate({**base, "planId": 0, "periodDays": 30})
    assert ExtendAction.model_validate({**base, "days": 7}).days == 7
    for days in (0, -3, 3651):
        with pytest.raises(ValidationError):
            ExtendAction.model_validate({**base, "days": days})
