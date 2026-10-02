"""
Оплаченная заглушка отказа в триале должна получить серверы при продлении.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.database.crud.server_squad as server_squad_crud
import app.database.crud.subscription as subscription_crud
from app.database.models import SubscriptionStatus

DEFAULT_SQUAD = "53b1b439-0396-49bc-ba5d-cab4340e6247"


def _subscription(**overrides):
    now = datetime.utcnow()
    data = dict(
        id=1,
        user_id=1,
        status=SubscriptionStatus.DISABLED.value,
        is_trial=False,
        end_date=now,
        connected_squads=[],
        traffic_used_gb=0.0,
        updated_at=now,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _db():
    return SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())


def _patch(monkeypatch, squads):
    monkeypatch.setattr(
        server_squad_crud,
        "get_active_server_squads",
        AsyncMock(return_value=[SimpleNamespace(squad_uuid=uuid) for uuid in squads]),
    )
    monkeypatch.setattr(subscription_crud, "clear_notifications", AsyncMock())


async def test_trial_denied_placeholder_gets_active_squads_on_paid_extend(monkeypatch):
    _patch(monkeypatch, [DEFAULT_SQUAD])
    subscription = _subscription()

    await subscription_crud.extend_subscription(_db(), subscription, 30)

    assert subscription.connected_squads == [DEFAULT_SQUAD]
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date > datetime.utcnow() + timedelta(days=29)


async def test_existing_squads_are_kept(monkeypatch):
    _patch(monkeypatch, [DEFAULT_SQUAD])
    subscription = _subscription(connected_squads=["other-squad"])

    await subscription_crud.extend_subscription(_db(), subscription, 30)

    assert subscription.connected_squads == ["other-squad"]


async def test_trial_extend_does_not_touch_squads(monkeypatch):
    _patch(monkeypatch, [DEFAULT_SQUAD])
    subscription = _subscription(is_trial=True, status=SubscriptionStatus.ACTIVE.value)

    await subscription_crud.extend_subscription(_db(), subscription, 3)

    assert subscription.connected_squads == []
