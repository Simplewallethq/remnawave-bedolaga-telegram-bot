from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.services import daily_subscription_metrics_service as service_module
from app.services.daily_subscription_metrics_service import DailySubscriptionMetricsService


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _service_at(value: datetime) -> DailySubscriptionMetricsService:
    return DailySubscriptionMetricsService(now_provider=lambda: value)


def test_yesterday_uses_moscow_date():
    service = _service_at(datetime(2024, 1, 10, 1, 0, tzinfo=MOSCOW_TZ))

    assert service._yesterday_moscow_date() == date(2024, 1, 9)


def test_as_of_is_end_of_moscow_day_as_utc_naive():
    service = DailySubscriptionMetricsService()

    assert service._as_of_for_date(date(2024, 1, 9)) == datetime(2024, 1, 9, 20, 59, 59, 999999)


async def test_collect_for_yesterday_skips_when_setting_already_collected():
    service = _service_at(datetime(2024, 1, 10, 12, 0, tzinfo=MOSCOW_TZ))
    service._get_last_snapshot_date = AsyncMock(return_value="2024-01-09")
    service._snapshot_exists = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock())

    result = await service.collect_for_yesterday(db)

    assert result == {"skipped": True, "reason": "already_collected", "date": "2024-01-09"}
    service._snapshot_exists.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_collect_for_yesterday_persists_metric(monkeypatch):
    service = _service_at(datetime(2024, 1, 10, 12, 0, tzinfo=MOSCOW_TZ))
    service._get_last_snapshot_date = AsyncMock(return_value=None)
    service._snapshot_exists = AsyncMock(return_value=False)
    service._count_paid_users = AsyncMock(return_value=7)
    service._count_lost_paid_users = AsyncMock(return_value=2)
    upsert_metric = AsyncMock(return_value=(SimpleNamespace(id=1), True))
    upsert_setting = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(service_module, "upsert_daily_subscription_metric", upsert_metric)
    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_setting)

    result = await service.collect_for_yesterday(db)

    assert result["skipped"] is False
    assert result["date"] == "2024-01-09"
    assert result["paid_users_count"] == 7
    assert result["lost_paid_users_count"] == 2
    upsert_metric.assert_awaited_once_with(
        db,
        metric_date=date(2024, 1, 9),
        paid_users_count=7,
        lost_paid_users_count=2,
    )
    upsert_setting.assert_awaited_once()
    db.commit.assert_awaited_once()


async def test_collect_for_yesterday_skips_existing_snapshot_and_updates_guard(monkeypatch):
    service = _service_at(datetime(2024, 1, 10, 12, 0, tzinfo=MOSCOW_TZ))
    service._get_last_snapshot_date = AsyncMock(return_value=None)
    service._snapshot_exists = AsyncMock(return_value=True)
    service._count_paid_users = AsyncMock()
    upsert_setting = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_setting)

    result = await service.collect_for_yesterday(db)

    assert result == {"skipped": True, "reason": "already_collected", "date": "2024-01-09"}
    service._count_paid_users.assert_not_awaited()
    upsert_setting.assert_awaited_once()
    db.commit.assert_awaited_once()
