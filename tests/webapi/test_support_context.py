"""Tests for the sanitized Tendi support-context endpoint."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.webapi.routes.support_context as support_context_module
from app.services.cabinet_service import build_support_context


class DummySession:
    pass


def _user(*, with_subscription: bool = True):
    plan = SimpleNamespace(code="plus", display_name="Plus")
    subscription = None
    if with_subscription:
        subscription = SimpleNamespace(
            remnawave_short_uuid="ABCD-1234",
            plan=plan,
            is_trial=False,
            actual_status="active",
            start_date=datetime(2026, 8, 1, 12, 0, 0),
            end_date=datetime.utcnow() + timedelta(days=31),
            days_left=30,
            device_limit=3,
            traffic_limit_gb=0,
            traffic_used_gb=12.345,
            autopay_enabled=True,
        )
    return SimpleNamespace(
        id=17,
        status="active",
        email="user@example.com",
        auth_source="telegram",
        username="leto_user",
        telegram_id=123456789,
        first_name="Лето",
        balance_kopeks=12550,
        remnawave_uuid="remna-uuid",
        subscription=subscription,
    )


def test_build_support_context_excludes_access_links() -> None:
    payload = build_support_context(_user())

    assert payload["user"]["accountId"] == "ABCD-1234"
    assert payload["user"]["balanceRub"] == 125.5
    assert payload["subscription"]["planCode"] == "plus"
    assert payload["subscription"]["planName"] == "Plus"
    assert payload["subscription"]["trafficLimitGb"] is None
    assert payload["subscription"]["trafficUsedGb"] == 12.35
    serialized = str(payload)
    assert "subscriptionUrl" not in serialized
    assert "happLink" not in serialized
    assert "incyLink" not in serialized


def test_build_support_context_without_subscription() -> None:
    payload = build_support_context(_user(with_subscription=False))
    assert payload["subscription"] is None


async def test_support_context_returns_404_for_unknown_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        support_context_module,
        "get_user_by_telegram_id",
        AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException) as exc_info:
        await support_context_module.get_support_context_by_telegram_id(
            999,
            _=object(),
            db=DummySession(),
        )

    assert exc_info.value.status_code == 404


async def test_support_context_serializes_known_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    monkeypatch.setattr(
        support_context_module,
        "get_user_by_telegram_id",
        AsyncMock(return_value=user),
    )

    payload = await support_context_module.get_support_context_by_telegram_id(
        user.telegram_id,
        _=object(),
        db=DummySession(),
    )

    assert payload["user"]["telegramId"] == user.telegram_id
    assert payload["subscription"]["deviceLimit"] == 3


async def test_support_context_resolves_cabinet_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    resolver = AsyncMock(return_value=user)
    monkeypatch.setattr(
        support_context_module,
        "get_user_by_support_account_id",
        resolver,
    )

    payload = await support_context_module.get_support_context_by_account_id(
        "ABCD-1234",
        _=object(),
        db=DummySession(),
    )

    resolver.assert_awaited_once()
    assert payload["user"]["accountId"] == "ABCD-1234"
