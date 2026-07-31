from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.services import trial_expiry_daily_metrics_service as service_module
from app.services.trial_expiry_daily_metrics_service import TrialExpiryDailyMetricsService


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _service_at(value: datetime) -> TrialExpiryDailyMetricsService:
    return TrialExpiryDailyMetricsService(now_provider=lambda: value)


def _event(user_id: int, occurred_at: datetime, extra=None, connected: bool = False):
    event = SimpleNamespace(user_id=user_id, occurred_at=occurred_at, extra=extra or {})
    event._snapshot_has_connected_to_vpn = connected
    return event


def test_target_date_uses_seven_day_lag_in_moscow():
    service = _service_at(datetime(2024, 7, 17, 1, 0, tzinfo=MOSCOW_TZ))

    assert service._target_date() == date(2024, 7, 10)


def test_date_range_uses_moscow_day_as_utc_naive():
    service = TrialExpiryDailyMetricsService()

    start, end = service._date_range_utc_naive(date(2024, 7, 10))

    assert start == datetime(2024, 7, 9, 21, 0, 0)
    assert end == datetime(2024, 7, 10, 21, 0, 0)


def test_extract_trial_ended_at_prefers_explicit_end():
    service = TrialExpiryDailyMetricsService()
    event = _event(
        1,
        datetime(2024, 7, 7, 10, 0),
        {"trial_ended_at": "2024-07-10T12:00:00Z", "trial_duration_days": 30},
    )

    assert service._extract_trial_ended_at(event) == datetime(2024, 7, 10, 12, 0)


async def test_build_trial_ended_user_sets_counts_connected_subset():
    service = TrialExpiryDailyMetricsService()
    target_start = datetime(2024, 7, 9, 21, 0)
    target_end = datetime(2024, 7, 10, 21, 0)
    events = [
        _event(1, datetime(2024, 7, 7, 12, 0), {"trial_duration_days": 3}, connected=True),
        _event(2, datetime(2024, 7, 7, 13, 0), {"trial_duration_days": 3}, connected=False),
        _event(3, datetime(2024, 7, 8, 13, 0), {"trial_duration_days": 3}, connected=True),
    ]

    ended, connected = await service._build_trial_ended_user_sets(events, target_start, target_end)

    assert ended == {1, 2}
    assert connected == {1}


async def test_collect_ready_cohort_persists_metric(monkeypatch):
    now = datetime(2024, 7, 17, 12, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    service._get_last_snapshot_date = AsyncMock(return_value=None)
    service._snapshot_exists = AsyncMock(return_value=False)
    service._get_activation_events = AsyncMock(return_value=[])
    service._build_trial_ended_user_sets = AsyncMock(return_value=({1, 2, 3}, {1, 3}))
    service._get_paid_user_ids = AsyncMock(return_value={2, 3})
    upsert_metric = AsyncMock(return_value=(SimpleNamespace(id=1), True))
    upsert_setting = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(service_module, "upsert_trial_expiry_daily_metric", upsert_metric)
    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_setting)

    result = await service.collect_ready_cohort(db)

    assert result["skipped"] is False
    assert result["date"] == "2024-07-10"
    assert result["trial_ended_count"] == 3
    assert result["trial_paid_7d_count"] == 2
    assert result["connected_trial_ended_count"] == 2
    assert result["connected_trial_paid_7d_count"] == 1
    upsert_metric.assert_awaited_once_with(
        db,
        metric_date=date(2024, 7, 10),
        snapshot_at=datetime(2024, 7, 17, 9, 0),
        trial_ended_count=3,
        trial_paid_7d_count=2,
        connected_trial_ended_count=2,
        connected_trial_paid_7d_count=1,
    )
    service._get_paid_user_ids.assert_awaited_once()
    paid_args, _ = service._get_paid_user_ids.await_args
    assert paid_args[2] == datetime(2024, 7, 9, 21, 0)
    assert paid_args[3] == datetime(2024, 7, 16, 21, 0)
    upsert_setting.assert_awaited_once()
    db.commit.assert_awaited_once()


async def test_collect_missing_ready_cohorts_backfills_missing_dates(monkeypatch):
    service = _service_at(datetime(2024, 7, 17, 12, 0, tzinfo=MOSCOW_TZ))
    existing_date = date(2024, 7, 9)
    service._snapshot_exists = AsyncMock(side_effect=lambda _db, metric_date: metric_date == existing_date)
    service._collect_for_date = AsyncMock(return_value={"skipped": False})
    upsert_setting = AsyncMock()
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_setting)

    result = await service.collect_missing_ready_cohorts(db, days=3)

    assert result["range_start"] == "2024-07-08"
    assert result["range_end"] == "2024-07-10"
    assert result["checked"] == 3
    assert result["created"] == 2
    assert result["skipped_existing"] == 1
    assert result["failed"] == 0
    assert result["created_dates"] == ["2024-07-08", "2024-07-10"]
    assert result["skipped_dates"] == ["2024-07-09"]
    assert [call.args[1] for call in service._collect_for_date.await_args_list] == [
        date(2024, 7, 8),
        date(2024, 7, 10),
    ]
    upsert_setting.assert_awaited_once()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
