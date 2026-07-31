from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.services import user_daily_metrics_service as service_module
from app.services.user_daily_metrics_service import UserDailyMetricsService


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _service_at(value: datetime) -> UserDailyMetricsService:
    return UserDailyMetricsService(now_provider=lambda: value)


def test_yesterday_uses_moscow_date():
    service = _service_at(datetime(2024, 1, 10, 1, 0, tzinfo=MOSCOW_TZ))

    assert service._yesterday_moscow_date() == date(2024, 1, 9)


def test_date_range_uses_moscow_day_as_utc_naive():
    service = UserDailyMetricsService()

    start, end = service._date_range_utc_naive(date(2024, 1, 9))

    assert start == datetime(2024, 1, 8, 21, 0, 0)
    assert end == datetime(2024, 1, 9, 20, 59, 59, 999999)


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
    now = datetime(2024, 1, 10, 12, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    service._get_last_snapshot_date = AsyncMock(return_value=None)
    service._snapshot_exists = AsyncMock(return_value=False)
    service._collect_metrics = AsyncMock(return_value={
        "new_telegram_users_count": 3,
        "new_bot_users_count": 3,
        "total_users_count": 10,
    })
    upsert_metric = AsyncMock(return_value=(SimpleNamespace(id=1), True))
    upsert_setting = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(service_module, "upsert_user_daily_metric", upsert_metric)
    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_setting)

    result = await service.collect_for_yesterday(db)

    assert result["skipped"] is False
    assert result["date"] == "2024-01-09"
    assert result["new_telegram_users_count"] == 3
    assert result["snapshot_at"] == "2024-01-10T09:00:00"
    upsert_metric.assert_awaited_once()
    _, kwargs = upsert_metric.await_args
    assert kwargs["metric_date"] == date(2024, 1, 9)
    assert kwargs["metrics"]["snapshot_at"] == datetime(2024, 1, 10, 9, 0)
    upsert_setting.assert_awaited_once()
    db.commit.assert_awaited_once()


async def test_collect_missing_recent_days_backfills_missing_dates(monkeypatch):
    service = _service_at(datetime(2024, 1, 10, 12, 0, tzinfo=MOSCOW_TZ))
    existing_date = date(2024, 1, 8)
    service._snapshot_exists = AsyncMock(side_effect=lambda _db, metric_date: metric_date == existing_date)
    service._collect_for_date = AsyncMock(return_value={"skipped": False})
    upsert_setting = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_setting)

    result = await service.collect_missing_recent_days(db, days=3)

    assert result["range_start"] == "2024-01-07"
    assert result["range_end"] == "2024-01-09"
    assert result["checked"] == 3
    assert result["created"] == 2
    assert result["skipped_existing"] == 1
    assert result["failed"] == 0
    assert result["created_dates"] == ["2024-01-07", "2024-01-09"]
    assert result["skipped_dates"] == ["2024-01-08"]
    assert [call.args[1] for call in service._collect_for_date.await_args_list] == [
        date(2024, 1, 7),
        date(2024, 1, 9),
    ]
    upsert_setting.assert_awaited_once()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
