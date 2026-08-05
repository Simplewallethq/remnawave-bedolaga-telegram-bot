from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import User
from app.services import vpn_deposit_bonus_service as bonus_module
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _ExecuteResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _ScalarResult(self._values)


class _FakeSession:
    def __init__(self, execute_values=None):
        self.execute_values = list(execute_values or [])
        self.added = []
        self.commits = 0
        self.refreshed = []

    async def execute(self, *_args, **_kwargs):
        values = self.execute_values.pop(0) if self.execute_values else []
        return _ExecuteResult(values)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)


@pytest.fixture(autouse=True)
def disable_vpn_bonus_debug(monkeypatch):
    monkeypatch.setattr(bonus_module, "IS_DEBUG", False)


def test_expiry_and_reminder_cutoff_use_detection_time() -> None:
    before_cutoff = datetime(2026, 7, 29, 17, 59, tzinfo=timezone.utc)  # 20:59 MSK
    at_cutoff = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)  # 21:00 MSK

    assert vpn_deposit_bonus_service.expires_at_for_detection(before_cutoff) == datetime(
        2026,
        7,
        29,
        20,
        59,
        59,
    )
    assert not vpn_deposit_bonus_service.should_skip_reminder(before_cutoff)
    assert vpn_deposit_bonus_service.should_skip_reminder(at_cutoff)


async def test_first_vpn_connection_queues_touch_for_existing_topup_user(monkeypatch) -> None:
    db = _FakeSession()
    user = User(id=42, telegram_id=100500, has_made_first_topup=True)
    monkeypatch.setattr(vpn_deposit_bonus_service, "_has_touch_log", AsyncMock(return_value=False))
    monkeypatch.setattr(vpn_deposit_bonus_service, "_log_once", AsyncMock())

    queued = await vpn_deposit_bonus_service.on_first_vpn_connection_detected(
        db,
        user,
        source="test",
        panel_first_connected_at="2026-07-01T10:00:00Z",
    )

    assert queued
    assert len(db.added) == 1
    log = db.added[0]
    assert log.slot_key == vpn_deposit_bonus_service.TOUCH_1_SLOT
    assert log.status == "queued"
    assert log.payload["campaign_key"] == vpn_deposit_bonus_service.campaign_key(user.id)
    assert log.payload["source"] == "test"
    assert db.commits == 1


async def test_debug_mode_queues_only_configured_user(monkeypatch) -> None:
    monkeypatch.setattr(bonus_module, "IS_DEBUG", True)
    monkeypatch.setattr(bonus_module, "DEBUG_USER_ID", 18835)
    monkeypatch.setattr(vpn_deposit_bonus_service, "_has_touch_log", AsyncMock(return_value=False))
    monkeypatch.setattr(vpn_deposit_bonus_service, "_log_once", AsyncMock())

    blocked_db = _FakeSession()
    blocked = await vpn_deposit_bonus_service.on_first_vpn_connection_detected(
        blocked_db,
        User(id=42, telegram_id=100500),
        source="test",
    )

    allowed_db = _FakeSession()
    allowed = await vpn_deposit_bonus_service.on_first_vpn_connection_detected(
        allowed_db,
        User(id=18835, telegram_id=100501),
        source="test",
    )

    assert not blocked
    assert blocked_db.added == []
    assert allowed
    assert len(allowed_db.added) == 1


async def test_payment_decision_credits_bonus_once_then_invoice_amount() -> None:
    user_id = 42
    metadata = vpn_deposit_bonus_service.build_payment_metadata(user_id)
    first_db = _FakeSession(execute_values=[[]])
    duplicate_db = _FakeSession(execute_values=[[{"campaign_key": metadata["campaign_key"]}]])

    first = await vpn_deposit_bonus_service.resolve_payment_decision(
        first_db,
        user_id=user_id,
        payment_amount_kopeks=vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS,
        metadata=metadata,
    )
    duplicate = await vpn_deposit_bonus_service.resolve_payment_decision(
        duplicate_db,
        user_id=user_id,
        payment_amount_kopeks=vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS,
        metadata=metadata,
    )

    assert first.is_campaign_payment
    assert not first.is_duplicate
    assert first.credit_amount_kopeks == vpn_deposit_bonus_service.BONUS_AMOUNT_KOPEKS
    assert first.referral_amount_kopeks == vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS
    assert duplicate.is_campaign_payment
    assert duplicate.is_duplicate
    assert duplicate.credit_amount_kopeks == vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS
    assert duplicate.referral_amount_kopeks == vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS


async def test_payment_decision_rejects_amount_other_than_invoice_amount() -> None:
    user_id = 42
    metadata = vpn_deposit_bonus_service.build_payment_metadata(user_id)
    db = _FakeSession(execute_values=[[]])

    decision = await vpn_deposit_bonus_service.resolve_payment_decision(
        db,
        user_id=user_id,
        payment_amount_kopeks=99_000,
        metadata=metadata,
    )

    assert not decision.is_campaign_payment
    assert decision.credit_amount_kopeks == 0


def test_stars_payload_metadata_requires_invoice_amount() -> None:
    user_id = 42
    valid_payload = vpn_deposit_bonus_service.build_stars_payload(
        user_id,
        vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS,
    )
    wrong_payload = vpn_deposit_bonus_service.build_stars_payload(user_id, 99_000)

    assert vpn_deposit_bonus_service.amount_from_stars_payload(valid_payload) == (
        vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS
    )
    assert vpn_deposit_bonus_service.metadata_from_stars_payload(user_id, valid_payload)
    assert vpn_deposit_bonus_service.metadata_from_stars_payload(user_id, wrong_payload) is None


def test_metadata_expiry_check_ignores_metadata_without_deadline() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    assert not vpn_deposit_bonus_service.is_metadata_expired({}, now)
    assert not vpn_deposit_bonus_service.is_metadata_expired(
        {"expires_at": "2026-08-05T20:59:59"},
        now,
    )
    assert vpn_deposit_bonus_service.is_metadata_expired(
        {"expires_at": "2026-08-03T20:59:59"},
        now,
    )


async def test_touch_1_queue_pages_past_expired_logs() -> None:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    expired_log = SimpleNamespace(
        id=1,
        status="queued",
        payload={"expires_at": "2026-07-29T11:00:00"},
    )
    valid_log = SimpleNamespace(
        id=2,
        status="queued",
        payload={"expires_at": "2026-07-29T20:59:00"},
    )
    db = _FakeSession(execute_values=[[expired_log], [valid_log]])

    logs = await vpn_deposit_bonus_service.list_touch_1_queue(
        db,
        now_utc=now,
        limit=1,
    )

    assert logs == [valid_log]
    assert expired_log.status == "expired"


async def test_touch_2_candidates_skip_paid_expired_and_existing_reminders(monkeypatch) -> None:
    now = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)  # 21:00 MSK
    valid_payload = {
        "campaign_key": "campaign-valid",
        "detected_at": "2026-07-29T10:00:00",
        "expires_at": "2026-07-29T20:59:00",
    }
    paid_payload = {**valid_payload, "campaign_key": "campaign-paid"}
    skipped_payload = {**valid_payload, "campaign_key": "campaign-skipped", "skip_reminder": True}
    expired_payload = {**valid_payload, "campaign_key": "campaign-expired", "expires_at": "2026-07-29T17:59:00"}
    existing_payload = {**valid_payload, "campaign_key": "campaign-existing"}
    logs = [
        SimpleNamespace(id=1, user_id=10, payload=valid_payload, created_at=datetime(2026, 7, 29, 10, 0)),
        SimpleNamespace(id=2, user_id=10, payload=paid_payload, created_at=datetime(2026, 7, 29, 10, 0)),
        SimpleNamespace(id=3, user_id=10, payload=skipped_payload, created_at=datetime(2026, 7, 29, 10, 0)),
        SimpleNamespace(id=4, user_id=10, payload=expired_payload, created_at=datetime(2026, 7, 29, 10, 0)),
        SimpleNamespace(id=5, user_id=10, payload=existing_payload, created_at=datetime(2026, 7, 29, 10, 0)),
    ]
    db = _FakeSession(execute_values=[logs])

    async def fake_was_paid(_db, _user_id, campaign_key):
        return campaign_key == "campaign-paid"

    async def fake_has_touch_log(_db, _user_id, _slot_key, campaign_key, **_kwargs):
        return campaign_key == "campaign-existing"

    monkeypatch.setattr(vpn_deposit_bonus_service, "was_paid", fake_was_paid)
    monkeypatch.setattr(vpn_deposit_bonus_service, "_has_touch_log", fake_has_touch_log)

    candidates = await vpn_deposit_bonus_service.list_touch_2_candidates(db, now_utc=now)

    assert candidates == [logs[0]]
